"""공개 기준 목록의 자동 등록·기존 설정 보존. 임시 SQLCipher DB와 가상 주민 자료만 사용한다."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from dgpc import area, service as S, store  # noqa: E402

# 출처는 제품 기준 자료이며 주민/수거팀/접수 fixture는 모두 가상이다.
EXPECTED = ("충장동", "동명동", "계림1동", "계림2동", "산수1동", "산수2동", "지산1동", "지산2동",
            "서남동", "학동", "학운동", "지원1동", "지원2동")


class DefaultDongsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dg-default-dongs-")
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "state"
        self.environment = patch.dict(os.environ, DG_STATE=str(self.state), DG_OUT=str(Path(self.directory.name) / "out"),
                                      DG_DB_KEY="default-dongs-test-only-not-production",
                                      PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.assertTrue(store.ENCRYPTED, "이 회귀는 실제 SQLCipher가 필요함")

    def connect(self, *, legacy=False, **kwargs):
        if legacy:
            with patch.object(store, "LATEST", 8), patch.object(store, "MIGRATIONS", [m for m in store.MIGRATIONS if m[0] <= 8]):
                con = store.connect(**kwargs)
        else:
            con = store.connect(**kwargs)
        self.addCleanup(con.close)
        return con

    @staticmethod
    def snapshot(con, table):
        return con.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()

    def test_new_database_contains_all_official_dongs_with_unassigned_operating_settings(self):
        con = self.connect(create=True)
        dongs = area.list_dongs(con)
        self.assertEqual(tuple(d["name"] for d in dongs), EXPECTED)
        self.assertEqual(len({d["id"] for d in dongs}), 13)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], store.LATEST)
        for dong in dongs:
            self.assertTrue(dong["active"])
            for key in ("crew_id", "daily_limit", "daily_limit_m3", "code"):
                self.assertIsNone(dong[key], key)
            self.assertEqual(dong["weekdays"], [])
            self.assertEqual(dong["keywords"], [])
        self.assertNotEqual((self.state / "pc.db").read_bytes()[:16], b"SQLite format 3\0")

    def test_v8_upgrade_preserves_existing_dongs_business_rows_and_user_links(self):
        con = self.connect(create=True, legacy=True)
        S.ensure_pc_keys(con)
        crew = S.save_crew(con, {"name": "가상 기본팀"})
        existing = area.save_dong(con, {"name": EXPECTED[0], "active": False, "crew_id": crew, "weekdays": [1, 3],
                                       "daily_limit": 7, "daily_limit_m3": 2.5, "keywords": ["가상 보존로"], "code": "X7", "sort": 99})
        area.save_dong(con, {"name": "가상 전용동", "sort": -10})
        S.create_user(con, "default_fixture", "가상 동 담당자", "DONG", "default-test-password", dong_ids=[existing])
        fee = next(row["id"] for row in S.FEE_TABLE["rows"] if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)
        S.create_request(con, {"name": "가상 보존 주민", "phone": "01000000000", "address": "가상구 시험로 99999", "dong": EXPECTED[0],
                               "items": [{"fee_rate_id": fee, "qty": 1}]})
        original = self.snapshot(con, "dong")
        tables = ("crew", "request", "request_item", "app_user", "app_user_dong", "pc_key", "audit_log", "meta")
        before = {table: self.snapshot(con, table) for table in tables}
        con.close()
        upgraded = self.connect()
        self.assertEqual(len(area.list_dongs(upgraded)), 14)
        for row in original:
                self.assertEqual(upgraded.execute("SELECT * FROM dong WHERE id=?", (row[0],)).fetchone()[:len(row)], row)
        for table, rows in before.items():
            self.assertEqual(self.snapshot(upgraded, table), rows, table)
        self.assertEqual(upgraded.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_whitespace_variant_is_not_duplicated_or_renamed(self):
        con = self.connect(create=True, legacy=True)
        did = area.save_dong(con, {"name": "계림 1동", "active": False})
        before = con.execute("SELECT * FROM dong WHERE id=?", (did,)).fetchone()
        con.close()
        upgraded = self.connect()
        self.assertEqual(len(area.list_dongs(upgraded)), 13)
        self.assertIsNone(area.get_dong(upgraded, "계림1동"))
        self.assertEqual(upgraded.execute("SELECT * FROM dong WHERE id=?", (did,)).fetchone()[:len(before)], before)

    def test_reopening_current_database_does_not_rewrite_rows(self):
        con = self.connect(create=True)
        self.assertEqual(len(area.list_dongs(con)), 13)
        before = self.snapshot(con, "dong")
        con.close()
        reopened = self.connect()
        self.assertEqual(self.snapshot(reopened, "dong"), before)
        self.assertEqual(reopened.total_changes, 0)
        self.assertEqual(store.run_migrations(reopened), [])

    def test_user_deleted_default_dong_stays_deleted_after_restart(self):
        con = self.connect(create=True)
        dong = area.get_dong(con, EXPECTED[0])
        self.assertIsNotNone(dong, "삭제 유지 검증 전에 기본 동이 등록되어야 함")
        area.delete_dong(con, dong["id"])
        con.close()
        reopened = self.connect()
        self.assertIsNone(area.get_dong(reopened, EXPECTED[0]))
        self.assertEqual(len(area.list_dongs(reopened)), 12)

    def test_readonly_v8_connection_does_not_seed_or_advance_version(self):
        con = self.connect(create=True, legacy=True)
        con.close()
        readonly = self.connect(readonly=True)
        self.assertEqual(readonly.execute("SELECT COUNT(*) FROM dong").fetchone()[0], 0)
        self.assertEqual(readonly.execute("PRAGMA user_version").fetchone()[0], 8)
        readonly.close()
        upgraded = self.connect()
        self.assertEqual(len(area.list_dongs(upgraded)), 13)

    def test_failed_seed_rolls_back_all_added_rows_and_can_retry(self):
        con = self.connect(create=True, legacy=True)
        area.save_dong(con, {"name": "가상 보존동"})
        before = self.snapshot(con, "dong")
        with patch.object(store, "_TEST_FAIL_STEP", 9), self.assertRaises(RuntimeError):
            store.run_migrations(con)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 8)
        self.assertEqual(self.snapshot(con, "dong"), before)
        self.assertEqual(store.run_migrations(con), list(range(9, store.LATEST + 1)))
        self.assertEqual(len(area.list_dongs(con)), 14)

    def test_upgrade_creates_encrypted_pre_migration_copy(self):
        con = self.connect(create=True, legacy=True)
        area.save_dong(con, {"name": "가상 사본동"})
        con.close()
        upgraded = self.connect()
        self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], store.LATEST)
        copies = list((self.state / "pre-migration").glob("*.db"))
        self.assertEqual(len(copies), 1)
        self.assertNotEqual(copies[0].read_bytes()[:16], b"SQLite format 3\0")

    def test_old_program_is_rejected_after_default_dongs_upgrade(self):
        con = self.connect(create=True)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], store.LATEST)
        with patch.object(store, "LATEST", 8), self.assertRaises(SystemExit):
            store.run_migrations(con)

    def test_default_name_matching_works_without_guessing_shared_legal_dong(self):
        con = self.connect(create=True)
        self.assertEqual(area.detect_dong(con, "가상구 충장동 시험로 99999")["dong"], EXPECTED[0])
        self.assertIsNone(area.detect_dong(con, "가상구 계림동 시험로 99999")["dong"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
