"""PC 직접 수거: 실제 암호화 DB, 기존 번호 배정과 원본 서식 경로를 검증한다."""
import importlib.util
import json
import unittest
import uuid
from unittest.mock import patch

import test_label_auto as fixtures
from dgpc import core as C, service as S


class PCCollectionTests(unittest.TestCase):
    request = fixtures.LabelAutoTests.request
    stock = fixtures.LabelAutoTests.stock
    events = fixtures.LabelAutoTests.events
    active = fixtures.LabelAutoTests.active

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('dgpc.pc_collection'), 'PC 직접 수거 처리가 필요합니다')
        from dgpc import pc_collection
        self.P = pc_collection
        fixtures.LabelAutoTests.setUp(self)

    def tearDown(self):
        if hasattr(self, 'con'):
            fixtures.LabelAutoTests.tearDown(self)

    def body(self, r, qty, day='2026-06-10', **extra):
        current = S.get_request(self.con, r['id'])
        return {'req_ver': current['req_ver'], 'basis': current['collection']['basis'],
                'collected_on': day, 'reason': '가상 현장 확인', 'client_req_id': str(uuid.uuid4()),
                'items': [{'line_id': line['line_id'], 'qty': qty} for line in current['collection']['lines']], **extra}

    def test_partial_and_revisit_use_actual_dates_and_only_additional_labels(self):
        self.stock(day='2026-05-31')
        r, _ = self.request()
        first = self.P.save(self.con, r['id'], self.body(r, 1))
        self.assertEqual((first['status'], first['labels_assigned']), ('PARTIAL', 1))
        second = self.P.save(self.con, r['id'], self.body(r, 2, '2026-07-01'))
        current = S.get_request(self.con, r['id'])
        self.assertEqual((second['status'], current['state']['lines'][0]['qty_collected']), ('DONE', 3))
        self.assertEqual(len(self.active(r)), 3)
        summaries = C.rows(self.con, 'SELECT summary_json FROM result_import ORDER BY rowid')
        self.assertEqual([json.loads(s['summary_json'])['applied'] for s in summaries], [3, 3])
        from dgpc import official_forms
        for month, qty in [('2026-06', 1), ('2026-07', 2)]:
            self.assertEqual(official_forms.monthly_data(self.con, self.dongs['충장동'], month)['totals']['qty'], qty)
        self.assertEqual(current['payment']['paid'], 0, '수거 기록은 수납 기록을 만들면 안 됨')

    def test_duplicate_retry_returns_original_response_without_consuming_again(self):
        self.stock()
        r, _ = self.request()
        data = self.body(r, 1)
        first = self.P.save(self.con, r['id'], data)
        count = self.con.execute('SELECT COUNT(*) FROM result_event').fetchone()[0]
        self.assertEqual(self.P.save(self.con, r['id'], data), first)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM result_event').fetchone()[0], count)
        self.assertEqual(len(self.active(r)), 1)

    def test_invalid_input_never_writes_results(self):
        r, line = self.request()
        invalid = [{'items': []}, {'items': [{'line_id': 'wrong', 'qty': 1}]},
                   *({'items': [{'line_id': line, 'qty': q}]} for q in (-1, 4, 1.5, True, '1')),
                   {'collected_on': '2099-01-01'}, {'collected_on': '2026-02-30'},
                   {'reason': '', 'items': [{'line_id': line, 'qty': 0}]}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(C.UserError):
                self.P.save(self.con, r['id'], self.body(r, 1, **values))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM result_import').fetchone()[0], 0)

    def test_mobile_result_or_new_assignment_invalidates_old_form(self):
        r, line = self.request()
        data = self.body(r, 1)
        self.events(r, [('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 2}), ('ATTEMPT', {'outcome': 'PARTIAL'})])
        with self.assertRaises(C.StaleError):
            self.P.save(self.con, r['id'], data)
        data = self.body(r, 1)
        crew = S.save_crew(self.con, {'name': '가상 전담팀'})
        S.confirm_plan(self.con, C.today_kst(), {crew: [{'request_id': r['id']}]})
        with self.assertRaises(C.StaleError):
            self.P.save(self.con, r['id'], data)

    def test_cancelled_and_purged_requests_are_rejected(self):
        for column, value in [('status', 'CANCELLED'), ('pii_purged_at', C.now())]:
            r, _ = self.request()
            data = self.body(r, 1)
            self.con.execute(f'UPDATE request SET {column}=? WHERE id=?', (value, r['id']))
            with self.assertRaises(C.UserError):
                self.P.save(self.con, r['id'], data)

    def test_shortage_does_not_block_collection_and_reports_pending(self):
        r, _ = self.request()
        result = self.P.save(self.con, r['id'], self.body(r, 3))
        self.assertEqual((result['status'], result['labels_pending']), ('DONE', 3))
        self.stock()
        self.assertEqual(len(self.active(r)), 3)

    def test_allocator_failure_rolls_back_events_version_and_inventory(self):
        self.stock()
        r, _ = self.request()
        data = self.body(r, 1)
        with patch('dgpc.label_auto.sync', side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):
                self.P.save(self.con, r['id'], data)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM result_import').fetchone()[0], 0)
        self.assertEqual(S.get_request(self.con, r['id'])['req_ver'], r['req_ver'])
        self.assertEqual(len(self.active(r)), 0)

    def test_zero_collection_is_not_collected_and_releases_active_assignment(self):
        r, _ = self.request()
        crew = S.save_crew(self.con, {'name': '가상 방문팀'})
        S.confirm_plan(self.con, C.today_kst(), {crew: [{'request_id': r['id']}]})
        result = self.P.save(self.con, r['id'], self.body(r, 0))
        self.assertEqual((result['status'], result['labels_assigned']), ('NOT_COLLECTED', 0))
        self.assertIsNone(C.one(self.con, 'SELECT * FROM active_assignment WHERE request_id=?', (r['id'],)))

    def test_pending_mobile_edits_do_not_count_as_already_collected(self):
        r, line = self.request()
        self.events(r, [('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 2})])
        preview = S.get_request(self.con, r['id'])['collection']
        self.assertEqual(preview['lines'][0]['previous'], 0)
        self.P.save(self.con, r['id'], self.body(r, 1))
        self.assertEqual(S.get_request(self.con, r['id'])['state']['lines'][0]['qty_collected'], 1)

    def test_reversed_mobile_files_use_same_current_confirmed_and_collected_basis(self):
        r, line = self.request()
        self.events(r, [('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 5, 'qty_collected': 3}), ('ATTEMPT', {'outcome': 'PARTIAL'})], start=3)
        self.events(r, [('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 1}), ('ATTEMPT', {'outcome': 'PARTIAL'})], start=1)
        current = S.get_request(self.con, r['id'])
        self.assertEqual((current['collection']['lines'][0]['previous'], current['collection']['lines'][0]['remaining']), (3, 2))
        self.assertEqual(self.P.save(self.con, r['id'], self.body(r, 2))['total'], 5)

    def test_fresh_phone_daylist_carries_pc_baseline_through_python_protocol(self):
        import dgproto as P
        r, _ = self.request()
        result = self.P.save(self.con, r['id'], self.body(r, 1))
        crew = S.save_crew(self.con, {'name': '가상 새 목록팀'})
        batch = S.confirm_plan(self.con, C.today_kst(), {crew: [{'request_id': r['id']}]})[0]
        payload, _ = S.build_daylist(self.con, batch['batch_id'])
        self.assertEqual(payload['stops'][0].get('pc_collection_ver'), result['req_ver'])
        compact = P.compact_daylist(payload, S.FEE_ROWS)
        self.assertEqual(P.expand_daylist(compact, S.FEE_ROWS)['stops'][0]['pc_collection_ver'], result['req_ver'])


if __name__ == '__main__':
    unittest.main()
