"""확정 수거 누적값을 실제 수거일별 증가분으로 재구성한다."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
import datetime as dt
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))


class CollectionTraceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("dgpc.collection"), "실제 수거일별 수량 재구성이 필요합니다")
        from dgpc import collection
        self.C = collection
        self.items = [{"line_id": "chair", "name": "가상 의자", "spec": "고정식", "unit": "EA", "unit_label": "개",
                       "unit_price": 2000, "price_status": "CONFIRMED", "fee_rate_id": "fixture-chair", "qty": 3}]
        self.events = []

    def add(self, seq, kind, body, day="2026-06-01", device="phone-a", gen="g-a", stamp=None):
        self.events.append({"event_id": f"{device}-{gen}-{seq}", "device_id": device, "gen": gen, "device_seq": seq,
                            "kind": kind, "body_json": json.dumps(body), "device_ts": stamp or day + "T01:00:00Z",
                            "imported_at": "2026-07-01T01:00:00Z"})

    def result(self, seq, qty, day="2026-06-01", **kw):
        self.add(seq, "ITEM_RESULT", {"line_id": "chair", "qty_confirmed": 3, "qty_collected": qty}, day, **kw)
        self.add(seq + 1, "ATTEMPT", {"outcome": "DONE" if qty == 3 else "PARTIAL"}, day, **kw)

    def trace(self):
        return self.C.trace_pure(self.items, self.events)

    def test_partial_revisit_is_two_then_one_on_actual_days(self):
        self.result(1, 2)
        self.result(3, 3, "2026-06-30")
        t = self.trace()
        self.assertEqual(t["total"], 3)
        self.assertEqual([(s["date"], s["qty"], s["start"], s["end"]) for s in t["segments"]],
                         [("2026-06-01", 2, 0, 2), ("2026-06-30", 1, 2, 3)])

    def test_unfinalized_change_and_repeated_final_are_not_new_usage(self):
        self.result(1, 1)
        self.add(3, "ITEM_RESULT", {"line_id": "chair", "qty_confirmed": 3, "qty_collected": 3})
        self.assertEqual(self.trace()["total"], 1)
        self.add(4, "ATTEMPT", {"outcome": "DONE"})
        self.add(5, "ATTEMPT", {"outcome": "DONE"})
        self.assertEqual(self.trace()["total"], 3)
        self.assertEqual(len(self.trace()["segments"]), 2)

    def test_correction_trims_latest_units_without_negative_collection_rows(self):
        self.result(1, 2)
        self.result(3, 3, "2026-06-02")
        self.result(5, 1, "2026-06-03")
        self.assertEqual([(s["date"], s["qty"]) for s in self.trace()["segments"]], [("2026-06-01", 1)])
        self.result(7, 2, "2026-06-04")
        self.assertEqual([(s["date"], s["qty"]) for s in self.trace()["segments"]], [("2026-06-01", 1), ("2026-06-04", 1)])

    def test_out_of_order_import_reconstructs_phone_sequence(self):
        self.result(1, 1)
        self.result(3, 3, "2026-06-02")
        self.events = self.events[2:] + self.events[:2]
        self.assertEqual([(s["date"], s["qty"]) for s in self.trace()["segments"]], [("2026-06-01", 1), ("2026-06-02", 2)])

    def test_new_phone_or_generation_reset_is_independent(self):
        self.result(100, 3)
        self.result(1, 1, "2026-06-02", gen="g-b")
        self.assertEqual(self.trace()["total"], 1)
        self.result(1, 2, "2026-06-03", device="phone-b", gen="g-b")
        self.assertEqual(self.trace()["total"], 2)

    def test_field_item_pending_price_and_later_link_are_preserved(self):
        self.add(1, "FIELD_ITEM_ADD", {"line_id": "field", "name": "가상 현장 품목", "qty": 2, "price_status": "PENDING"})
        self.add(2, "ITEM_RESULT", {"line_id": "field", "qty_confirmed": 2, "qty_collected": 2})
        self.add(3, "ATTEMPT", {"outcome": "DONE"})
        self.assertEqual(self.trace()["total"], 2)
        field = next(x for x in self.trace()["lines"] if x["line_id"] == "field")
        self.assertIsNone(field["price"])
        linked = self.C.trace_pure(self.items, self.events, [{"line_id": "field", "unit_price": 4000, "fee_rate_id": "fixture-link"}])
        self.assertEqual(next(x for x in linked["lines"] if x["line_id"] == "field")["price"], 4000)

    def test_bad_timestamp_uses_import_time_and_kst_day(self):
        self.add(1, "ITEM_RESULT", {"line_id": "chair", "qty_confirmed": 3, "qty_collected": 1})
        self.add(2, "ATTEMPT", {"outcome": "PARTIAL"}, stamp="2099-01-01T00:00:00Z")
        self.assertEqual(self.trace()["segments"][0]["date"], "2026-07-01")
        self.events[-1]["device_ts"] = "2026-06-01T15:00:00Z"
        self.assertEqual(self.trace()["segments"][0]["date"], "2026-06-02")

    def test_invalid_events_do_not_crash_or_invent_quantities(self):
        self.add(1, "ITEM_RESULT", {"line_id": "unknown", "qty_confirmed": 3, "qty_collected": 3})
        self.add(2, "ATTEMPT", {"outcome": []})
        self.add(3, "ITEM_RESULT", [])
        self.add(4, "ATTEMPT", {"outcome": "DONE"})
        self.assertEqual(self.trace()["total"], 0)

    def test_future_device_clock_is_checked_against_receipt_time_not_today(self):
        self.result(1, 1, "2026-06-02")
        for event in self.events:
            event["imported_at"] = "2026-06-01T01:00:00Z"
        class Clock(dt.datetime):
            day = 1
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, cls.day, 2, tzinfo=dt.timezone.utc).astimezone(tz) if tz else cls(2026, 6, cls.day, 2)
        with patch("dgpc.core.dt.datetime", Clock):
            first = self.trace()["segments"][0]["date"]
            Clock.day = 3
            second = self.trace()["segments"][0]["date"]
        self.assertEqual((first, second), ("2026-06-01", "2026-06-01"))


if __name__ == "__main__":
    unittest.main()
