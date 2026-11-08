"""숫자 배출표 원장 회귀: 가상 자료·임시 SQLCipher DB만 사용한다.

실행: .venv/Scripts/python.exe tests/test_pc_labels.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from dgpc import core as C, privacy as PV, service as S, store  # noqa: E402


class NumberedLabelsTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("dgpc.numbered_labels"), "숫자 배출표 원장 모듈이 필요합니다")
        from dgpc import numbered_labels
        self.L = numbered_labels
        self.directory = tempfile.TemporaryDirectory(prefix="dg-numbered-labels-")
        self.old_env = {key: os.environ.get(key) for key in ("DG_STATE", "DG_OUT", "DG_DB_KEY")}
        os.environ.update(DG_STATE=self.directory.name, DG_OUT=str(Path(self.directory.name) / "out"),
                          DG_DB_KEY="numbered-label-fixture-key-not-production")
        self.con = store.connect(create=True)
        self.assertTrue(store.ENCRYPTED, "SQLCipher 환경에서 검증해야 합니다")
        self.token = C.ACTOR.set("label_fixture")

    def tearDown(self):
        if not hasattr(self, "con"):
            return
        C.ACTOR.reset(self.token)
        self.con.close()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.directory.cleanup()

    def request(self, name="가상주민", **extra):
        fee = next(row for row in S.FEE_ROWS.values() if row["status"] == "CONFIRMED")
        result = S.create_request(self.con, {"name": name, "phone": "01000005555", "address": "가상시 동구 예시로 1",
                                             "items": [{"fee_rate_id": fee["id"], "qty": 1}], **extra})
        return S.get_request(self.con, result["id"])

    def total(self, table="numbered_label"):
        return self.con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def label(self, number):
        return self.L.label_history(self.con, number)["label"]

    def test_receive_preserves_zeroes_and_canonical_alias_blocks_duplicates(self):
        result = self.L.receive_range(self.con, "0001", "0003")
        self.assertEqual(result, {"count": 3, "numbers": ["0001", "0002", "0003"]})
        self.assertEqual(self.label("1")["number"], "0001")
        with self.assertRaises(C.UserError):
            self.L.receive_range(self.con, "1", "3")
        self.assertEqual(self.total(), 3)
        self.assertEqual(self.total("numbered_label_event"), 3)

    def test_number_requires_ascii_json_string(self):
        invalid = (0, 1, 1.0, True, None, "", " 1", "1 ", "+1", "-1", "1.0", "1e3", "１２", "١", "1\n", "a", "0" * 31)
        for number in invalid:
            with self.subTest(number=number), self.assertRaises(C.UserError):
                self.L.receive_range(self.con, number, number)
        self.assertEqual(self.total(), 0)
        self.L.receive_range(self.con, "0", "0")
        self.assertEqual(self.label("000")["number"], "0")
        maximum = "9" * 30
        self.L.receive_range(self.con, maximum, maximum)
        self.assertEqual(self.label(maximum)["number"], maximum)

    def test_ranges_are_bounded_and_atomic_on_overlap(self):
        for start, end in (("1", "02"), ("002", "001"), ("00000", "05000"), ("9" * 30, "1" + "0" * 30)):
            with self.subTest(start=start, end=end), self.assertRaises(C.UserError):
                self.L.receive_range(self.con, start, end)
        self.L.receive_range(self.con, "001", "005")
        with self.assertRaises(C.UserError):
            self.L.receive_range(self.con, "004", "008")
        self.assertEqual(self.total(), 5)
        self.assertEqual(self.total("numbered_label_event"), 5)

    def test_largest_allowed_range_has_exactly_5000_numbers(self):
        result = self.L.receive_range(self.con, "00000", "04999")
        self.assertEqual(result["count"], 5000)
        self.assertEqual(self.total(), 5000)
        self.assertEqual(result["numbers"][-1], "04999")

    def test_distribution_requires_destination_and_all_available_numbers(self):
        self.L.receive_range(self.con, "0100", "0103")
        for destination in ("", "  ", 1, "가" * 101):
            with self.subTest(destination=destination), self.assertRaises(C.UserError):
                self.L.distribute_range(self.con, "0100", "0101", destination)
        self.assertEqual(self.L.distribute_range(self.con, "0100", "0101", " 가상1동 주민센터 "), {"count": 2})
        self.assertEqual(self.label("100")["destination"], "가상1동 주민센터")
        self.assertEqual(self.label("100")["status"], "DISTRIBUTED")
        for start, end in (("0101", "0103"), ("0102", "0104")):
            with self.subTest(start=start), self.assertRaises(C.UserError):
                self.L.distribute_range(self.con, start, end, "가상2동 주민센터")
        self.assertEqual(self.label("102")["status"], "AVAILABLE")
        self.assertEqual(self.total("numbered_label_event"), 6)

    def test_use_accepts_valid_request_and_line_and_replay_is_unchanged(self):
        request = self.request()
        line = request["items"][0]["line_id"]
        self.L.receive_range(self.con, "0007", "0008")
        self.L.distribute_range(self.con, "0007", "0007", "가상기관")
        result = self.L.use_number(self.con, "7", request["id"], line)
        self.assertFalse(result["unchanged"])
        self.assertEqual(result["label"]["status"], "USED")
        self.assertEqual(result["label"]["number"], "0007")
        before = self.total("numbered_label_event")
        repeated = self.L.use_number(self.con, "0007", request["id"], line)
        self.assertTrue(repeated["unchanged"])
        self.assertEqual(self.total("numbered_label_event"), before)
        with self.assertRaises(C.UserError):
            self.L.use_number(self.con, "7", request["id"])
        self.assertFalse(self.L.use_number(self.con, "8", request["id"])["unchanged"])

    def test_use_rejects_missing_cancelled_purged_removed_and_foreign_targets(self):
        valid = self.request()
        other = self.request("가상다른주민")
        cancelled = self.request("가상취소주민")
        S.cancel_request(self.con, cancelled["id"], code="RESIDENT")
        purged = self.request("가상파기주민")
        with C.tx(self.con):
            PV.pseudonymize(self.con, purged["id"], C.now())
            self.con.execute("UPDATE request_item SET removed=1 WHERE line_id=?", (valid["items"][0]["line_id"],))
        self.L.receive_range(self.con, "42", "42")
        invalid = [("missing-request", None), (cancelled["id"], None), (purged["id"], None),
                   (valid["id"], "missing-line"), (valid["id"], valid["items"][0]["line_id"]),
                   (valid["id"], other["items"][0]["line_id"])]
        for request_id, line_id in invalid:
            with self.subTest(request=request_id, line=line_id), self.assertRaises(C.UserError):
                self.L.use_number(self.con, "42", request_id, line_id)
        self.assertEqual(self.label("42")["status"], "AVAILABLE")
        self.assertEqual(self.total("numbered_label_event"), 1)

    def test_used_number_cannot_move_to_another_request(self):
        first, second = self.request(), self.request("가상둘째주민")
        self.L.receive_range(self.con, "52", "52")
        self.L.use_number(self.con, "52", first["id"])
        with self.assertRaises(C.UserError):
            self.L.use_number(self.con, "52", second["id"])
        self.assertEqual(self.label("52")["request_id"], first["id"])

    def test_void_is_terminal_and_reason_uses_purgeable_audit_note(self):
        request = self.request("가상개인정보주민")
        self.L.receive_range(self.con, "0061", "0061")
        self.L.use_number(self.con, "61", request["id"])
        with self.assertRaises(C.UserError):
            self.L.void_number(self.con, "61", " ")
        reason = "가상개인정보주민이 잘못 붙여 정정"
        self.L.void_number(self.con, "61", reason)
        history = self.L.label_history(self.con, "61")
        self.assertEqual(history["label"]["status"], "VOID")
        self.assertEqual(history["rows"][0]["reason"], reason)
        # 주민 필드·사유를 원장과 이벤트에 복제하지 않는다.
        raw = json.dumps(C.rows(self.con, "SELECT * FROM numbered_label") + C.rows(self.con, "SELECT * FROM numbered_label_event"), ensure_ascii=False)
        self.assertNotIn("가상개인정보주민", raw)
        audit = C.one(self.con, "SELECT detail FROM audit_log WHERE id=?", (history["rows"][0]["audit_id"],))
        self.assertNotIn(reason, audit["detail"])
        self.assertEqual(json.loads(audit["detail"])["request"], request["id"])
        with C.tx(self.con):
            PV.pseudonymize(self.con, request["id"], C.now())
        self.assertEqual(self.L.label_history(self.con, "61")["rows"][0]["reason"], "(파기)")
        for action in (lambda: self.L.use_number(self.con, "61", request["id"]),
                       lambda: self.L.void_number(self.con, "61", "다시"),
                       lambda: self.L.distribute_range(self.con, "61", "61", "가상기관")):
            with self.assertRaises(C.UserError):
                action()

    def test_history_is_latest_twenty_and_does_not_expose_request_pii(self):
        self.L.receive_range(self.con, "80", "80")
        with C.tx(self.con):
            for _ in range(25):
                self.con.execute("INSERT INTO numbered_label_event (number_key,action,from_status,to_status,at,actor) VALUES ('80','RECEIVE',NULL,'AVAILABLE',?,?)",
                                 (C.now(), "fixture-history"))
        history = self.L.label_history(self.con, "00080")
        self.assertEqual(len(history["rows"]), 20)
        self.assertEqual([r["id"] for r in history["rows"]], sorted((r["id"] for r in history["rows"]), reverse=True))
        self.assertFalse({"name", "phone", "address", "memo"} & set(history["label"]))
        with self.assertRaises(C.UserError):
            self.L.label_history(self.con, "999")

    def test_list_filters_pages_numeric_sort_and_global_counts(self):
        self.L.receive_range(self.con, "0000", "0054")
        self.L.receive_range(self.con, "100", "100")
        self.L.distribute_range(self.con, "0000", "0004", "가상1동")
        request = self.request()
        self.L.use_number(self.con, "1", request["id"])
        self.L.void_number(self.con, "2", "인쇄 훼손")
        page = self.L.label_page(self.con)
        self.assertEqual((page["total"], page["size"], page["pages"], len(page["rows"])), (56, 50, 2, 50))
        self.assertEqual(page["rows"][0]["number"], "0000")
        self.assertEqual(page["counts"], {"ALL": 56, "AVAILABLE": 51, "DISTRIBUTED": 3, "USED": 1, "VOID": 1})
        last = self.L.label_page(self.con, {"page": 99})
        self.assertEqual((last["page"], len(last["rows"]), last["rows"][-1]["number"]), (2, 6, "100"))
        filtered = self.L.label_page(self.con, {"status": "DISTRIBUTED", "destination": "1동", "q": "00"})
        self.assertEqual(filtered["total"], 3)
        self.assertEqual(filtered["counts"], page["counts"])
        for filters in ({"status": "oops"}, {"q": "%"}, {"q": 0}, {"q": False}, {"page": "x"}):
            with self.subTest(filters=filters), self.assertRaises(C.UserError):
                self.L.label_page(self.con, filters)

    def test_readonly_allows_reads_and_rejects_all_mutations_as_user_error(self):
        self.L.receive_range(self.con, "91", "91")
        request = self.request()
        readonly = store.connect(readonly=True)
        try:
            before = self.total("audit_log")
            self.assertEqual(self.L.label_page(readonly)["total"], 1)
            self.assertEqual(self.L.label_history(readonly, "91")["label"]["number"], "91")
            for action in (lambda: self.L.receive_range(readonly, "92", "92"),
                           lambda: self.L.distribute_range(readonly, "91", "91", "가상기관"),
                           lambda: self.L.use_number(readonly, "91", request["id"]),
                           lambda: self.L.void_number(readonly, "91", "훼손")):
                with self.assertRaises(C.UserError):
                    action()
            self.assertEqual(self.total("audit_log"), before)
        finally:
            readonly.close()

    def test_concurrent_alias_receives_have_one_winner(self):
        connections = [store.connect(), store.connect()]
        barrier, results = threading.Barrier(2), queue.Queue()

        def receive(con, number):
            try:
                barrier.wait(timeout=5)
                self.L.receive_range(con, number, number)
                results.put("ok")
            except Exception as error:
                results.put(error)

        threads = [threading.Thread(target=receive, args=(con, number)) for con, number in zip(connections, ("00099", "99"))]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            outcomes = [results.get_nowait(), results.get_nowait()]
            self.assertEqual(outcomes.count("ok"), 1, outcomes)
            self.assertTrue(any(isinstance(result, C.UserError) for result in outcomes), outcomes)
            self.assertEqual(self.total(), 1)
            self.assertEqual(self.total("numbered_label_event"), 1)
        finally:
            for con in connections:
                con.close()

    def test_event_failure_rolls_back_numbers_and_audit_together(self):
        self.con.execute("""CREATE TEMP TRIGGER fixture_fail_label BEFORE INSERT ON numbered_label_event
            WHEN NEW.number_key='3' BEGIN SELECT RAISE(ABORT,'fixture event failure'); END""")
        before = self.total("audit_log")
        with self.assertRaisesRegex(store.dbapi.IntegrityError, "fixture event failure"):
            self.L.receive_range(self.con, "1", "4")
        self.assertEqual(self.total(), 0)
        self.assertEqual(self.total("numbered_label_event"), 0)
        self.assertEqual(self.total("audit_log"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
