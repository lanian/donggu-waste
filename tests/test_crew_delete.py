"""수거팀 삭제·과거 배차 보존·휴대폰 연결 변경을 임시 암호화 DB에서 확인한다."""
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

import test_pc_labels as fixture
from dgpc import area, core as C, pc_collection, service as S, store


class CrewDeleteTests(unittest.TestCase):
    setUp = fixture.NumberedLabelsTest.setUp
    tearDown = fixture.NumberedLabelsTest.tearDown
    request = fixture.NumberedLabelsTest.request

    def team(self, name="가상 임시팀", **kw):
        return S.save_crew(self.con, {"name": name, **kw})

    def phone(self):
        dev = str(uuid.uuid4())
        self.con.execute("""INSERT INTO device(id,kex_pub,sign_pub,status,registered_at)
            VALUES (?,x'01',x'02','ACTIVE',?)""", (dev, C.now()))
        return dev

    def delete(self, team):
        return S.delete_crew(self.con, team)

    def assert_deleted(self, team, archived):
        answer = {"ok": True, "archived": archived}
        self.assertEqual(self.delete(team), answer)
        self.assertEqual(self.delete(team), answer)
        self.assertNotIn(team, [c["id"] for c in S.list_crews(self.con)])
        with self.assertRaisesRegex(C.UserError, "삭제"):
            self.team(id=team)
        count = self.con.execute("""SELECT COUNT(*) FROM audit_log WHERE entity='crew'
            AND entity_id=? AND action IN ('DELETE','ARCHIVE')""", (team,)).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.con.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unused_hard_delete_preserves_other_teams_and_allows_same_name(self):
        old = self.team(id=str(uuid.uuid4()))
        inactive = self.team("가상 휴무팀", active=False)
        self.assert_deleted(old, False)
        self.assertIsNone(C.one(self.con, "SELECT * FROM crew WHERE id=?", (old,)))
        replacement = self.team()
        self.assertNotEqual(old, replacement)
        self.assertIn(inactive, [c["id"] for c in S.list_crews(self.con)])
        with self.assertRaisesRegex(C.UserError, "수거팀"):
            self.delete(str(uuid.uuid4()))

    def test_unload_only_history_keeps_name_after_restart(self):
        crew = self.team()
        S.add_unload(self.con, {"crew_id": crew, "work_date": "2024-01-01", "place": "가상반입지", "weight_kg": 20})
        self.assert_deleted(crew, True)
        self.con.close()
        self.con = store.connect()
        row = C.one(self.con, "SELECT * FROM crew WHERE id=?", (crew,))
        self.assertTrue(row["archived_at"])
        self.assertEqual(row["active"], 0)
        self.assertEqual(S.list_unloads(self.con, "2024-01-01")[0]["crew_name"], "가상 임시팀")
        with self.assertRaisesRegex(C.UserError, "이력"):
            self.team()

    def test_active_visits_past_and_future_block_until_collected_reassigned_or_cancelled(self):
        crew = self.team()
        other = self.team("가상 이동팀")
        past, future, cancel = [self.request()["id"] for _ in range(3)]
        old_day, next_day = "2020-01-13", "2099-01-13"
        S.confirm_plan(self.con, old_day, {crew: [{"request_id": past}]})
        S.confirm_plan(self.con, next_day, {crew: [{"request_id": future}, {"request_id": cancel}]})
        with self.assertRaisesRegex(C.UserError, old_day):
            self.delete(crew)
        record = S.get_request(self.con, past)
        pc_collection.save(self.con, past, {"req_ver": record["req_ver"], "basis": record["collection"]["basis"],
            "collected_on": old_day, "items": [{"line_id": record["items"][0]["line_id"], "qty": 1}],
            "client_req_id": str(uuid.uuid4())})
        with self.assertRaisesRegex(C.UserError, next_day):
            self.delete(crew)
        S.cancel_request(self.con, cancel, code="RESIDENT")
        S.confirm_plan(self.con, next_day, {crew: [], other: [{"request_id": future}]})
        self.assert_deleted(crew, True)
        historic = S.list_batches(self.con, old_day)
        self.assertEqual([(b["crew_name"], len(b["stops"])) for b in historic], [("가상 임시팀", 1)])
        self.assertEqual(S.get_request(self.con, future)["active_crew_id"], other)

    def test_linked_dong_account_pilot_and_phone_require_action_before_delete(self):
        crew = self.team()
        dong = area.save_dong(self.con, {"name": "가상 담당동", "crew_id": crew,
            "depot": {"label": "가상 차고지", "lat": 35.15, "lon": 126.91}})
        user = S.create_user(self.con, "crew-fixture", "가상 대원", "FIELD", "crew-fixture-password", crew_id=crew)
        S.save_pilot(self.con, {"crew_id": crew})
        with self.assertRaisesRegex(C.UserError, "행정동"):
            self.delete(crew)
        self.assertEqual(area.get_dong(self.con, "가상 담당동")["crew_id"], crew)
        area.save_dong(self.con, {"id": dong, "name": "가상 담당동", "crew_id": self.team("가상 대체팀")})
        with self.assertRaisesRegex(C.UserError, "계정"):
            self.delete(crew)
        S.set_user_crew(self.con, user, None)
        with self.assertRaisesRegex(C.UserError, "시범 운영"):
            self.delete(crew)
        S.save_pilot(self.con, {"crew_id": ""})
        dev = self.phone()
        self.team(id=crew, device_id=dev)
        with self.assertRaisesRegex(C.UserError, "휴대폰"):
            self.delete(crew)
        self.team(id=crew)
        self.assert_deleted(crew, False)
        self.assertEqual(C.one(self.con, "SELECT status FROM device WHERE id=?", (dev,))["status"], "ACTIVE")

    def test_inactive_crew_keeps_phone_until_explicit_unlink_and_transfer(self):
        dev = self.phone()
        owner = self.team("가상 휴무팀", device_id=dev, active=False)
        destination = self.team("가상 업무팀")
        with self.assertRaisesRegex(C.UserError, "가상 휴무팀.*해제"):
            self.team("가상 신규팀", device_id=dev)
        with self.assertRaisesRegex(C.UserError, "가상 휴무팀.*해제"):
            self.team("가상 업무팀", id=destination, device_id=dev, expected_device_id=None)
        self.team("가상 휴무팀", id=owner, active=False)
        self.team("가상 업무팀", id=destination, device_id=dev, expected_device_id=None)
        self.assertEqual(self.con.execute("SELECT id FROM crew WHERE device_id=?", (dev,)).fetchall(), [(destination,)])

    def test_existing_duplicate_can_be_edited_and_unlinked(self):
        dev = self.phone()
        self.team("가상 과거팀", device_id=dev, active=False)
        duplicate = self.team("가상 중복팀")
        self.con.execute("UPDATE crew SET device_id=? WHERE id=?", (dev, duplicate))
        self.team("가상 중복팀", id=duplicate, device_id=dev, capacity_m3=11)
        with self.assertRaises(C.UserError):
            self.team("가상 추가팀", device_id=dev)
        self.team("가상 중복팀", id=duplicate, device_id=None)
        self.assertEqual(self.con.execute("SELECT name FROM crew WHERE device_id=?", (dev,)).fetchall(), [("가상 과거팀",)])

    def test_stale_expected_phone_cannot_replace_current_target(self):
        before, after = self.phone(), self.phone()
        target = self.team("가상 연결팀")
        self.team("가상 연결팀", id=target, device_id=before, expected_device_id="")
        with self.assertRaisesRegex(C.UserError, "바뀌"):
            self.team("가상 연결팀", id=target, device_id=after, expected_device_id=None)
        self.team("가상 연결팀", id=target, device_id=after, expected_device_id=before)
        self.assertEqual(C.one(self.con, "SELECT device_id FROM crew WHERE id=?", (target,))["device_id"], after)

    def test_concurrent_team_links_choose_one_owner(self):
        dev = self.phone()
        ready = threading.Barrier(2, timeout=20)

        def link(name):
            con = store.connect()
            try:
                ready.wait()
                try:
                    return S.save_crew(con, {"name": name, "device_id": dev})
                except C.UserError as exc:
                    self.assertIn("해제", str(exc))
                    return None
            finally:
                con.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            owners = [cid for cid in pool.map(link, ("가상 동시 A", "가상 동시 B")) if cid]
        self.assertEqual(len(owners), 1)
        self.assertEqual(self.con.execute("SELECT id FROM crew WHERE device_id=?", (dev,)).fetchall(), [(owners[0],)])

    def test_loaded_route_without_pickups_requires_its_unload_or_day_close(self):
        today = C.today_kst()
        history = {"final_unload": {"final": True, "label": "가상반입지"},
                   "initial_load": {"volume_m3": 2, "weight_kg": 0}, "end": {"lat": 35.19, "lon": 126.91}}
        for result in ("하차", "마감"):
            with self.subTest(result=result):
                crew = self.team("가상 " + result + " 복귀팀")
                unload = {"crew_id": crew, "work_date": today, "place": "가상반입지", "volume_m3": 2}
                S.add_unload(self.con, unload)
                S.confirm_plan(self.con, today, {crew: []}, histories={crew: history})
                with self.assertRaisesRegex(C.UserError, "최종 하차"):
                    self.delete(crew)
                if result == "하차":
                    S.add_unload(self.con, unload)
                else:
                    S.close_day(self.con, today)
                self.assert_deleted(crew, True)


if __name__ == "__main__":
    unittest.main()
