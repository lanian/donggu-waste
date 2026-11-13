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
        self.assertGreaterEqual(store.LATEST, 10)
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
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], store.LATEST)
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

    def v9_database(self):
        with patch.object(store, 'MIGRATIONS', [m for m in store.MIGRATIONS if m[0] <= 9]), patch.object(store, 'LATEST', 9):
            con = store.connect(create=True)
            for key, destination in [('1', '충장동'), ('2', '가상 기관')]:
                con.execute("INSERT INTO numbered_label(number_key,number,status,destination,received_at,distributed_at,updated_at) VALUES(?,?,'DISTRIBUTED',?,'2026-06-01T00:00:00Z','2026-06-02T00:00:00Z','2026-06-02T00:00:00Z')", (key, '000' + key, destination))
                con.execute("INSERT INTO numbered_label_event(number_key,action,to_status,at,actor) VALUES(?,'DISTRIBUTE','DISTRIBUTED','2026-06-02T00:00:00Z','fixture')", (key,))
            self.original_labels = con.execute('SELECT * FROM numbered_label ORDER BY number_key').fetchall()
            self.original_events = con.execute('SELECT * FROM numbered_label_event ORDER BY id').fetchall()
            con.close()

    def test_v9_stock_and_history_preserved_with_only_exact_dong_mapped(self):
        self.v9_database()
        con = store.connect()
        try:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], store.LATEST)
            current = con.execute('SELECT * FROM numbered_label ORDER BY number_key').fetchall()
            self.assertEqual([r[:-1] for r in current], self.original_labels)
            self.assertEqual(current[0][-1], con.execute("SELECT id FROM dong WHERE name='충장동'").fetchone()[0])
            self.assertIsNone(current[1][-1])
            self.assertEqual(con.execute('SELECT * FROM numbered_label_event ORDER BY id').fetchall(), self.original_events)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM numbered_label_allocation').fetchone()[0], 0)
        finally:
            con.close()

    def test_v10_failure_restores_v9_stock_schema_and_history(self):
        self.v9_database()
        with patch.object(store, '_TEST_FAIL_STEP', 10), self.assertRaises(RuntimeError):
            store.connect()
        con = store.connect(readonly=True)
        try:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 9)
            self.assertEqual(con.execute('SELECT * FROM numbered_label ORDER BY number_key').fetchall(), self.original_labels)
            self.assertEqual(con.execute('SELECT * FROM numbered_label_event ORDER BY id').fetchall(), self.original_events)
            self.assertNotIn('stock_dong_id', [r[1] for r in con.execute('PRAGMA table_info(numbered_label)')])
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='numbered_label_allocation'").fetchone())
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
