"""도로망 포함 백업과 복원은 가상 SQLCipher 자료·도로 파일로만 검증한다."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
import desktop_road_rules as roads
from dgpc import backup, service, store


PASSWORD = "test-only-backup-pass"
ACTIVE = "a" * 32
PREVIOUS = "b" * 32


@contextmanager
def db_at(directory):
    with patch.dict(os.environ, DG_STATE=str(directory), DG_DB_KEY="test-only-encrypted-db-key"):
        yield


class RoadBackupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="dg-road-backup-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.pack = self.root / "osrm-pack"
        (self.pack / "data").mkdir(parents=True)
        (self.pack / "data/gwangju.osrm.properties").write_bytes(b"OSRM TEST")
        (self.pack / "data/gwangju.osrm.mldgr").write_bytes(b"base graph")
        self.fingerprint = roads.graph_fingerprint(self.pack)
        self.source, self.target = self.root / "source", self.root / "target"
        self.source_road = self.source / "road-rules"
        for generation in (ACTIVE, PREVIOUS):
            directory = self.source_road / generation
            directory.mkdir(parents=True)
            hashes = {}
            for suffix in ("properties", "partition", "cells", "cell_metrics", "mldgr", "fileIndex", "ramIndex"):
                content = f"{generation}/{suffix}".encode() * 100
                filename = f"trial.osrm.{suffix}"
                (directory / filename).write_bytes(content)
                hashes[filename] = hashlib.sha256(content).hexdigest()
            (directory / "rules.json").write_text(json.dumps({
                "schema": 1, "base_fingerprint": self.fingerprint, "graph_hashes": hashes,
                "rules": [{"road_id": "1", "direction": "forward", "name": "가상 길",
                           "checked_on": "2026-11-21", "evidence": "가상 표지"}],
            }), encoding="utf8")
        (self.source_road / "state.json").write_text(json.dumps({
            "schema": 1, "base_fingerprint": self.fingerprint, "active_id": ACTIVE, "previous_id": PREVIOUS,
        }), encoding="utf8")
        (self.source_road / ("c" * 32)).mkdir()
        (self.source_road / "road-rules.lock").touch()
        for loc, value in ((self.source, "from backup"), (self.target, "before restore")):
            with db_at(loc):
                con = store.connect(create=True)
                try:
                    con.execute("INSERT INTO meta VALUES ('test_marker', ?)", (value,))
                finally:
                    con.close()

    def make_manual(self, source=None):
        with db_at(source or self.source):
            con = store.connect()
            try:
                return store.backup_to(con, self.root / "test.dgbak", PASSWORD)
            finally:
                con.close()

    def assert_installed(self):
        restored = self.target / "road-rules"
        state = json.loads((restored / "state.json").read_bytes())
        self.assertEqual((state["active_id"], state["previous_id"]), (ACTIVE, PREVIOUS))
        self.assertFalse((restored / ("c" * 32)).exists())
        for generation in (ACTIVE, PREVIOUS):
            for src in (self.source_road / generation).iterdir():
                self.assertEqual(src.read_bytes(), (restored / generation / src.name).read_bytes())
        with db_at(self.target):
            con = store.connect()
            try:
                self.assertEqual(con.execute("SELECT value FROM meta WHERE key='test_marker'").fetchone()[0], "from backup")
                self.assertFalse(con.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'dg_backup_road_%'").fetchone())
                self.assertEqual(con.execute("PRAGMA freelist_count").fetchone()[0], 0)
            finally:
                con.close()

    def test_encrypted_manual_backup_restores_both_road_generations(self):
        path = self.make_manual()
        with db_at(self.target):
            result = store.restore_from(path, PASSWORD, osrm_pack=self.pack)
        self.assertEqual(result["road_state"]["active_id"], ACTIVE)
        self.assertTrue(Path(result["kept"]).exists())
        self.assert_installed()

    def test_encrypted_auto_backup_has_the_same_restore_contract(self):
        with db_at(self.source):
            con = store.connect()
            try:
                pem, _ = backup.generate_key(con, PASSWORD)
                result = backup.make_auto(con, "TEST")
                self.assertTrue(result["ok"], result)
                path = backup.backup_dir(con) / result["file"]
            finally:
                con.close()
        with db_at(self.target):
            backup.restore_auto(path, pem, PASSWORD, osrm_pack=self.pack)
        self.assert_installed()

    def test_wrong_pack_and_bad_archive_stop_before_substitution(self):
        path = self.make_manual()
        orig = (self.target / "pc.db").read_bytes()
        with db_at(self.target):
            with self.assertRaisesRegex(ValueError, "도로 팩"):
                store.restore_from(path, PASSWORD)
            with self.assertRaisesRegex(ValueError, "도로 팩"):
                store.restore_from(path, PASSWORD, osrm_pack=self.root / "other")
            con, _ = store.open_backup(path, PASSWORD)
            try:
                con.execute("UPDATE dg_backup_road_files SET bytes=x'ff' WHERE name=?", (f"{ACTIVE}/trial.osrm.mldgr",))
            finally:
                con.close()
            with self.assertRaisesRegex(ValueError, "도로"):
                store.restore_from(path, PASSWORD, osrm_pack=self.pack)
        self.assertEqual((self.target / "pc.db").read_bytes(), orig)
        self.assertFalse((self.target / "road-rules").exists())
        self.assertFalse(list(self.target.glob("pc.db.before-restore-*")))

    def test_legacy_archive_leaves_current_road_files_untouched(self):
        path = self.make_manual(self.target)
        before = (self.source_road / "state.json").read_bytes()
        with db_at(self.source):
            self.assertIsNone(store.restore_from(path, PASSWORD)["road_state"])
        self.assertEqual((self.source_road / "state.json").read_bytes(), before)

    def test_failed_database_swap_rolls_back_both_staged_parts(self):
        path = self.make_manual()
        old_road = self.target / "road-rules/state.json"
        old_road.parent.mkdir()
        old_road.write_text(json.dumps({"schema": 1, "base_fingerprint": self.fingerprint,
                                        "active_id": "base", "previous_id": None}), encoding="utf8")
        old_state = old_road.read_bytes()
        old_db = (self.target / "pc.db").read_bytes()
        rename = Path.rename

        def fail_last_move(source, dest):
            if source.name == "restored.db":
                raise OSError("가상 파일 교체 실패")
            return rename(source, dest)

        with db_at(self.target), patch.object(Path, "rename", fail_last_move):
            with self.assertRaisesRegex(ValueError, "설치하지"):
                store.restore_from(path, PASSWORD, osrm_pack=self.pack)
        self.assertEqual(old_road.read_bytes(), old_state)
        self.assertEqual((self.target / "pc.db").read_bytes(), old_db)
        self.assertFalse((self.target / store.ROAD_RESTORE_PENDING).exists())
        self.assertFalse(list(self.target.glob("pc.db.before-restore-*")))
        self.assertFalse((old_road.parent / ACTIVE).exists())

    def test_interrupted_restore_moves_orphan_sqlite_sidecars_before_retry(self):
        path = self.make_manual()
        (self.target / "pc.db").rename(self.target / "kept-prior-database")
        (self.target / store.ROAD_RESTORE_PENDING).touch()
        for ext in ("-wal", "-shm", "-journal"):
            (self.target / f"pc.db{ext}").write_bytes(b"old sidecar")
        with db_at(self.target):
            with self.assertRaisesRegex(SystemExit, "복원이 중단"):
                store.connect()
            store.restore_from(path, PASSWORD, osrm_pack=self.pack)
        self.assertFalse((self.target / store.ROAD_RESTORE_PENDING).exists())
        for ext in ("-wal", "-shm", "-journal"):
            self.assertFalse((self.target / f"pc.db{ext}").exists())
            backed_up, = self.target.glob(f"pc.db.before-restore-*{ext}")
            self.assertEqual(backed_up.read_bytes(), b"old sidecar")
        self.assert_installed()

    def test_retention_keeps_the_latest_group_and_removes_old_orphans(self):
        newest, older, orphan = (self.target / f"pc.db.before-restore-{stamp}" for stamp in
                                 ("20261121-010000", "20220101-010000", "20200101-010000"))
        for db in (newest, older, orphan):
            Path(str(db) + "-journal").touch()
            companion = Path(str(db) + ".road-rules")
            companion.mkdir()
            (companion / "state.json").touch()
        newest.touch()
        older.touch()
        self.assertEqual(store.restore_copy_retention(self.target, now=time.time()), [older.name, orphan.name])
        self.assertTrue(Path(str(newest) + ".road-rules").exists())
        self.assertTrue(Path(str(newest) + "-journal").exists())
        self.assertFalse(list(self.target.glob(f"{older.name}*")))
        self.assertFalse(list(self.target.glob(f"{orphan.name}*")))


if __name__ == "__main__":
    unittest.main()
