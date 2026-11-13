"""행정동 순서 저장: 실제 SQLCipher와 HTTP 권한·경합·이력·기존 설정 보존 회귀."""
from __future__ import annotations

import http.client
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import area, dong_work, service as S, users


class DongOrderTest(SessionHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.crew = S.save_crew(self.con, {"name": "가상 순서팀"})
        self.a = area.save_dong(self.con, {"name": "가상순서A동", "code": "A1", "weekdays": [0, 2], "crew_id": self.crew,
                                           "daily_limit": 12, "daily_limit_m3": 3.5, "keywords": ["가상 순서로"]})
        self.b = area.save_dong(self.con, {"name": "가상순서B동", "active": False})
        self.base = [d["id"] for d in area.list_dongs(self.con)]

    def order(self):
        return [d["id"] for d in area.list_dongs(self.con)]

    def save_order(self, ids, base=None, cookie=None):
        return self.post("dongs/order", {"ids": ids, "base_ids": self.base if base is None else base}, cookie or self.admin_cookie)

    def bootstrap(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", "/api/bootstrap", headers={"X-DG-CSRF": "1", "Cookie": self.admin_cookie})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())
        finally:
            connection.close()

    def snapshot(self):
        return {d["id"]: {key: value for key, value in d.items() if key not in {"sort", "updated_at"}}
                for d in area.list_dongs(self.con)}

    def test_admin_reorders_all_dongs_without_changing_settings_or_links(self):
        uid = S.create_user(self.con, "order_dong", "가상 동담당", "DONG", "order-password-123", dong_ids=[self.a])
        before = self.snapshot()
        wanted = list(reversed(self.base))
        status, result, _ = self.save_order(wanted)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.order(), wanted)
        self.assertEqual([d["id"] for d in result["dongs"]], wanted)
        self.assertEqual([d["sort"] for d in area.list_dongs(self.con)], list(range(len(wanted))))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(users.user_dong_ids(self.con, uid), [self.a])
        scoped = dong_work.actor(self.con, {"user_id": uid})
        self.assertEqual([d["id"] for d in dong_work.safe_dongs(self.con, scoped)], [self.a])

    def test_saved_order_is_used_by_bootstrap_and_one_audit_is_recorded(self):
        wanted = self.base[1:] + self.base[:1]
        before = self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        status, result, _ = self.save_order(wanted)
        self.assertEqual(status, 200, result)
        self.assertEqual([d["id"] for d in self.bootstrap()["dongs"]], wanted)
        audit = self.con.execute("SELECT entity,action,detail FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(audit[:2], ("dong", "REORDER"))
        self.assertEqual(json.loads(audit[2])["from"], self.base)
        self.assertEqual(json.loads(audit[2])["to"], wanted)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], before + 1)
        status, _, _ = self.save_order(wanted)
        self.assertEqual(status, 200, "응답을 잃은 같은 요청은 안전하게 다시 확인")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], before + 1)

    def test_malformed_missing_duplicate_and_unknown_ids_never_partially_reorder(self):
        before = self.order()
        for ids in (None, "invalid", [False], self.base[:-1], self.base[:-1] + [self.base[0]], self.base[:-1] + ["unknown"]):
            with self.subTest(ids=ids):
                status, _, _ = self.save_order(ids)
                self.assertIn(status, (400, 409))
                self.assertEqual(self.order(), before)

    def test_non_admin_roles_cannot_change_order(self):
        wanted = list(reversed(self.base))
        for role in ("INTAKE", "LEAD", "DONG", "FIELD"):
            username = "order_" + role.lower()
            values = {"dong_ids": [self.a]} if role == "DONG" else {}
            uid = S.create_user(self.con, username, "가상 권한계정", role, "order-password-123", **values)
            self.con.execute("UPDATE app_user SET must_change=0 WHERE id=?", (uid,))
            cookie = self.login(username, "order-password-123")
            self.assertEqual(self.save_order(wanted, cookie=cookie)[0], 403, role)
            self.assertEqual(self.order(), self.base)

    def test_stale_order_or_changed_membership_returns_conflict(self):
        first = self.base[1:] + self.base[:1]
        self.assertEqual(self.save_order(first)[0], 200)
        second = list(reversed(self.base))
        self.assertEqual(self.save_order(second)[0], 409)
        self.assertEqual(self.order(), first)
        area.save_dong(self.con, {"name": "가상 추가동"})
        self.assertEqual(self.save_order(second, base=first)[0], 409)
        self.assertEqual(self.order()[:-1], first)

    def test_regular_row_edit_preserves_current_order_and_new_dong_appends(self):
        wanted = list(reversed(self.base))
        self.assertEqual(self.save_order(wanted)[0], 200)
        data = area.get_dong(self.con, "가상순서A동")
        data.pop("sort")
        data["daily_limit"] = 99
        area.save_dong(self.con, data)
        self.assertEqual(self.order(), wanted)
        added = area.save_dong(self.con, {"name": "가상 맨끝동"})
        self.assertEqual(self.order(), wanted + [added])

    def test_same_order_does_not_write_an_audit(self):
        before = self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        status, result, _ = self.save_order(self.base)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
