"""수거 개수 기준 배출표 사용 장수: 가상 자료와 임시 SQLCipher만 사용한다."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
from dgpc import core as C, numbered_labels as NL, privacy as PV, service as S, store


class LabelUsageTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(NL, "usage_page", None)), "수거 수량 기준 자동 집계가 필요합니다")
        self.directory = tempfile.TemporaryDirectory(prefix="dg-label-usage-")
        self.old_env = {k: os.environ.get(k) for k in ("DG_STATE", "DG_OUT", "DG_DB_KEY")}
        os.environ.update(DG_STATE=self.directory.name, DG_OUT=str(Path(self.directory.name) / "out"),
                          DG_DB_KEY="label-usage-fixture-not-production")
        self.con = store.connect(create=True)
        self.assertTrue(store.ENCRYPTED)
        self.actor = C.ACTOR.set("label_usage_fixture")
        self.import_seq = 0
        self.event_seq = 0
        self.fee = next(r["id"] for r in S.FEE_ROWS.values() if r["status"] == "CONFIRMED")

    def tearDown(self):
        if not hasattr(self, "con"):
            return
        C.ACTOR.reset(self.actor)
        self.con.close()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.directory.cleanup()

    def request(self, qty=3, dong="충장동"):
        r = S.create_request(self.con, {"name": "가상비공개주민", "phone": "01000009987", "address": "가상비공개주소",
                                      "dong": dong, "items": [{"fee_rate_id": self.fee, "qty": qty}]})
        r = S.get_request(self.con, r["id"])
        return r, r["items"][0]["line_id"]

    def events(self, r, events, start=None, applied="APPLIED", gen=None):
        self.import_seq += 1
        imp = f"usage-import-{self.import_seq}"
        self.con.execute("""INSERT INTO result_import(id,channel,device_id,package_sha256,imported_at)
            VALUES (?,'USB','usage-fixture',?,'2020-11-12T01:00:00Z')""", (imp, imp))
        for seq, (kind, body) in enumerate(events, self.event_seq + 1 if start is None else start):
            self.event_seq = max(self.event_seq, seq)
            self.con.execute("""INSERT INTO result_event
                (event_id,import_id,device_id,device_seq,request_id,assign_ver,kind,body_json,apply_status,device_ts,gen)
                VALUES (?,?, 'usage-fixture',?,?,1,?,?,?,'2026-11-12T01:00:00Z',?)""",
                (f"{imp}-{seq}", imp, seq, r["id"], kind, json.dumps(body), applied, gen))

    def result(self, r, line, qty, outcome="DONE", **kw):
        self.events(r, [("ITEM_RESULT", {"line_id": line, "qty_confirmed": r["items"][0]["qty"], "qty_collected": qty}),
                        ("ATTEMPT", {"outcome": outcome})], **kw)

    def page(self, **filters):
        return NL.usage_page(self.con, filters)

    def test_quantity_partial_and_zero_are_counted_in_sheets(self):
        for collected, outcome in ((3, "DONE"), (1, "PARTIAL"), (0, "NOT_COLLECTED")):
            r, line = self.request()
            self.result(r, line, collected, outcome)
        p = self.page()
        self.assertEqual(p["totals"], {"used": 4, "requests": 2})
        self.assertEqual(sorted(x["used"] for x in p["rows"]), [1, 3])
        self.assertIn("누적", p["source_note"])

    def test_unfinalized_edits_do_not_change_confirmed_usage(self):
        r, line = self.request()
        self.events(r, [("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 2})])
        self.assertEqual(self.page()["totals"]["used"], 0)
        self.events(r, [("ATTEMPT", {"outcome": "PARTIAL"})])
        self.assertEqual(self.page()["totals"]["used"], 2)
        self.events(r, [("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 3})])
        self.assertEqual(self.page()["totals"]["used"], 2)
        self.events(r, [("ATTEMPT", {"outcome": "DONE"})])
        self.assertEqual(self.page()["totals"]["used"], 3)

    def test_revisit_and_repeated_snapshots_are_not_added_twice(self):
        r, line = self.request()
        self.result(r, line, 1, "PARTIAL")
        self.result(r, line, 3)
        self.result(r, line, 3)
        p = self.page()
        self.assertEqual(p["totals"]["used"], 3)
        self.assertEqual(p["rows"][0]["lines"][0]["used"], 3)
        self.con.execute("UPDATE request SET status='ASSIGNED' WHERE id=?", (r["id"],))
        self.assertEqual(self.page()["totals"]["used"], 3)

    def test_correction_in_same_second_with_reset_sequence_is_latest(self):
        r, line = self.request()
        self.result(r, line, 3, start=100, gen="generation-before")
        self.result(r, line, 1, "PARTIAL", start=1, gen="generation-after")
        self.assertEqual(self.page()["totals"]["used"], 1)
        self.result(r, line, 0, "NOT_COLLECTED")
        self.assertEqual(self.page()["totals"]["used"], 0)

    def test_conflicts_and_rejections_do_not_count_until_applied(self):
        r, line = self.request()
        self.result(r, line, 1, "PARTIAL")
        self.result(r, line, 3, applied="CONFLICT")
        self.result(r, line, 2, applied="REJECTED")
        self.assertEqual(self.page()["totals"]["used"], 1)
        self.con.execute("UPDATE result_event SET apply_status='APPLIED' WHERE import_id='usage-import-2'")
        self.assertEqual(self.page()["totals"]["used"], 3)

    def test_collected_field_item_and_removed_base_item_remain_counted(self):
        r, line = self.request()
        self.events(r, [("FIELD_ITEM_ADD", {"line_id": "field-fixture", "name": "가상 추가 의자", "qty": 2, "unit_label": "개"}),
                        ("ITEM_RESULT", {"line_id": "field-fixture", "qty_confirmed": 2, "qty_collected": 2}),
                        ("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 3}),
                        ("ATTEMPT", {"outcome": "DONE"})])
        self.con.execute("UPDATE request_item SET removed=1 WHERE line_id=?", (line,))
        p = self.page()
        self.assertEqual(p["totals"]["used"], 5)
        self.assertEqual(len(p["rows"][0]["lines"]), 2)

    def test_late_old_event_keeps_new_device_or_generation_snapshot(self):
        for changed_device in (False, True):
            with self.subTest(changed_device=changed_device):
                r, line = self.request()
                self.events(r, [("ATTEMPT", {"outcome": "PARTIAL"})], start=20, gen="old")
                self.result(r, line, 2, "PARTIAL", start=1, gen="old" if changed_device else "new")
                if changed_device:
                    self.con.execute("UPDATE result_event SET device_id='other-fixture-phone' WHERE import_id=?", (f"usage-import-{self.import_seq}",))
                self.events(r, [("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 1})], start=19, gen="old")
                usage = next(x for x in self.page()["rows"] if x["id"] == r["id"])
                self.assertEqual(usage["used"], 2)

    def test_late_field_add_is_reconstructed_before_its_finalized_result(self):
        r, _ = self.request()
        self.events(r, [("ITEM_RESULT", {"line_id": "late-field", "qty_confirmed": 2, "qty_collected": 2}),
                        ("ATTEMPT", {"outcome": "DONE"})], start=2)
        self.assertEqual(self.page()["totals"]["used"], 0)
        self.events(r, [("FIELD_ITEM_ADD", {"line_id": "late-field", "name": "가상 복원 품목", "qty": 2})], start=1)
        self.assertEqual(self.page()["totals"]["used"], 2)

    def test_cancel_and_actual_privacy_purge_keep_historical_usage(self):
        r, line = self.request()
        self.result(r, line, 3)
        self.con.execute("UPDATE request SET status='CANCELLED',cancelled_at='2020-01-01T00:00:00Z' WHERE id=?", (r["id"],))
        self.assertEqual(self.page()["totals"]["used"], 3)
        self.assertEqual(PV.purge_pii(self.con)["count"], 1)
        p = self.page()
        self.assertEqual(p["totals"]["used"], 3)
        raw = json.dumps(p, ensure_ascii=False)
        for private in ("가상비공개주민", "01000009987", "가상비공개주소"):
            self.assertNotIn(private, raw)

    def test_bad_and_unknown_line_events_are_ignored_and_quantities_clamped(self):
        r, line = self.request()
        self.events(r, [("ITEM_RESULT", {"line_id": "unknown", "qty_confirmed": 3, "qty_collected": 3}),
                        ("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 99999}),
                        ("ATTEMPT", {"outcome": "DONE"})])
        self.assertEqual(self.page()["totals"]["used"], 3)
        self.events(r, [("ITEM_RESULT", []), ("ATTEMPT", {"outcome": "UNKNOWN"})])
        self.assertEqual(self.page()["totals"]["used"], 3)
        self.events(r, [("ATTEMPT", {"outcome": []})])
        self.assertEqual(self.page()["totals"]["used"], 3)

    def test_read_only_aggregation_does_not_consume_serials_or_modify_finance(self):
        r, line = self.request()
        NL.receive_range(self.con, "0001", "0003")
        NL.use_number(self.con, "0001", r["id"], line)
        self.result(r, line, 3)
        tables = ("numbered_label", "numbered_label_event", "payment", "audit_log", "request")
        before = {t: self.con.execute("SELECT * FROM " + t).fetchall() for t in tables}
        for _ in range(2):
            self.assertEqual(self.page()["totals"]["used"], 3)
        self.assertEqual(before, {t: self.con.execute("SELECT * FROM " + t).fetchall() for t in tables})
        self.assertEqual(NL.label_page(self.con, {})["counts"]["USED"], 1)

    def test_filters_paging_and_no_resident_fields(self):
        for n in range(52):
            r, line = self.request(dong="충장동" if n < 51 else "동명동")
            self.result(r, line, 1)
        p = self.page(page="2")
        self.assertEqual((p["total"], p["totals"]["used"], len(p["rows"])), (52, 52, 2))
        self.assertEqual(self.page(page="999")["page"], 2)
        self.assertEqual(self.page(dong="동명동")["totals"]["used"], 1)
        target = p["rows"][0]
        self.assertEqual(self.page(q=target["receipt_no"])["total"], 1)
        self.assertEqual(self.page(q="01000009987")["total"], 0)
        self.assertEqual(set(target), {"id", "receipt_no", "dong", "at", "used", "lines"})
        self.assertNotIn("가상비공개", json.dumps(p, ensure_ascii=False))

    def test_invalid_filters_fail_cleanly(self):
        for filters in ({"q": []}, {"q": "x" * 61}, {"dong": ["충장동"]}, {"dong": "x" * 101},
                        {"page": True}, {"page": "x"}, {"page": "1.1"}):
            with self.subTest(filters=filters), self.assertRaises(C.UserError):
                NL.usage_page(self.con, filters)


if __name__ == "__main__":
    unittest.main()
