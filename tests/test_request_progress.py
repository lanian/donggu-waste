"""가상 접수의 본청·동별 진행 표시와 일괄 조회 회귀."""
import unittest

import test_label_usage_api as fixture
from dgpc import addresses, area, core as C, service as S


class RequestProgressTests(fixture.UsageHTTPCase):
    def test_office_and_scoped_views_keep_the_current_visit_separate_from_the_last_outcome(self):
        dong = area.get_dong(self.con, '충장동')['id']
        S.create_user(self.con, 'progress_dong', '가상 담당자', 'DONG', 'progress-test-123', dong_ids=[dong])
        cookie = self.login('progress_dong', 'progress-test-123')
        req, line_id = self.request()
        other, _ = self.request(dong='동명동')
        rid = req['id']
        day = C.today_kst()
        unit = req['items'][0]['unit_label']

        def snapshots():
            out = []
            for path, session in [('requests', self.admin_cookie), ('dong-work/requests', cookie)]:
                code, listing = self.get(path + '?bucket=all', session)
                self.assertEqual(code, 200, listing)
                keyed = {r['id']: r for r in listing['rows']}
                if session == cookie:
                    self.assertNotIn(other['id'], keyed)
                self.assertIn('progress', keyed[rid], '접수 목록에 진행 근거가 필요합니다')
                out.append(keyed[rid]['progress'])
                code, detail = self.get(path + '/' + rid, session)
                self.assertEqual(code, 200, detail)
                out.append(detail['progress'])
            self.assertTrue(all(item == out[0] for item in out), out)
            return out[0]

        progress = snapshots()
        self.assertEqual(progress, {'status': 'RECEIVED', 'last_outcome': None, 'assigned_to': None,
                                    'pay_status': 'UNPAID', 'remaining': [{'qty': 3, 'unit_label': unit}],
                                    'collected': [], 'planned_date': None, 'has_visit': False, 'has_location': False,
                                    'revisit_confirmed': False})
        self.assertEqual(self.get('dong-work/requests/' + other['id'], cookie)[0], 403)
        self.assertEqual(self.get('requests/' + rid, cookie)[0], 403)
        current = S.get_request(self.con, rid)
        code, out, _ = self.post('requests/' + rid + '/collection', {
            'req_ver': current['req_ver'], 'basis': current['collection']['basis'], 'collected_on': day,
            'client_req_id': 'progress-partial', 'reason': '가상 품목 확인',
            'items': [{'line_id': line_id, 'qty': 1}],
            'field_items': [{'name': '가상 다른 단위', 'unit': 'L20', 'confirmed_qty': 3, 'qty': 2}]}, self.admin_cookie)
        self.assertEqual(code, 200, out)
        partial = snapshots()
        self.assertEqual((partial['status'], partial['last_outcome'], partial['pay_status'], partial['has_visit']),
                         ('PARTIAL', 'PARTIAL', 'AMOUNT_PENDING', True))
        self.assertEqual({r['unit_label']: r['qty'] for r in partial['collected']}, {unit: 1, '20ℓ당': 2})
        self.assertEqual({r['unit_label']: r['qty'] for r in partial['remaining']}, {unit: 2, '20ℓ당': 1})
        crew = S.save_crew(self.con, {'name': '가상 재방문팀'})
        S.confirm_plan(self.con, day, {crew: [{'request_id': rid}]})
        revisit = snapshots()
        self.assertEqual((revisit['status'], revisit['last_outcome'], revisit['assigned_to'], revisit['planned_date']),
                         ('ASSIGNED', 'PARTIAL', '가상 재방문팀', day))

    def test_only_applied_visit_evidence_counts(self):
        req, _ = self.request()
        self.events(req, [('NAV_INTENT', {}), ('DIAL_INTENT', {}), ('SMS_INTENT', {}),
                          ('CONTACT', {'result': 'REACHED'})])
        self.events(req, [('ARRIVE', {})], applied='CONFLICT')
        detail = S.get_request(self.con, req['id'])
        self.assertIn('progress', detail, '상세에 방문 근거가 필요합니다')
        self.assertFalse(detail['progress']['has_visit'])
        self.events(req, [('ARRIVE', {})])
        self.assertTrue(S.get_request(self.con, req['id'])['progress']['has_visit'])

    def test_payment_and_cancel_are_independent_of_collection(self):
        req, line = self.request(qty=1)
        rid = req['id']
        amount = req['payment']['levied']
        code, saved, _ = self.post('requests/' + rid + '/collection', {
            'req_ver': req['req_ver'], 'basis': req['collection']['basis'], 'collected_on': C.today_kst(),
            'client_req_id': 'progress-done', 'items': [{'line_id': line, 'qty': 1}]}, self.admin_cookie)
        self.assertEqual(code, 200, saved)

        def check(request_id, status, pay):
            detail = S.get_request(self.con, request_id)
            progress = detail['progress']
            self.assertEqual((progress['status'], progress['pay_status'], detail['payment']['status']), (status, pay, pay))
            self.assertEqual(S.search_requests(self.con, {'q': detail['receipt_no']})['rows'][0]['progress'], progress)
            return progress

        self.assertEqual(check(rid, 'DONE', 'UNPAID')['remaining'], [])
        S.add_payment(self.con, rid, {'amount': amount, 'method': '무통장 입금'})
        check(rid, 'DONE', 'PAID')
        other, _ = self.request(qty=1)
        S.add_payment(self.con, other['id'], {'amount': amount, 'method': '현장 카드'})
        check(other['id'], 'RECEIVED', 'PAID')
        S.cancel_request(self.con, other['id'], code='RESIDENT', req_ver=other['req_ver'])
        self.assertFalse(check(other['id'], 'CANCELLED', 'REFUND_NEEDED')['has_visit'])

    def test_confirmed_revisit_is_distinct_from_rescheduling_or_automatic_carry(self):
        req, _ = self.request()
        rid, today = req['id'], C.today_kst()
        path = 'dong-work/followups/' + rid
        code, out, _ = self.post('requests/' + rid + '/contacts', {'kind': '독촉', 'content': '가상 확인'}, self.admin_cookie)
        self.assertEqual(code, 200, out)

        def check(expected):
            detail = S.get_request(self.con, rid)
            row = S.search_requests(self.con, {'q': req['receipt_no']})['rows'][0]
            code, followup = self.get(path, self.admin_cookie)
            self.assertEqual(code, 200, followup)
            self.assertIn('revisit_confirmed', detail['progress'], '날짜와 별도로 재방문 약속 확정 여부가 필요합니다')
            self.assertEqual([detail['progress']['revisit_confirmed'], row['progress']['revisit_confirmed'],
                              followup['appointment']['confirmed']], [expected] * 3)
            return detail

        S.reschedule_requests(self.con, [rid], today, code='RESIDENT')
        regular = check(False)
        def confirm(request):
            code, out, _ = self.post(path + '/revisit', {'date': today, 'start': '09:00', 'end': '10:00',
                                     'req_ver': request['req_ver']}, self.admin_cookie)
            self.assertEqual(code, 200, out)
        confirm(regular)
        confirmed = check(True)
        S.update_request(self.con, rid, {**confirmed, 'appt_end': '10:30'}, '가상 방문 시간 변경')
        confirm(check(False))
        check(True)
        S.reschedule_requests(self.con, [rid], '2099-06-01', code='RESIDENT')
        S.reschedule_requests(self.con, [rid], today, code='RESIDENT')
        confirm(check(False))  # 마감 직전까지 오늘 약속이 확정됨
        check(True)
        closed = S.close_day(self.con, today)
        self.assertIn(rid, [r['id'] for r in closed['carried']])
        carried = check(False)
        self.assertNotEqual(carried['planned_date'], today)

    def test_query_count_stays_constant_for_a_page_of_requests(self):
        def scheduled_request():
            req, _ = self.request()
            S.reschedule_requests(self.con, [req['id']], C.today_kst(), code='RESIDENT')
        scheduled_request()

        def select_count():
            queries = []
            self.con.set_trace_callback(queries.append)
            try:
                page = S.search_requests(self.con, {'size': 50})
            finally:
                self.con.set_trace_callback(None)
            self.assertTrue(all('progress' in row for row in page['rows']), '목록 조회의 공통 진행 상태')
            return sum(q.lstrip().upper().startswith('SELECT') for q in queries)

        baseline = select_count()
        for _ in range(4):
            scheduled_request()
        self.assertEqual(select_count(), baseline)

    def test_unreadable_appointment_audit_does_not_block_other_requests(self):
        today = C.today_kst()
        requests = [self.request()[0] for _ in range(3)]
        for req in requests:
            S.reschedule_requests(self.con, [req['id']], today, code='RESIDENT')
        details = ['{', '[]', '{"date":"' + today + '","start":null,"end":null}']
        for req, raw in zip(requests, details):
            self.con.execute("""INSERT INTO audit_log(entity,entity_id,action,detail,at,actor)
                VALUES ('request',?,'REVISIT_APPT',?,?,'progress-fixture')""", (req['id'], raw, C.now()))
        listed = {r['id']: r['progress']['revisit_confirmed'] for r in S.search_requests(self.con)['rows']}
        self.assertEqual([listed[r['id']] for r in requests], [False, False, True])

    def test_address_warnings_match_individual_checks_after_prefetch(self):
        S.set_setting(self.con, 'jurisdiction', '동구')
        for address, outside in [('광주광역시 북구 가상로 12', True), ('광주광역시 동구 서구로 7', False),
                                 ('가상구길 5', False)]:
            req = S.create_request(self.con, {'name': '가상 주소', 'phone': '01000001234', 'address': address,
                                   'lat': 35.12, 'lon': 126.91, 'items': [{'fee_rate_id': self.fee, 'qty': 1}]})
            row = S.search_requests(self.con, {'q': req['receipt_no']})['rows'][0]
            warnings = addresses.address_warnings(self.con, address)
            self.assertEqual(row['warnings'], warnings)
            self.assertEqual(any('관할' in warning for warning in warnings), outside)
            self.assertTrue(row['progress']['has_location'])


if __name__ == '__main__':
    unittest.main()
