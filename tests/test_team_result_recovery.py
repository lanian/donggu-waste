"""가상 목록·서명 결과를 실제 PC 서버로 반입해 팀별 회수 근거를 검증."""
import uuid
import unittest
from unittest.mock import patch

from test_pc import Phone
from test_label_usage_api import UsageHTTPCase
from dgpc import core as C, service as S
from dgpc.pc_collection import save as save_pc_collection
import dgproto as P

DAY = "2026-01-05"
NEXT = "2026-01-06"


class TeamRecoveryTest(UsageHTTPCase):
    def team(self, name):
        phone = Phone(S.registration_text(self.con))
        dev = S.register_device(self.con, phone.code(), name, confirm=True)["device_id"]
        return S.save_crew(self.con, {"name": name, "device_id": dev}), phone

    def plan(self, day, crew, *receipts, at="00:10"):
        with patch("dgpc.assign.now", return_value=f"{day}T{at}:00Z"):
            return S.confirm_plan(self.con, day, {crew: [{"request_id": r["id"]} for r in receipts]})[0]

    def send(self, day, batch, phone, confirmed=True, at="00:15"):
        with patch("dgpc.assign.now", return_value=f"{day}T{at}:00Z"):
            transfer = S.make_bundle(self.con, batch["batch_id"])
            _, payload, receipt = phone.receive(transfer["frames"])
            if confirmed:
                S.confirm_receipt(self.con, transfer["bundle_id"], receipt)
        return transfer, payload["stops"], receipt

    def event(self, day, phone, stop, kind="ATTEMPT", body=None, at="00:40"):
        event = phone.ev(stop, kind, body if body is not None else {"outcome": "DONE"})
        event["device_ts"] = f"{day}T{at}:00Z"
        return event

    def upload(self, day, phone, *events, stamp=None, channel="MAIL"):
        data = phone.seal({"schema": "dg.result/1", "work_date": day, "events": list(events)}, P.TYPE_RESULT)
        with patch("dgpc.results.now", return_value=stamp or f"{day}T01:00:00Z"):
            return data, S.import_result(self.con, data, channel)

    def summary(self, day=DAY, cookie=None):
        code, result = self.get(f"close?date={day}", cookie or self.admin_cookie)
        self.assertEqual(code, 200, result)
        return result

    def team_row(self, crew, day=DAY):
        return next(r for r in self.summary(day)["result_recovery"] if r["crew_id"] == crew)

    def test_no_result_receipt_if_an_event_cannot_be_saved_even_as_conflict(self):
        crew, phone = self.team("가상 저장 장애 팀")
        batch = self.plan(DAY, crew, self.request()[0], self.request()[0])
        _, stops, _ = self.send(DAY, batch, phone)
        events = [self.event(DAY, phone, stop) for stop in stops]
        failed_id = str(uuid.UUID(events[-1]["event_id"]))
        data = phone.seal({"schema": "dg.result/1", "work_date": DAY, "events": events}, P.TYPE_RESULT)
        self.con.execute(f"""CREATE TRIGGER fixture_block_event BEFORE INSERT ON result_event
            WHEN NEW.event_id='{failed_id}' BEGIN SELECT RAISE(ABORT, 'fixture storage failure'); END""")
        try:
            status, body, _ = self.post("import/result", data, self.admin_cookie)
            self.assertEqual(status, 400, body)
            self.assertIn("반영 확인 코드", body["error"])
            for table in ("result_import", "result_event", "result_seq"):
                self.assertEqual(self.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        finally:
            self.con.execute("DROP TRIGGER fixture_block_event")
        status, body, _ = self.post("import/result", data, self.admin_cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["applied"], 2)
        self.assertTrue(body["release_code"])

    def test_separate_files_duplicate_and_current_receipt_confirmation(self):
        crew, phone = self.team("가상 회수 A팀")
        receipts = [self.request()[0] for _ in range(2)]
        self.assertEqual(self.summary()["result_recovery"], [])
        batch = self.plan(DAY, crew, *receipts)
        self.assertEqual(self.team_row(crew)["receipt_status"], "UNSENT")
        bundle, stops, code = self.send(DAY, batch, phone, confirmed=False)
        row = self.team_row(crew)
        self.assertEqual((row["receipt_status"], row["stage"], row["missing"]), ("UNCONFIRMED", "WAITING", 2))
        first = self.event(DAY, phone, stops[0], body={"outcome": "PARTIAL"})
        raw, applied = self.upload(DAY, phone, first, channel="QR")
        self.assertEqual(applied["applied"], 1)
        row = self.team_row(crew)
        self.assertEqual((row["receipt_status"], row["stage"], row["applied"], row["missing"], row["last_applied_at"]),
                         ("UNCONFIRMED", "PARTIAL", 1, 1, DAY + "T01:00:00Z"))
        self.assertTrue(S.import_result(self.con, raw)["duplicate_file"])
        self.assertEqual(self.upload(DAY, phone, first, stamp=DAY + "T02:00:00Z")[1]["skipped_duplicate"], 1)
        self.assertEqual(self.team_row(crew)["last_applied_at"], DAY + "T01:00:00Z")
        self.assertIsNone(C.one(self.con, "SELECT receipt_confirmed_at FROM transfer_bundle WHERE id=?", (bundle["bundle_id"],))["receipt_confirmed_at"])
        S.confirm_receipt(self.con, bundle["bundle_id"], code)
        self.upload(DAY, phone, self.event(DAY, phone, stops[1]), stamp=DAY + "T02:10:00Z")
        row = self.team_row(crew)
        self.assertEqual((row["receipt_status"], row["stage"], row["applied"], row["missing"], row["last_applied_at"]),
                         ("CONFIRMED", "COMPLETE", 2, 0, DAY + "T02:10:00Z"))
        self.assertEqual(self.summary()["crews_no_result"], [])
        self.send(DAY, batch, phone, confirmed=False, at="02:20")
        row = self.team_row(crew)
        self.assertEqual((row["receipt_status"], row["stage"], row["last_applied_at"]),
                         ("UNCONFIRMED", "COMPLETE", DAY + "T02:10:00Z"))
        replacement = Phone(S.registration_text(self.con))
        dev = S.register_device(self.con, replacement.code(), "가상 교체 단말", confirm=True)["device_id"]
        S.save_crew(self.con, {"id": crew, "name": "가상 회수 A팀", "device_id": dev})
        row = self.team_row(crew)
        self.assertEqual((row["receipt_status"], row["applied"]), ("UNSENT", 2),
                         "교체 단말의 수신 상태와 이전 단말에서 실제 반영한 결과를 분리")

    def test_other_date_team_and_old_revision_do_not_prove_current_recovery(self):
        crew, phone = self.team("가상 날짜·회차 팀")
        foreign, other = self.team("가상 다른 팀")
        r, _ = self.request()
        distant, _ = self.request()
        old = self.plan(DAY, crew, r)
        _, old_stops, _ = self.send(DAY, old, phone)
        current = self.plan(DAY, crew, r, at="00:20")
        _, stops, _ = self.send(DAY, current, phone, confirmed=False, at="00:25")
        other_date = self.plan(NEXT, crew, distant)
        _, distant_stops, _ = self.send(NEXT, other_date, phone)
        self.plan(DAY, foreign, self.request()[0])
        self.upload(NEXT, phone, self.event(NEXT, phone, distant_stops[0]))
        foreign_event = self.event(DAY, other, stops[0], "ATTEMPT")
        _, wrong_team = self.upload(DAY, other, foreign_event)
        self.assertEqual(wrong_team["conflict"], 1)
        _, old_applied = self.upload(DAY, phone, self.event(DAY, phone, old_stops[0], body={"outcome": "PARTIAL"}))
        self.assertEqual(old_applied["applied"], 1, "같은 팀의 옛 갱신본은 기존 규칙으로 반영")
        self.assertEqual(S.get_request(self.con, r["id"])["status"], "PARTIAL")
        row = self.team_row(crew)
        self.assertEqual((row["assign_ver"], row["receipt_status"], row["applied"], row["missing"], row["last_applied_at"]), (2, "UNCONFIRMED", 0, 1, None))
        self.assertEqual(self.team_row(crew, NEXT)["applied"], 1)
        self.assertEqual(self.team_row(foreign)["receipt_status"], "UNSENT")
        S.resolve_conflict(self.con, foreign_event["event_id"], "APPLY", "가상 다른 팀 결과 수동 반영")
        self.assertEqual(self.team_row(crew)["applied"], 0, "수동 반영도 출처 일치가 필요")
        _, wrong_date = self.upload(NEXT, phone, self.event(DAY, phone, stops[0]))
        self.assertEqual(wrong_date["applied"], 1, "기존 결과 반영 규칙은 그대로 둔다")
        self.assertEqual(self.team_row(crew)["applied"], 0)

    def test_item_only_conflict_manual_resolution_and_pc_entry(self):
        crew, phone = self.team("가상 수동 반영 팀")
        a, line = self.request()
        b, _ = self.request()
        c, _ = self.request()
        batch = self.plan(DAY, crew, a, b, c)
        _, stops, _ = self.send(DAY, batch, phone)
        self.upload(DAY, phone, self.event(DAY, phone, stops[0], "ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 1}))
        # PC 직접 수거는 팀 단말 결과와 별도 집계하고, 개별 접수의 결과 대기에서 제외한다.
        req = S.get_request(self.con, b["id"])
        with patch("dgpc.core.now", return_value=DAY + "T00:50:00Z"):
            save_pc_collection(self.con, b["id"], {"req_ver": req["req_ver"], "basis": req["collection"]["basis"],
                "collected_on": DAY, "items": [{"line_id": req["items"][0]["line_id"], "qty": 3}],
                "client_req_id": str(uuid.uuid4())})
        conflict = self.event(DAY, phone, stops[1])
        _, result = self.upload(DAY, phone, conflict)
        self.assertEqual(result["conflict"], 1)
        row = self.team_row(crew)
        self.assertEqual((row["applied"], row["pc"], row["missing"], row["last_applied_at"]), (0, 1, 2, DAY + "T00:50:00Z"))
        S.resolve_conflict(self.con, conflict["event_id"], "REJECT", "가상 거부")
        self.assertEqual(self.team_row(crew)["missing"], 2)
        # 현재 회차와 일치하는 충돌은 실제 수동 반영된 뒤에만 센다.
        self.con.execute("UPDATE request SET status='CANCELLED' WHERE id=?", (c["id"],))
        manual = self.event(DAY, phone, stops[2])
        self.assertEqual(self.upload(DAY, phone, manual)[1]["conflict"], 1)
        self.con.execute("UPDATE request SET status='ASSIGNED' WHERE id=?", (c["id"],))
        with patch("dgpc.results.now", return_value=DAY + "T03:00:00Z"):
            S.resolve_conflict(self.con, manual["event_id"], "APPLY", "가상 실제 결과 확인")
        self.assertEqual((self.team_row(crew)["applied"], self.team_row(crew)["last_applied_at"]), (1, DAY + "T03:00:00Z"))

    def test_revisit_carry_and_team_scope(self):
        crew, phone = self.team("가상 재방문 팀")
        another, _ = self.team("가상 비공개 팀")
        r, _ = self.request()
        outstanding, _ = self.request()
        self.plan(DAY, another, self.request()[0])
        batch = self.plan(DAY, crew, r, outstanding)
        _, stops, _ = self.send(DAY, batch, phone)
        result = self.event(DAY, phone, stops[0], body={"outcome": "NOT_COLLECTED"})
        revisit = self.event(DAY, phone, stops[0], "REVISIT_APPT", {"date": NEXT})
        self.upload(DAY, phone, result, revisit)
        self.assertEqual(self.team_row(crew)["applied"], 1)
        after_close = S.close_day(self.con, DAY)
        self.assertEqual([x["id"] for x in after_close["carried"] if x["id"] in {r["id"], outstanding["id"]}], [outstanding["id"]])
        self.assertTrue(any("가상 재방문 팀" in w for w in after_close["warnings"]))
        self.assertEqual(self.team_row(crew)["missing"], 1)
        next_batch = self.plan(NEXT, crew, r)
        _, next_stops, _ = self.send(NEXT, next_batch, phone)
        self.assertEqual(self.team_row(crew, NEXT)["missing"], 1)
        self.upload(NEXT, phone, self.event(NEXT, phone, next_stops[0]))
        self.assertEqual(self.team_row(crew, NEXT)["applied"], 1)
        pw = "team-fixture-password-123"
        S.create_user(self.con, "recovery_field", "가상 수거팀", "FIELD", pw, crew_id=crew)
        field = self.login("recovery_field", pw)
        self.assertEqual([row["crew_id"] for row in self.summary(cookie=field)["result_recovery"]], [crew])
        dong = C.one(self.con, "SELECT id FROM dong WHERE name='충장동'")["id"]
        S.create_user(self.con, "recovery_dong", "가상 동 담당", "DONG", pw, dong_ids=[dong])
        self.assertEqual(self.get(f"close?date={DAY}", self.login("recovery_dong", pw))[0], 403)

    def test_legacy_evidence_and_reused_date_version_without_separate_proof(self):
        crew, phone = self.team("가상 출처 확인 팀")
        r, _ = self.request()
        first = self.plan(DAY, crew, r)
        _, stops, _ = self.send(DAY, first, phone)
        event = self.event(DAY, phone, stops[0], body={"outcome": "NOT_COLLECTED"})
        old_package = phone.seal({"schema": "dg.result/1", "events": [event]}, P.TYPE_RESULT)
        with patch("dgpc.results.now", return_value=DAY + "T01:00:00Z"):
            self.assertEqual(S.import_result(self.con, old_package)["applied"], 1)
        self.assertEqual(self.team_row(crew)["applied"], 1, "보존된 전송과 기록 시각으로 옛 반영을 확인")
        # 예정일을 따로 수정하지 않고 재배정하면 같은 접수 버전·갱신본 번호가 다른 날짜에 재사용될 수 있다.
        current = self.plan(NEXT, crew, r)
        _, latest_stops, _ = self.send(NEXT, current, phone)
        self.assertEqual((stops[0]["assign_ver"], stops[0]["req_ver"]),
                         (latest_stops[0]["assign_ver"], latest_stops[0]["req_ver"]))
        self.assertEqual(self.team_row(crew, NEXT)["uncertain"], 0, "다음 날 전송 전에 기록한 결과는 그날의 후보가 아님")
        self.upload(NEXT, phone, self.event(NEXT, phone, latest_stops[0]))
        row = self.team_row(crew, NEXT)
        self.assertEqual((row["applied"], row["missing"], row["uncertain"]), (0, 1, 1))
        self.assertEqual(S.get_request(self.con, r["id"])["status"], "DONE")

    def test_display_must_precede_import_and_include_the_stop(self):
        crew, phone = self.team("가상 발송 제외 팀")
        a, _ = self.request()
        b, _ = self.request()
        batch = self.plan(DAY, crew, a, b)
        S.reschedule_requests(self.con, [b["id"]], NEXT, "OTHER", "가상 출발 전 일정 변경")
        not_yet_sent, _ = S.build_daylist(self.con, batch["batch_id"])
        first = self.event(DAY, phone, not_yet_sent["stops"][0], at="01:00")
        self.assertEqual(self.upload(DAY, phone, first)[1]["applied"], 1)
        _, stops, _ = self.send(DAY, batch, phone, at="01:00")
        self.assertEqual([s["request_id"] for s in stops], [a["id"]])
        old_stop = {"request_id": b["id"], "assign_ver": batch["assign_ver"], "req_ver": b["req_ver"]}
        _, result = self.upload(DAY, phone, self.event(DAY, phone, old_stop, at="01:10"), stamp=DAY + "T01:20:00Z")
        self.assertEqual(result["applied"], 1)
        row = self.team_row(crew)
        self.assertEqual((row["total"], row["applied"], row["missing"]), (1, 0, 1))


if __name__ == "__main__":
    unittest.main()
