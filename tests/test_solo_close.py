"""마감마다 팀장 확인 여부를 보존하고 1인 마감도 기록 후 자동 백업한다."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_pc_labels as fixture
from dgpc import core as C, ops, service as S, store


class SoloCloseTests(unittest.TestCase):
    setUp = fixture.NumberedLabelsTest.setUp
    tearDown = fixture.NumberedLabelsTest.tearDown
    day = "2020-01-06"
    next_day = "2020-01-07"

    def setting(self, required):
        S.save_settings(self.con, {"close_approval_required": required})
        self.assertIs(S.settings(self.con).get("close_approval_required"), required)

    def pending(self):
        return next(i for i in ops.pending(self.con)["items"] if i["key"] == "close_approve")

    def test_default_and_legacy_remain_two_person_rounds(self):
        self.assertIs(S.settings(self.con).get("close_approval_required"), True)
        with self.assertRaises(C.UserError):
            S.save_settings(self.con, {"close_approval_required": "false"})
        with patch.dict(os.environ, DG_STATE=str(Path(self.directory.name) / "v16")):
            with patch.object(store, "LATEST", 16), patch.object(store, "MIGRATIONS", [m for m in store.MIGRATIONS if m[0] <= 16]):
                old = store.connect(create=True)
                try:
                    old.execute("""INSERT INTO day_close_log(id, work_date, seq, closed_at, closed_by, summary_json)
                        VALUES ('legacy-close', ?, 1, ?, ?, '{}')""", (self.day, C.now(), C.ACTOR.get()))
                finally:
                    old.close()
            migrated = store.connect()
            try:
                self.assertIs(S.close_info(migrated, self.day)["approval_required"], True)
                self.assertIs(S.day_summary(migrated, self.day)["closed"]["approval_required"], True)
                with self.assertRaisesRegex(C.UserError, "기록한 사람"):
                    S.approve_close(migrated, self.day)
            finally:
                migrated.close()

    def test_switch_only_affects_new_rounds_and_pending_visibility(self):
        S.close_day(self.con, self.day)
        self.setting(False)
        self.assertEqual(self.pending()["count"], 0)
        before = S.day_summary(self.con, self.day)
        self.assertIs(before["close_approval_required"], False)
        self.assertIs(before["closed"]["approval_required"], True)
        with self.assertRaises(C.UserError):
            S.approve_close(self.con, self.day)
        res = S.close_day(self.con, self.next_day, "가상 단독 마감")
        self.assertFalse(res["backup"]["ok"])
        self.assertIn("백업 키", res["backup"]["error"])
        self.assertEqual(S.close_info(self.con, self.next_day)["status"], "RECORDED")
        self.assertIsNone(S.close_info(self.con, self.next_day)["approved_by"])
        self.setting(True)
        pending = self.pending()
        self.assertEqual((pending["count"], pending["oldest"]), (1, self.day))
        token = C.ACTOR.set("가상검토자")
        try:
            with self.assertRaisesRegex(C.UserError, "1인|단독|팀장 확인"):
                S.approve_close(self.con, self.next_day)
            S.approve_close(self.con, self.day)
        finally:
            C.ACTOR.reset(token)
        self.assertEqual(self.pending()["count"], 0)
        S.close_day(self.con, self.next_day)
        self.assertEqual([c["approval_required"] for c in S.day_summary(self.con, self.next_day)["closes"]],
                         [False, True])
        self.assertEqual(self.pending()["count"], 1)

    def test_same_solo_request_after_settings_change_reuses_backup_and_round(self):
        self.setting(False)
        results = []
        def completed(con, reason, *, on_record):
            self.assertFalse(con.in_transaction, "암호화 백업 전에 마감 회차가 커밋되어야 한다")
            results.append(reason)
            result = {"ok": True, "file": "가상-backup.dgabk"}
            with C.tx(con):
                on_record(result)
            return result
        with patch("dgpc.close._bk.make_auto", side_effect=completed):
            first = S.close_day(self.con, self.day, client_req_id="solo-close-fixture")
            self.setting(True)
            S.close_day(self.con, self.day)
            retried = S.close_day(self.con, self.day, client_req_id="solo-close-fixture")
        self.assertEqual(first, retried)
        self.assertEqual(first["backup"], {"ok": True, "file": "가상-backup.dgabk"})
        self.assertEqual(results, ["CLOSE_SOLO"])
        self.assertEqual(S.close_info(self.con, self.day)["seq"], 2)

    def test_interrupted_publication_is_reported_without_a_second_backup(self):
        self.setting(False)
        key = "interrupted-after-export"
        with patch("dgpc.close._bk.make_auto", side_effect=RuntimeError("중단 재현")) as export:
            with self.assertRaisesRegex(RuntimeError, "중단 재현"):
                S.close_day(self.con, self.day, client_req_id=key)
            replay = S.close_day(self.con, self.day, client_req_id=key)
        self.assertEqual(export.call_count, 1)
        self.assertFalse(replay["backup"]["ok"])
        self.assertIn("백업", replay["backup"]["error"])
        self.assertEqual(S.close_info(self.con, self.day)["seq"], 1)


if __name__ == "__main__":
    unittest.main()
