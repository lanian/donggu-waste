"""단독 마감의 관리자 설정, 검토 대기, 암호화 백업과 같은 HTTP 요청 재시도."""
import unittest
import uuid

from test_label_usage_api import UsageHTTPCase
from dgpc import backup, service as S


class SoloCloseAPI(UsageHTTPCase):
    def test_single_close_commits_once_and_makes_one_readable_backup(self):
        day = S.today_kst()
        password = "solo-backup-test-only"
        pem, _ = backup.generate_key(self.con, password)
        S.create_request(self.con, self.request_body())
        key = "close_approval_required"
        self.assertIs(self.get("bootstrap", self.admin_cookie)[1]["settings"].get(key), True)
        self.assertEqual(self.post("settings", {key: False}, self.worker_cookie)[0], 403)
        self.assertEqual(self.post("settings", {key: False}, self.admin_cookie)[0], 200)
        self.post_timeout = 30
        body = {"date": day, "note": "단독 운영 시험", "client_req_id": str(uuid.uuid4())}
        status, result, _ = self.post("close", body, self.worker_cookie)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["backup"]["ok"], result["backup"])
        status, repeated, _ = self.post("close", body, self.worker_cookie)
        self.assertEqual(status, 200, repeated)
        self.assertEqual(repeated["seq"], result["seq"])
        self.assertEqual(repeated["backup"], result["backup"])
        files = list(backup.backup_dir(self.con).glob("*.dgabk"))
        self.assertEqual(len(files), 1)
        self.assertEqual(backup.verify_auto(self.con, files[0].name, pem, password)["requests"], 1)
        summary = self.get(f"close?date={day}", self.admin_cookie)[1]
        self.assertIs(summary[key], False)
        self.assertEqual(len(summary["closes"]), 1)
        self.assertIs(summary["closed"]["approval_required"], False)
        self.assertIsNone(summary["closed"]["approved_by"])
        self.assertEqual(self.post("settings", {key: True}, self.admin_cookie)[0], 200)
        pending = self.get("pending", self.admin_cookie)[1]["items"]
        self.assertFalse(any(row["key"] == "close_approve" and row["count"] for row in pending))
        self.assertEqual(self.post("close/approve", {"date": day}, self.admin_cookie)[0], 400)


if __name__ == "__main__":
    unittest.main()
