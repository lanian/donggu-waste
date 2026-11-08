"""일일 작업일지·월보 회귀: 임시 DB와 가상 자료만 사용한다.

실행: python tests/test_pc_reports.py
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from dgpc import reports as R, store  # noqa: E402
from dgpc.core import KST, UserError  # noqa: E402


class ReportsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="dg-reports-")
        cls.old_env = {key: os.environ.get(key) for key in ("DG_STATE", "DG_OUT", "DG_DB_KEY")}
        os.environ.update(DG_STATE=cls.directory.name, DG_OUT=str(Path(cls.directory.name) / "out"),
                          DG_DB_KEY="reports-test-key-not-for-production")
        cls.con = store.connect(create=True)
        cls.con.execute("INSERT INTO crew (id,name) VALUES ('crew-test','=가상 수거팀')")
        cls.con.execute("""INSERT INTO result_import (id,channel,device_id,package_sha256,imported_at)
            VALUES ('import-test','USB','device-test','reports-fixture','2026-11-08T01:00:00Z')""")
        fixtures = [
            ("done", "2026-11-07", "DONE", "2026-11-05T15:00:00Z", "2026-11-07T14:59:59Z", None, 3, 3, 2000),
            ("partial", "2026-11-08", "PARTIAL", "2026-11-06T15:00:00Z", "2026-11-07T15:00:00Z", None, 5, 2, 1000),
            ("cancel", "2026-11-07", "CANCELLED", "2026-11-05T16:00:00Z", None, "2026-11-07T14:59:59Z", 7, 0, 1000),
            ("pending", "2026-11-07", "ON_HOLD", "2026-11-07T14:59:59Z", "2026-11-07T14:59:59Z", None, 1, 0, None),
            ("month-in", None, "RECEIVED", "2026-10-31T15:00:00Z", None, None, 1, 0, 1000),
            ("month-out", None, "RECEIVED", "2026-11-30T15:00:00Z", None, None, 1, 0, 1000),
        ]
        for seq, (rid, planned, status, received, completed, cancelled, qty, collected, price) in enumerate(fixtures):
            cls.con.execute("""INSERT INTO request
                (id,receipt_no,name,phone,address,spot_note,memo,planned_date,status,received_at,updated_at,
                 completed_at,cancelled_at,dong,channel)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, "PRIVATE-RECEIPT-" + rid, "가상주민비공개", "01000009090", "가상주민주소비공개",
                 "가상상세위치비공개", "가상메모비공개", planned, status, received, received, completed, cancelled, "가상1동", "전화"))
            cls.con.execute("""INSERT INTO request_item
                (line_id,request_id,name,spec,unit,unit_label,qty,unit_price,amount,price_status)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("line-" + rid, rid, "=가상 품목", "기본", "EA", "개", qty, price,
                 price * qty if price is not None else None, "CONFIRMED" if price is not None else "PENDING"))
            if collected:
                cls.con.execute("""INSERT INTO result_event
                    (event_id,import_id,device_id,device_seq,request_id,assign_ver,kind,body_json,apply_status,device_ts)
                    VALUES (?,'import-test','device-test',?,?,1,'ITEM_RESULT',?,'APPLIED','2026-11-07T01:00:00Z')""",
                    ("event-" + rid, seq + 1, rid, json.dumps({"line_id": "line-" + rid, "qty_confirmed": qty, "qty_collected": collected})))
        cls.con.execute("""UPDATE request SET reduction_code='FIXTURE',reduction_rate=50,
            reduction_label='가상 감면',reduction_round_unit=1 WHERE id='done'""")
        cls.con.execute("""INSERT INTO assignment_batch (id,crew_id,work_date,assign_ver,status,created_at)
            VALUES ('batch-test','crew-test','2026-11-07',1,'ACTIVE','2026-11-06T01:00:00Z')""")
        cls.con.execute("INSERT INTO assignment_stop (batch_id,request_id,seq) VALUES ('batch-test','done',1)")
        cls.con.execute("INSERT INTO active_assignment (request_id,batch_id) VALUES ('done','batch-test')")
        for uid, work_date, volume, weight, void in [
            ("u-volume", "2026-11-07", 1.25, None, None),
            ("u-weight", "2026-11-07", None, 100, None),
            ("u-void", "2026-11-07", 999, 999, "가상 무효"),
            ("u-next", "2026-11-08", 9, None, None),
        ]:
            cls.con.execute("""INSERT INTO unload_log
                (id,crew_id,work_date,place,volume_m3,weight_kg,note,recorded_at,void_reason)
                VALUES (?,'crew-test',?,'가상 반입지',?,?,'가상하차메모비공개','2026-11-07T01:00:00Z',?)""",
                (uid, work_date, volume, weight, void))
        for pid, rid, kind, amount, paid_on, void in [
            ("pay-before", "done", "PAY", 4000, "2026-11-06", None),
            ("pay-day", "done", "PAY", 1000, "2026-11-07", None),
            ("refund-day", "done", "REFUND", 200, "2026-11-07", None),
            ("pay-other-cohort", "partial", "PAY", 300, "2026-11-07", None),
            ("pay-void", "done", "PAY", 999999, "2026-11-07", "가상 무효"),
            ("pay-next-month", "month-out", "PAY", 800, "2026-12-01", None),
        ]:
            cls.con.execute("""INSERT INTO payment
                (id,request_id,kind,amount,method,paid_on,note,recorded_at,void_reason)
                VALUES (?,?,?,?, '현금',?,'가상납부메모비공개','2026-11-07T01:00:00Z',?)""",
                (pid, rid, kind, amount, paid_on, void))

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.directory.cleanup()
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def report_function(self, name):
        fn = getattr(R, name, None)
        self.assertTrue(callable(fn), name + " 보고서 함수가 필요합니다.")
        return fn

    def daily(self, date="2026-11-07", basis="planned"):
        return self.report_function("daily_report")(self.con, date, basis)

    def monthly(self, month="2026-11", basis="planned"):
        return self.report_function("monthly_report")(self.con, month, basis)

    def snapshot(self):
        tables = [row[0] for row in self.con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: sorted(self.con.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr) for name in tables}

    def test_processing_days_use_actual_kst_completion_for_every_basis(self):
        self.con.execute("SAVEPOINT report_lead_days")
        try:
            self.con.execute("UPDATE request SET completed_at='2026-11-10T15:00:00Z' WHERE id='done'")
            periods = (("planned", "2026-11-07"), ("received", "2026-11-06"), ("completed", "2026-11-11"))
            for basis, date in periods:
                with self.subTest(basis=basis):
                    self.assertEqual(R.stats(self.con, date, date, basis)["totals"]["lead_days_avg"], 5)
            self.assertEqual(self.daily()["totals"]["lead_days_avg"], 5)
            self.assertEqual(self.monthly()["totals"]["lead_days_avg"], 5)
            pilot = R.pilot_report(self.con, "2026-11-06", "2026-11-06")
            self.assertEqual(next(row["pilot"] for row in pilot["rows"] if row["key"] == "lead_days"), 5)
        finally:
            self.con.execute("ROLLBACK TO report_lead_days")
            self.con.execute("RELEASE report_lead_days")

    def test_processing_days_do_not_use_planned_date_when_completion_is_missing(self):
        self.con.execute("SAVEPOINT report_missing_completion")
        try:
            self.con.execute("UPDATE request SET completed_at=NULL WHERE id='done'")
            self.assertIsNone(self.daily()["totals"]["lead_days_avg"])
        finally:
            self.con.execute("ROLLBACK TO report_missing_completion")
            self.con.execute("RELEASE report_missing_completion")

    def test_processing_days_include_completed_requests_without_a_planned_date(self):
        self.con.execute("SAVEPOINT report_missing_plan")
        try:
            self.con.execute("UPDATE request SET planned_date=NULL WHERE id='done'")
            report = R.stats(self.con, "2026-11-06", "2026-11-06", "received")
            self.assertEqual(report["totals"]["lead_days_avg"], 1)
        finally:
            self.con.execute("ROLLBACK TO report_missing_plan")
            self.con.execute("RELEASE report_missing_plan")

    def test_daily_reuses_stats_and_explains_current_cumulative_values(self):
        report = self.daily()
        self.assertEqual(report["kind"], "daily")
        self.assertEqual(report["work_date"], "2026-11-07")
        for key, value in R.stats(self.con, "2026-11-07", "2026-11-07").items():
            self.assertEqual(report[key], value)
        self.assertEqual(report["totals"]["requests"], 3)
        self.assertEqual(report["totals"]["by_status"], {"DONE": 1, "CANCELLED": 1, "ON_HOLD": 1})
        self.assertEqual(report["items"][0]["collected"], 3)
        self.assertEqual(report["totals"]["calculated"], 6000)
        self.assertEqual(report["totals"]["reduction"], 3000)
        self.assertEqual(report["totals"]["levied"], 3000)
        self.assertEqual(report["totals"]["amount_pending"], 1)
        for term in ("현재", "누적", "수거 예정일", "작업일", "납부일"):
            self.assertIn(term, report["source_note"])
        self.assertLess(abs((dt.datetime.now(KST).replace(tzinfo=None) - dt.datetime.strptime(report["generated_at"], "%Y-%m-%d %H:%M")).total_seconds()), 120)

    def test_daily_basis_uses_kst_and_final_result_date(self):
        received = self.daily(basis="received")
        completed = self.daily(basis="completed")
        next_day = self.daily("2026-11-08", "completed")
        self.assertEqual(received["totals"]["requests"], 2)
        self.assertEqual(received["totals"]["by_status"], {"PARTIAL": 1, "ON_HOLD": 1})
        self.assertIn("접수일", received["source_note"])
        self.assertEqual(completed["totals"]["requests"], 3)
        self.assertEqual(completed["totals"]["by_status"]["CANCELLED"], 1)
        self.assertEqual(next_day["totals"]["by_status"], {"PARTIAL": 1})
        self.assertEqual(next_day["items"][0]["collected"], 2)

    def test_unload_work_dates_and_payment_dates_are_independent_of_basis(self):
        for basis in ("planned", "received", "completed"):
            report = self.daily(basis=basis)
            self.assertEqual(report["unload_summary"], {"count": 2, "volume_records": 1, "weight_records": 1})
            self.assertEqual(report["totals"]["unload_m3"], 1.25)
            self.assertEqual(report["totals"]["unload_kg"], 100)
            self.assertEqual(report["revenue"]["totals"], {"pay_n": 2, "pay": 1300, "refund_n": 1, "refund": 200, "net": 1100})
        self.assertEqual(self.daily()["totals"]["paid"], 4800)

    def test_monthly_uses_full_calendar_month_and_keeps_stats_contract(self):
        report = self.monthly(basis="received")
        self.assertEqual((report["kind"], report["month"], report["start"], report["end"]),
                         ("monthly", "2026-11", "2026-11-01", "2026-11-30"))
        for key, value in R.stats(self.con, "2026-11-01", "2026-11-30", "received").items():
            self.assertEqual(report[key], value)
        self.assertEqual(report["totals"]["requests"], 5)
        self.assertEqual(report["unload_summary"], {"count": 3, "volume_records": 2, "weight_records": 1})
        self.assertEqual(report["revenue"]["totals"]["net"], 5100)
        self.assertEqual(self.monthly("2024-02")["end"], "2024-02-29")
        self.assertEqual(self.monthly("2025-02")["end"], "2025-02-28")

    def test_empty_reports_have_zero_totals_and_measurement_counts(self):
        for report in (self.daily("2020-01-01"), self.monthly("2020-01")):
            self.assertEqual(report["totals"]["requests"], 0)
            self.assertEqual(report["totals"]["unload_m3"], 0)
            self.assertEqual(report["revenue"]["totals"]["net"], 0)
            self.assertEqual(report["unload_summary"], {"count": 0, "volume_records": 0, "weight_records": 0})
            self.assertEqual(report["items"], [])

    def test_invalid_date_month_and_basis_are_user_errors(self):
        daily, monthly = self.report_function("daily_report"), self.report_function("monthly_report")
        for date in ("", "2026-02-30", "2026-1-01", "9999-12-31", None, 20261107):
            with self.subTest(date=date), self.assertRaises(UserError):
                daily(self.con, date)
        for month in ("", "2026-13", "2026-00", "2026-1", "2026-01-01", "0000-01", "9999-12", None, 202611):
            with self.subTest(month=month), self.assertRaises(UserError):
                monthly(self.con, month)
        for fn, date in ((daily, "2026-11-07"), (monthly, "2026-11")):
            with self.assertRaises(UserError):
                fn(self.con, date, "invalid")

    def test_csv_contains_all_aggregate_sections_and_escapes_spreadsheet_formulas(self):
        for name, period in (("daily_report_csv", "2026-11-07"), ("monthly_report_csv", "2026-11")):
            with self.subTest(name=name):
                raw = self.report_function(name)(self.con, period)
                self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
                cells = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
                single_rows = [row[0] for row in cells if len(row) == 1]
                for title in ("요약", "상태별", "날짜별", "행정동별", "품목별", "품목 분류별", "수거팀별", "접수 경로별", "감면별", "월별", "하차 기록", "세입(납부일 기준)"):
                    self.assertIn(title, single_rows)
                self.assertTrue(any("'=가상 품목" in row for row in cells))
                self.assertTrue(any("'=가상 수거팀" in row for row in cells))

    def test_json_and_csv_do_not_expose_resident_or_request_details(self):
        outputs = [json.dumps(self.daily(), ensure_ascii=False), json.dumps(self.monthly(), ensure_ascii=False),
                   self.report_function("daily_report_csv")(self.con, "2026-11-07").decode("utf-8-sig"),
                   self.report_function("monthly_report_csv")(self.con, "2026-11").decode("utf-8-sig")]
        for value in outputs:
            for secret in ("가상주민비공개", "01000009090", "가상주민주소비공개", "가상상세위치비공개", "가상메모비공개",
                           "가상하차메모비공개", "가상납부메모비공개", "PRIVATE-RECEIPT-", '"request_id"', '"receipt_no"'):
                self.assertNotIn(secret, value)

    def test_generation_and_csv_leave_all_source_tables_unchanged(self):
        before = self.snapshot()
        self.daily()
        self.monthly()
        self.report_function("daily_report_csv")(self.con, "2026-11-07")
        self.report_function("monthly_report_csv")(self.con, "2026-11")
        self.assertEqual(self.snapshot(), before)
        readonly = store.connect(readonly=True)
        try:
            self.assertEqual(R.daily_report(readonly, "2026-11-07")["totals"]["requests"], 3)
            self.assertTrue(R.monthly_report_csv(readonly, "2026-11"))
        finally:
            readonly.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
