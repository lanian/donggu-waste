"""확정 배정 변경을 가상 접수와 실제 PC API·노선 계산으로 검증."""
import concurrent.futures
import threading
import unittest
import uuid
from unittest.mock import patch

from test_label_usage_api import UsageHTTPCase
from dgpc import core as C, server as SV, service as S
from dgpc.pc_collection import save as save_pc_collection

DAY = "2099-01-05"


class AssignmentChangeReviewTest(UsageHTTPCase):
    def setUp(self):
        super().setUp()
        self.crews = [S.save_crew(self.con, {"name": f"가상 재계산 {i}팀", "capacity_m3": 8,
                       "shift_start": "09:00", "shift_end": "18:00"}) for i in (1, 2, 3)]
        C.set_setting(self.con, "depot", {"lat": 35.14, "lon": 126.90, "label": "가상 차고지"})

    def receipt(self, name, **extra):
        return S.create_request(self.con, {**self.request_body(), "name": name, "planned_date": DAY,
            "lat": 35.141, "lon": 126.901, "items": [{"fee_rate_id": self.fee, "qty": 1}], **extra})["id"]

    def plan(self, **routes):
        return S.confirm_plan(self.con, DAY, {self.crews[int(i[-1])]: [{"request_id": rid, "eta": "11:11"}
            for rid in ids] for i, ids in routes.items()})

    def current(self):
        return {r["crew_id"]: r for r in S.current_assignment(self.con, DAY)}

    def change(self, op, affected, **details):
        cur = self.current()
        return {"op": op, "assign_ver": {c: cur[c]["assign_ver"] if c in cur else 0 for c in affected},
                "expected_order": {c: [s["request_id"] for s in cur.get(c, {}).get("stops", [])] for c in affected}, **details}

    def review(self, change, expected=200, cookie=None):
        status, result, _ = self.post("route", {"date": DAY, "time_limit": 1, "assignment_change": change}, cookie or self.admin_cookie)
        self.assertEqual(status, expected, result)
        return result

    def commit(self, review, expected=200):
        status, result, _ = self.post("assign/edit", {"date": DAY, "review_basis": review["review_basis"], "reason": "가상 당일 조정"}, self.admin_cookie)
        self.assertEqual(status, expected, result)
        return result

    def test_insert_reorder_move_remove_and_only_changed_revisions(self):
        a, b, c = self.crews
        r1, r2, r3, keep = [self.receipt(f"가상 방문 {i}") for i in range(4)]
        self.plan(c0=[r1], c1=[r2], c2=[keep])
        unaffected = self.current()[c]
        steps = [
            ("insert", [a], {"crew_id": a, "request_id": r3}, {a: [r1, r3]}),
            ("reorder", [a], {"crew_id": a, "order": [r3, r1]}, {a: [r3, r1]}),
            ("move", [a, b], {"to_crew_id": b, "request_id": r3}, {a: [r1], b: [r2, r3]}),
            ("remove", [a], {"request_id": r1}, {a: []}),
        ]
        for op, affected, details, expected in steps:
            with self.subTest(op=op):
                before = self.current()
                code, _, _ = self.post("assign/edit", {"date": DAY, "op": op, **details}, self.admin_cookie)
                self.assertEqual(code, 409)
                preview = self.review(self.change(op, affected, **details))
                self.assertEqual(self.current(), before, "검토는 배정을 저장하지 않는다")
                self.assertEqual({r["crew_id"]: [s["request_id"] for s in r["stops"]] for r in preview["routes"]}, expected)
                if any(expected.values()):
                    self.assertEqual(preview["matrix_source"], "ESTIMATE")
                    self.assertTrue(all(s["eta"] and s["eta"] != "11:11" for r in preview["routes"] for s in r["stops"]))
                saved = self.commit(preview)
                self.assertEqual(set(saved["resend_needed"]), set(affected))
                after = self.current()
                self.assertEqual(after[c], unaffected)
                for cid in affected:
                    self.assertEqual(after[cid]["assign_ver"], before[cid]["assign_ver"] + 1)
                    self.assertEqual([s["request_id"] for s in after[cid]["stops"]], expected[cid])

    def test_completed_stop_and_collected_items_stay_in_history(self):
        a = self.crews[0]
        done, left, extra = [self.receipt(f"가상 수거 보존 {i}") for i in range(3)]
        history = self.plan(c0=[done, left])[0]["batch_id"]
        req = S.get_request(self.con, done)
        save_pc_collection(self.con, done, {"req_ver": req["req_ver"], "basis": req["collection"]["basis"],
            "collected_on": C.today_kst(), "items": [{"line_id": req["items"][0]["line_id"], "qty": 1}],
            "client_req_id": str(uuid.uuid4())})
        before = S.get_request(self.con, done)
        review = self.review(self.change("insert", [a], crew_id=a, request_id=extra))
        self.assertNotIn(done, review["requests"])
        self.commit(review)
        after = S.get_request(self.con, done)
        self.assertEqual((after["req_ver"], after["status"], after["state"]["lines"], after["events"]),
                         (before["req_ver"], "DONE", before["state"]["lines"], before["events"]))
        self.assertEqual(C.one(self.con, "SELECT eta FROM assignment_stop WHERE request_id=? AND batch_id=?", (done, history))["eta"], "11:11")
        self.assertNotIn(done, [x["request_id"] for x in self.current()[a]["stops"]])

    def test_old_revision_and_changed_data_during_review_and_commit(self):
        a = self.crews[0]
        first, extra = [self.receipt(f"가상 동시 변경 {i}") for i in range(2)]
        self.plan(c0=[first])
        old = self.change("insert", [a], crew_id=a, request_id=extra)
        self.plan(c0=[first])
        self.review(old, 409)
        current = self.change("insert", [a], crew_id=a, request_id=extra)
        before = self.current()
        entered, release = threading.Event(), threading.Event()
        actual = SV.routing.plan

        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(10))
            return actual(*args, **kwargs)

        with patch.object(SV.routing, "plan", delayed), concurrent.futures.ThreadPoolExecutor() as pool:
            request = pool.submit(self.review, current, 409)
            try:
                self.assertTrue(entered.wait(10))
                status, _, _ = self.post(f"requests/{extra}/location", {"lat": 35.143, "lon": 126.902}, self.admin_cookie)
                self.assertEqual(status, 200)
            finally:
                release.set()
            request.result()
        self.assertEqual(self.current(), before)
        preview = self.review(current)
        C.set_setting(self.con, "service_min_base", S.settings(self.con)["service_min_base"] + 1)
        self.commit(preview, 409)
        self.assertEqual(self.current(), before)

    def test_visit_and_shift_violations_and_road_time_source(self):
        a = self.crews[0]
        early = self.receipt("가상 이른 약속", appt_start="09:00", appt_end="09:18")
        late = self.receipt("가상 늦은 약속", appt_start="10:30", appt_end="11:00")
        self.plan(c0=[early, late])
        before = self.current()
        wrong = self.review(self.change("reorder", [a], crew_id=a, order=[late, early]))
        self.assertTrue(wrong["infeasible"])
        self.assertNotIn("review_basis", wrong)
        self.assertTrue(any("방문 약속" in r["reason"] for r in wrong["unassigned"]))
        timed = self.review(self.change("reorder", [a], crew_id=a, order=[late, early], start_after="18:00"))
        self.assertTrue(any("근무 종료" in r["reason"] for r in timed["unassigned"]))
        self.assertEqual(self.current(), before)
        C.set_setting(self.con, "travel_method", "OSRM")
        extra = self.receipt("가상 도로 시간")

        def road(points, *args):
            return [[0 if i == j else 2 for j in range(len(points))] for i in range(len(points))], "OSRM", {}

        with patch.object(SV.routing, "build_matrix", side_effect=road):
            result = self.review(self.change("insert", [a], crew_id=a, request_id=extra))
        self.assertEqual(result["matrix_source"], "OSRM")
        self.assertGreater(result["routes"][0]["drive_min"], 0)
        self.commit(result)

    def test_capacity_requires_valid_unload_and_records_recalculated_stops(self):
        a = self.crews[0]
        one, two = self.receipt("가상 적재 1"), self.receipt("가상 적재 2")
        S.save_crew(self.con, {"id": a, "name": "가상 재계산 1팀", "capacity_m3": 1, "shift_end": "18:00"})
        C.set_setting(self.con, "volume_overrides", {self.fee: .7})
        self.plan(c0=[one])
        change = self.change("insert", [a], crew_id=a, request_id=two)
        blocked = self.review(change)
        self.assertNotIn("review_basis", blocked)
        self.assertTrue(any("적재한도" in r["reason"] for r in blocked["unassigned"]))
        C.set_setting(self.con, "unload_site", {"lat": 35.14, "lon": 126.90, "label": "가상 반입지"})
        C.set_setting(self.con, "unload_min", 5)
        C.set_setting(self.con, "max_unloads", 1)
        review = self.review(change)
        self.assertTrue(review["routes"], review.get("unassigned"))
        route = review["routes"][0]
        self.assertEqual(route["unloads"], 1)
        self.assertEqual(route["stops"][0]["unload_after"]["label"], "가상 반입지")
        self.assertEqual([s["load_m3"] for s in route["stops"]], [.7, .7])
        self.commit(review)
        saved = self.current()[a]["stops"]
        self.assertEqual(saved[0]["unload_after"]["eta"], route["stops"][0]["unload_after"]["eta"])

    def test_dong_account_cannot_review_or_confirm_office_assignment_edit(self):
        a = self.crews[0]
        one, two = self.receipt("가상 권한 1"), self.receipt("가상 권한 2")
        self.plan(c0=[one])
        change = self.change("insert", [a], crew_id=a, request_id=two)
        dong = C.one(self.con, "SELECT id FROM dong WHERE name='충장동'")["id"]
        pw = "assign-scope-test-only-123"
        S.create_user(self.con, "assign_scope", "가상 동 담당", "DONG", pw, dong_ids=[dong])
        cookie = self.login("assign_scope", pw)
        self.review(change, 403, cookie)
        preview = self.review(change)
        self.assertEqual(self.post("assign/edit", {"date": DAY, "review_basis": preview["review_basis"]}, cookie)[0], 403)
        self.assertEqual(self.current()[a]["assign_ver"], 1)


if __name__ == "__main__":
    unittest.main()
