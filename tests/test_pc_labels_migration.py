"""숫자 배출표 migration: 실제 SQLCipher 임시 DB의 이전·실패 rollback."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from dgpc import store


class LabelMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="dg-label-migration-")
        self.state = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {"DG_STATE": str(self.state), "DG_DB_KEY": "migration-fixture-only-123"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def old_database(self):
        migrations = [entry for entry in store.MIGRATIONS if entry[0] <= 6]
        with patch.object(store, "MIGRATIONS", migrations), patch.object(store, "LATEST", 6):
            con = store.connect(create=True)
            con.execute("INSERT INTO meta(key,value) VALUES ('label_migration_fixture','preserve')")
            con.close()

    def test_fresh_database(self):
        self.assertEqual(store.LATEST, 7)
        con = store.connect(create=True)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM numbered_label").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM numbered_label_event").fetchone()[0], 0)
            self.assertTrue(con.execute("PRAGMA foreign_keys").fetchone()[0])
        finally:
            con.close()

    def test_existing_database_preserved(self):
        self.old_database()
        con = store.connect()
        try:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='label_migration_fixture'").fetchone()[0], "preserve")
            self.assertEqual(con.execute("SELECT count(*) FROM numbered_label").fetchone()[0], 0)
        finally:
            con.close()
        self.assertTrue(list((self.state / "pre-migration").glob("*.db")))
        self.assertNotEqual((self.state / "pc.db").read_bytes()[:16], b"SQLite format 3\0")

    def test_failure_rolls_back(self):
        self.old_database()
        with patch.object(store, "_TEST_FAIL_STEP", 7):
            with self.assertRaises(RuntimeError):
                store.connect()
        con = store.connect(readonly=True)
        try:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='label_migration_fixture'").fetchone()[0], "preserve")
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='numbered_label'").fetchone())
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
