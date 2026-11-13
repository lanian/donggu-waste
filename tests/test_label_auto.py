"""동별 배출표 실물 번호 자동 배정: 임시 암호화 상태와 가상 주민만 사용."""
import importlib.util
import json
import unittest
from unittest.mock import patch

import test_label_usage as fixture
from dgpc import area, core as C, numbered_labels as NL, store


class LabelAutoTests(unittest.TestCase):
    request = fixture.LabelUsageTests.request
    def events(self, r, *args, **kwargs):
        fixture.LabelUsageTests.events(self, r, *args, **kwargs)
        self.con.execute("UPDATE result_import SET imported_at='2026-07-01T01:00:00Z' WHERE id=?", (f"usage-import-{self.import_seq}",))

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("dgpc.label_auto"), "동별 입고 번호 자동 배정이 필요합니다")
        from dgpc import label_auto
        self.A = label_auto
        fixture.LabelUsageTests.setUp(self)
        self.dongs = {d["name"]: d["id"] for d in area.list_dongs(self.con)}

    def tearDown(self):
        fixture.LabelUsageTests.tearDown(self)

    def stock(self, start="0001", end="0005", dong="충장동", day="2026-06-01"):
        return NL.receive_range(self.con, start, end, dong_id=self.dongs[dong], received_on=day)

    def collect(self, r, line, qty, day="2026-06-01", outcome=None):
        self.events(r, [("ITEM_RESULT", {"line_id": line, "qty_confirmed": r["items"][0]["qty"], "qty_collected": qty}),
                        ("ATTEMPT", {"outcome": outcome or ("DONE" if qty == r["items"][0]["qty"] else "PARTIAL")})])
        self.con.execute("UPDATE result_event SET device_ts=? WHERE import_id=?", (day + "T01:00:00Z", f"usage-import-{self.import_seq}"))
        return self.A.sync(self.con, [r["id"]])

    def active(self, r):
        return C.rows(self.con, "SELECT * FROM numbered_label_allocation WHERE request_id=? AND voided_at IS NULL ORDER BY line_id,unit_index", (r["id"],))

    def test_fresh_schema_and_dong_stock_isolation(self):
        self.assertEqual(self.con.execute("PRAGMA user_version").fetchone()[0], store.LATEST)
        self.stock("0001", "0003")
        self.stock("0010", "0012", "동명동")
        r, line = self.request()
        self.collect(r, line, 3)
        self.assertEqual([x["number_key"] for x in self.active(r)], ["1", "2", "3"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM numbered_label WHERE stock_dong_id=? AND status='DISTRIBUTED'", (self.dongs["동명동"],)).fetchone()[0], 3)
        self.assertEqual(NL.label_history(self.con, "1")["label"]["number"], "0001")

    def test_repeated_sync_has_no_extra_consumption_or_audit(self):
        self.stock()
        r, line = self.request()
        self.collect(r, line, 3)
        n = self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        for _ in range(2):
            self.A.sync(self.con, [r["id"]])
        self.assertEqual(len(self.active(r)), 3)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], n)

    def test_shortage_is_reported_and_new_stock_completes_pending(self):
        self.stock("0001", "0001")
        r, line = self.request()
        self.collect(r, line, 3)
        summary = self.A.summary(self.con, self.dongs["충장동"])
        self.assertEqual((summary["required"], summary["assigned"], summary["pending"]), (3, 1, 2))
        self.stock("0002", "0003")
        self.assertEqual(len(self.active(r)), 3)
        self.assertEqual(self.A.summary(self.con, self.dongs["충장동"])["pending"], 0)

    def test_revisit_assigns_only_increment_with_collection_days(self):
        self.stock()
        r, line = self.request()
        self.collect(r, line, 1)
        self.collect(r, line, 3, "2026-06-30")
        a = self.active(r)
        self.assertEqual([x["collection_at"][:10] for x in a], ["2026-06-01", "2026-06-30", "2026-06-30"])
        self.assertEqual([x["unit_index"] for x in a], [0, 1, 2])

    def test_future_stock_does_not_backfill_earlier_collection(self):
        self.stock(day="2026-06-15")
        r, line = self.request()
        self.collect(r, line, 1, "2026-06-01")
        self.assertEqual(self.active(r), [])
        self.collect(r, line, 2, "2026-06-20")
        self.assertEqual([x["unit_index"] for x in self.active(r)], [1])
        self.assertEqual(self.A.summary(self.con, self.dongs["충장동"])["pending"], 1)

    def test_field_item_without_request_item_fk_is_supported(self):
        self.stock()
        r, _ = self.request()
        self.events(r, [("FIELD_ITEM_ADD", {"line_id": "field-auto", "name": "가상 현장 품목", "qty": 2}),
                        ("ITEM_RESULT", {"line_id": "field-auto", "qty_confirmed": 2, "qty_collected": 2}),
                        ("ATTEMPT", {"outcome": "DONE"})])
        self.con.execute("UPDATE result_event SET device_ts='2026-06-01T01:00:00Z'")
        self.A.sync(self.con, [r["id"]])
        a = self.active(r)
        self.assertEqual([x["line_id"] for x in a], ["field-auto", "field-auto"])
        self.assertTrue(all(x[0] is None for x in self.con.execute("SELECT line_id FROM numbered_label WHERE status='USED'")))

    def test_quantity_correction_voids_auto_numbers_and_never_reuses_them(self):
        self.stock()
        r, line = self.request()
        self.collect(r, line, 3)
        self.collect(r, line, 1, "2026-06-20")
        self.assertEqual(len(self.active(r)), 1)
        self.assertEqual(NL.label_history(self.con, "2")["label"]["status"], "VOID")
        self.collect(r, line, 3, "2026-06-21")
        self.assertEqual([x["number_key"] for x in self.active(r)], ["1", "4", "5"])
        self.assertEqual(self.A.summary(self.con, self.dongs["충장동"])["pending"], 0)

    def test_manual_line_and_whole_request_numbers_count_once(self):
        self.stock("0001", "0006")
        for n, whole in ((1, False), (4, True)):
            r, line = self.request()
            NL.use_number(self.con, str(n), r["id"], None if whole else line)
            self.collect(r, line, 3)
            a = self.active(r)
            self.assertEqual(len(a), 3)
            self.assertEqual(sum(x["automatic"] == 0 for x in a), 1)
            self.assertEqual(a[0]["number_key"], str(n))
            if whole:
                self.assertIsNone(NL.label_history(self.con, str(n))["label"]["line_id"])

    def test_voided_allocated_number_is_replaced_from_same_dong(self):
        self.stock("0001", "0004")
        r, line = self.request(qty=2)
        self.collect(r, line, 2)
        NL.void_number(self.con, "1", "가상 훼손")
        self.assertEqual({x["number_key"] for x in self.active(r)}, {"2", "3"})
        self.assertEqual(NL.label_history(self.con, "1")["label"]["status"], "VOID")

    def test_scoped_manual_use_cannot_take_another_dongs_stock(self):
        self.stock(dong="동명동")
        r, line = self.request(dong="충장동")
        with self.assertRaises(C.UserError):
            NL.use_number(self.con, "1", r["id"], line)

    def test_legacy_scope_assignment_preserves_used_link_and_rejects_conflicts(self):
        NL.receive_range(self.con, "0001", "0003", received_on="2026-06-01")
        r, line = self.request()
        NL.use_number(self.con, "1", r["id"], line)
        NL.assign_stock_dong(self.con, "0001", "0003", self.dongs["충장동"], "2026-06-01")
        original = NL.label_history(self.con, "1")["label"]
        self.assertEqual((original["status"], original["request_id"], original["line_id"]), ("USED", r["id"], line))
        self.assertEqual(original["stock_dong_id"], self.dongs["충장동"])
        self.collect(r, line, 3)
        self.assertEqual(len(self.active(r)), 3)
        with self.assertRaises(C.UserError):
            NL.assign_stock_dong(self.con, "0001", "0003", self.dongs["동명동"], "2026-06-01")

    def test_allocation_failure_rolls_back_stock_and_links_together(self):
        self.stock()
        r, line = self.request()
        self.events(r, [("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 3}), ("ATTEMPT", {"outcome": "DONE"})])
        self.con.execute("UPDATE result_event SET device_ts='2026-06-01T01:00:00Z'")
        with patch.object(NL, "_record", side_effect=RuntimeError("fixture audit failure")), self.assertRaises(RuntimeError):
            self.A.sync(self.con, [r["id"]])
        self.assertEqual(self.active(r), [])
        self.assertEqual(NL.label_page(self.con, {})["counts"]["USED"], 0)

    def test_decrease_and_increase_in_one_import_does_not_reuse_retired_auto_units(self):
        self.stock()
        r, line = self.request()
        self.collect(r, line, 3)
        self.events(r, [("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 1}), ("ATTEMPT", {"outcome": "PARTIAL"}),
                        ("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 3}), ("ATTEMPT", {"outcome": "DONE"})])
        self.con.execute("UPDATE result_event SET device_ts='2026-06-21T01:00:00Z' WHERE import_id=?", (f"usage-import-{self.import_seq}",))
        result = self.A.sync(self.con, [r["id"]])
        self.assertEqual(result["voided"], 2)
        self.assertEqual([a["number_key"] for a in self.active(r)], ["1", "4", "5"])
        self.A.sync(self.con, [r["id"]])
        self.assertEqual([a["number_key"] for a in self.active(r)], ["1", "4", "5"])

    def test_distribution_before_received_day_is_rejected(self):
        NL.receive_range(self.con, "0001", "0003", received_on="2026-06-15")
        with self.assertRaises(C.UserError):
            NL.distribute_range(self.con, "0001", "0003", "충장동", distributed_on="2026-06-01")
        self.assertEqual(NL.label_page(self.con, {})["counts"]["AVAILABLE"], 3)

    def test_unscoped_existing_manual_number_reserves_unit_without_extra_auto_use(self):
        r, line = self.request(qty=1)
        NL.receive_range(self.con, "0901", "0901", received_on="2026-06-01")
        NL.use_number(self.con, "0901", r["id"], line)
        self.stock("0001", "0001")
        self.collect(r, line, 1)
        self.assertEqual(NL.label_page(self.con, {})["counts"]["USED"], 1)
        self.assertEqual(self.A.summary(self.con, self.dongs["충장동"])["pending"], 1)
        NL.assign_stock_dong(self.con, "0901", "0901", self.dongs["충장동"], "2026-06-01")
        self.assertEqual(self.A.summary(self.con, self.dongs["충장동"])["pending"], 0)
        self.assertEqual(NL.label_history(self.con, "0001")["label"]["status"], "DISTRIBUTED")

    def test_unmatched_manual_used_number_is_counted_as_excess(self):
        r, line = self.request(qty=1)
        self.stock("0901", "0903")
        NL.use_number(self.con, "0901", r["id"], line)
        NL.use_number(self.con, "0902", r["id"], line)
        self.collect(r, line, 1)
        summary = self.A.summary(self.con, self.dongs["충장동"])
        self.assertEqual((summary["required"], summary["assigned"], summary["pending"], summary["manual_excess"]), (1, 1, 0, 1))

    def test_scoped_sync_does_not_allocate_current_other_dong_after_request_moves(self):
        self.stock('0001', '0001')
        self.stock('0101', '0102', dong='동명동')
        r, line = self.request()
        self.collect(r, line, 3)
        from dgpc.intake import set_requests_dong
        set_requests_dong(self.con, [r['id']], '동명동', '가상 동 정정')
        result = self.A.sync(self.con, dong_id=self.dongs['충장동'])
        self.assertEqual((result['required'], result['new_assigned']), (0, 0))
        self.assertEqual(self.A.summary(self.con, self.dongs['충장동'])['required'], 0)
        self.assertEqual(NL.label_history(self.con, '0101')['label']['status'], 'DISTRIBUTED')
        next_result = self.A.sync(self.con, dong_id=self.dongs['동명동'])
        self.assertEqual((next_result['new_assigned'], next_result['pending'], next_result['conflicts']), (2, 1, 1))


if __name__ == "__main__":
    unittest.main()
