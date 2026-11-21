"""관리자 팀 삭제, 수거 완료 후 이력 보존, 동일 요청 재시도의 HTTP 회귀."""
import unittest
import uuid

from test_label_usage_api import UsageHTTPCase
from dgpc import service as S


class CrewDeleteAPI(UsageHTTPCase):
    def test_only_admin_can_remove_unused_crew(self):
        name = "가상 잘못 등록한 팀"
        cid = self.post("crews", {"name": name}, self.admin_cookie)[1]["id"]
        path = f"crews/{cid}/delete"
        self.assertEqual(self.post(path, {}, self.worker_cookie)[0], 403)
        self.assertEqual(self.post(path, {}, None)[0], 401)
        for _ in range(2):
            status, result, _ = self.post(path, {}, self.admin_cookie)
            self.assertEqual((status, result), (200, {"ok": True, "archived": False}))
        self.assertNotIn(cid, [c["id"] for c in self.get("bootstrap", self.admin_cookie)[1]["crews"]])
        status, replacement, _ = self.post("crews", {"name": name}, self.admin_cookie)
        self.assertEqual(status, 200, replacement)
        self.assertNotEqual(replacement["id"], cid)

    def test_complete_then_archive_retains_dispatch_map_and_unload_records(self):
        day = S.today_kst()
        name = "가상 교체 대상팀"
        cid = self.post("crews", {"name": name}, self.admin_cookie)[1]["id"]
        request = S.create_request(self.con, self.request_body())
        rid = request["id"]
        status, assigned, _ = self.post("assign", {"date": day, "plan": {cid: [{"request_id": rid}]}}, self.admin_cookie)
        self.assertEqual(status, 200, assigned)
        S.add_unload(self.con, {"crew_id": cid, "work_date": day, "place": "가상 반입지", "volume_m3": 2})
        path = f"crews/{cid}/delete"
        status, reason, _ = self.post(path, {}, self.admin_cookie)
        self.assertEqual(status, 400, reason)
        current = S.get_request(self.con, rid)
        status, collected, _ = self.post(f"requests/{rid}/collection", {
            "client_req_id": str(uuid.uuid4()), "req_ver": current["req_ver"], "basis": current["collection"]["basis"],
            "collected_on": day, "items": [{"line_id": line["line_id"], "qty": 1} for line in current["collection"]["lines"]],
            "reason": "가상 수거 확인",
        }, self.admin_cookie)
        self.assertEqual(status, 200, collected)
        for _ in range(2):
            status, result, _ = self.post(path, {}, self.admin_cookie)
            self.assertEqual((status, result), (200, {"ok": True, "archived": True}))
        self.assertNotIn(cid, [c["id"] for c in self.get("bootstrap", self.admin_cookie)[1]["crews"]])
        status, batches = self.get(f"batches?date={day}&route_map=1", self.admin_cookie)
        self.assertEqual(status, 200, batches)
        self.assertEqual([(b["crew_name"], b["id"]) for b in batches], [(name, assigned[0]["batch_id"])])
        status, unloads = self.get(f"unloads?date={day}", self.admin_cookie)
        self.assertEqual(status, 200, unloads)
        self.assertEqual([(u["crew_name"], u["volume_m3"]) for u in unloads], [(name, 2)])


if __name__ == "__main__":
    unittest.main()
