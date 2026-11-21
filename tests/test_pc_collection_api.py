"""PC 직접 수거 HTTP 권한·재시도·서명된 휴대폰 결과 충돌 회귀."""
import unittest
import uuid

import test_label_usage_api as fixture
import test_dong_work as scoped
from dgpc import area, core as C, server as SV, service as S, users as U


class PCCollectionAPI(fixture.UsageHTTPCase):
    waiting_post = scoped.DongWorkHTTPTest.waiting_post

    def body(self, r, qty=1):
        current = S.get_request(self.con, r['id'])
        return {'req_ver': current['req_ver'], 'basis': current['collection']['basis'], 'collected_on': C.today_kst(),
                'items': [{'line_id': line['line_id'], 'qty': qty} for line in current['collection']['lines']],
                'reason': '가상 전달 확인', 'client_req_id': str(uuid.uuid4())}

    def test_office_save_retry_and_permissions(self):
        r, _ = self.request()
        data = self.body(r)
        path = 'requests/' + r['id'] + '/collection'
        for _ in range(2):
            status, result, _ = self.post(path, data, self.worker_cookie)
            self.assertEqual(status, 200, result)
        self.assertEqual(S.get_request(self.con, r['id'])['state']['lines'][0]['qty_collected'], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM result_import WHERE device_id='PC-COLLECTION'").fetchone()[0], 1)
        for role in ['ADMIN', 'INTAKE', 'LEAD', 'FIELD', 'DONG']:
            self.assertEqual(SV.allowed(role, 'POST', path), role in ['ADMIN', 'INTAKE'])

    def test_dong_save_enforces_current_scope_even_for_idempotent_retry(self):
        a = area.get_dong(self.con, '충장동')['id']
        b = area.get_dong(self.con, '동명동')['id']
        uid = S.create_user(self.con, 'collection_dong', '가상 동 담당자', 'DONG', 'collection-dong-123', dong_ids=[a])
        cookie = self.login('collection_dong', 'collection-dong-123')
        r, _ = self.request()
        other, _ = self.request(dong='동명동')
        path = 'dong-work/requests/' + r['id'] + '/collection'
        data = self.body(r)
        data['items'][0].update(confirmed_qty=1, qty=1)
        data['field_items'] = [{'fee_rate_id': self.fee, 'confirmed_qty': 2, 'qty': 1}]
        status, result, _ = self.post(path, data, cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual((result['status'], result['added']), ('PARTIAL', 2))
        self.assertEqual(self.post(path, data, cookie)[1], result)
        state = S.get_request(self.con, r['id'])['state']
        self.assertEqual([(line['kind'], line['qty_confirmed'], line['qty_collected'])
                          for line in state['lines']], [('RECEIVED', 1, 1), ('FIELD', 2, 1)])
        self.assertEqual(self.post('dong-work/requests/' + other['id'] + '/collection', self.body(other), cookie)[0], 403)
        U.set_user_dongs(self.con, uid, [b])
        self.assertEqual(self.post(path, data, cookie)[0], 403)

    def test_logout_during_slow_body_prevents_collection(self):
        r, _ = self.request()
        self.assertTrue(SV.allowed('INTAKE', 'POST', 'requests/' + r['id'] + '/collection'), 'PC 수거 저장 경로가 필요합니다')
        with self.waiting_post('requests/' + r['id'] + '/collection', self.body(r), self.worker_cookie) as finish:
            self.post('logout', cookie=self.worker_cookie)
            self.assertEqual(finish()[0], 401)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM result_import').fetchone()[0], 0)

    def test_scope_change_during_body_wait_prevents_collection(self):
        a = area.get_dong(self.con, '충장동')['id']
        b = area.get_dong(self.con, '동명동')['id']
        uid = S.create_user(self.con, 'collection_scope', '가상 범위 담당', 'DONG', 'collection-scope-123', dong_ids=[a])
        cookie = self.login('collection_scope', 'collection-scope-123')
        r, _ = self.request()
        with self.waiting_post('dong-work/requests/' + r['id'] + '/collection', self.body(r), cookie) as finish:
            U.set_user_dongs(self.con, uid, [b])
            self.assertEqual(finish()[0], 403)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM result_import').fetchone()[0], 0)

    def test_old_signed_mobile_collection_is_queued_and_fresh_revision_is_allowed(self):
        r, line = self.request()
        seal, event = self.phone(r)
        old = [event('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 3}), event('ATTEMPT', {'outcome': 'DONE'})]
        status, result, _ = self.post('requests/' + r['id'] + '/collection', self.body(r), self.admin_cookie)
        self.assertEqual(status, 200, result)
        status, result, _ = self.post('import/result', seal(old), self.admin_cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['conflict'], 2, '옛 휴대폰 수량이 PC 입력을 덮어쓰면 안 됨')
        self.assertEqual(S.get_request(self.con, r['id'])['state']['lines'][0]['qty_collected'], 1)
        unaware = [event('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 3}), event('ATTEMPT', {'outcome': 'DONE'})]
        for ev in unaware:
            ev['base_req_ver'] = S.get_request(self.con, r['id'])['req_ver']
        self.assertEqual(self.post('import/result', seal(unaware), self.admin_cookie)[1]['conflict'], 2, '옛 앱이 새 요청 버전만 받아도 기준 확인 전에는 덮어쓰지 않음')
        fresh = [event('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 3}), event('ATTEMPT', {'outcome': 'DONE'})]
        for ev in fresh:
            ev['base_req_ver'] = S.get_request(self.con, r['id'])['req_ver']
            ev['body']['pc_collection_ver'] = ev['base_req_ver']
        self.assertEqual(self.post('import/result', seal(fresh), self.admin_cookie)[1]['applied'], 2)
        self.assertEqual(S.get_request(self.con, r['id'])['state']['lines'][0]['qty_collected'], 3)

    def test_recent_imports_identifies_pc_entry_without_phone_confirmation(self):
        r, _ = self.request()
        self.assertEqual(self.post('requests/' + r['id'] + '/collection', self.body(r), self.admin_cookie)[0], 200)
        status, recent = self.get('imports/recent', self.admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(recent[0]['kind'], 'PC')
        self.assertIsNone(recent[0]['release_code'])
        self.assertEqual(recent[0]['counts']['applied'], 3)

    def test_old_phone_duplicate_pc_field_item_cannot_double_fees_or_usage_on_review(self):
        from dgpc import numbered_labels
        numbered_labels.receive_range(self.con, '0001', '0010', dong_id=area.get_dong(self.con, '충장동')['id'], received_on=C.today_kst())
        for variant in ('catalog', 'waived', 'pending'):
            with self.subTest(variant=variant):
                r, _ = self.request()
                if variant == 'waived':
                    with C.tx(self.con):
                        self.con.execute("UPDATE request SET reduction_code='TEST',reduction_rate=100 WHERE id=?", (r['id'],))
                seal, event = self.phone(r)
                item = {'name': '가상 별도품목', 'unit': 'SET', 'spec': '가상 규격'} if variant == 'pending' else {'fee_rate_id': self.fee}
                data = self.body(r, 0)
                data['field_items'] = [{**item, 'confirmed_qty': 1, 'qty': 1}]
                status, _, _ = self.post('requests/' + r['id'] + '/collection', data, self.admin_cookie)
                self.assertEqual(status, 200)
                before = S.get_request(self.con, r['id'])
                mobile_line = str(uuid.uuid4())
                added = event('FIELD_ITEM_ADD', {**item, 'line_id': mobile_line, 'qty': 1})
                collected = event('ITEM_RESULT', {'line_id': mobile_line, 'qty_confirmed': 1, 'qty_collected': 1})
                attempt = event('ATTEMPT', {'outcome': 'DONE'})
                package = seal([added, collected, attempt])
                status, result, _ = self.post('import/result', package, self.admin_cookie)
                self.assertEqual((status, result['conflict']), (200, 3))
                status, bulk, _ = self.post('conflicts/bulk', {'action': 'APPLY', 'reason': '가상 확인', 'import_id': result['import_id']}, self.admin_cookie)
                after = S.get_request(self.con, r['id'])
                self.assertEqual((after['payment']['levied'], after['collection']['previous_total'], len(after['state']['lines'])),
                                 (before['payment']['levied'], 1, 2))
                allocated = C.one(self.con, 'SELECT COUNT(*) AS n FROM numbered_label_allocation WHERE request_id=? AND voided_at IS NULL', (r['id'],))
                self.assertEqual(allocated['n'], 1)
                self.assertEqual((status, bulk['resolved'], len(bulk['failed'])), (200, 0, 3))
                status, error, _ = self.post('conflicts/' + added['event_id'], {'action': 'APPLY', 'reason': '가상 개별'}, self.admin_cookie)
                self.assertEqual(status, 400, error)
                self.assertIn('PC 수거 처리', error['error'])
                self.assertEqual(self.post('import/result', package, self.admin_cookie)[1]['release_code'], result['release_code'])
                self.assertEqual(self.post('conflicts/bulk', {'action': 'REJECT', 'reason': '가상 PC 기록 대조', 'import_id': result['import_id']}, self.admin_cookie)[1]['resolved'], 3)
                self.assertEqual(after['status'], 'PARTIAL')

    def test_attempt_from_separate_file_keeps_duplicate_field_visit_in_review(self):
        r, _ = self.request()
        seal, event = self.phone(r)
        data = self.body(r, 0)
        data['field_items'] = [{'name': '가상 물건', 'unit': 'PIECE', 'confirmed_qty': 2, 'qty': 1}]
        self.assertEqual(self.post('requests/' + r['id'] + '/collection', data, self.admin_cookie)[0], 200)
        added = event('FIELD_ITEM_ADD', {'line_id': str(uuid.uuid4()), 'name': ' 가상  물건 ', 'unit': 'PIECE', 'qty': 2})
        attempt = event('ATTEMPT', {'outcome': 'PARTIAL'})
        self.assertEqual(self.post('import/result', seal([added]), self.admin_cookie)[1]['conflict'], 1)
        self.assertEqual(self.post('import/result', seal([attempt]), self.admin_cookie)[1]['conflict'], 1)
        verdict = {'action': 'APPLY', 'reason': '가상 확인'}
        self.assertEqual(self.post('conflicts/' + attempt['event_id'], verdict, self.admin_cookie)[0], 400)
        next_visit = event('ATTEMPT', {'outcome': 'PARTIAL'})
        self.assertEqual(self.post('import/result', seal([next_visit]), self.admin_cookie)[1]['conflict'], 1)
        self.assertEqual(self.post('conflicts/' + next_visit['event_id'], verdict, self.admin_cookie)[0], 200)

    def test_completed_pc_collection_preserved_for_known_and_stale_phone_lists(self):
        r, line = self.request()
        seal, event = self.phone(r)
        status, saved, _ = self.post('requests/' + r['id'] + '/collection', self.body(r, 3), self.admin_cookie)
        self.assertEqual((status, saved['status']), (200, 'DONE'))
        for current in (True, False):
            events = [event('FIELD_ITEM_ADD', {'line_id': str(uuid.uuid4()), 'fee_rate_id': self.fee, 'qty': 1}),
                      event('ITEM_RESULT', {'line_id': line, 'qty_confirmed': 3, 'qty_collected': 0}),
                      event('ATTEMPT', {'outcome': 'NOT_COLLECTED'}), event('FIELD_MEMO', {'text': '가상 완료 지점 확인'})]
            if current:
                for ev in events:
                    ev['base_req_ver'] = saved['req_ver']
                    ev['body']['pc_collection_ver'] = saved['req_ver']
            status, result, _ = self.post('import/result', seal(events), self.admin_cookie)
            after = S.get_request(self.con, r['id'])
            self.assertEqual((after['status'], after['collection']['previous_total'], len(after['state']['lines'])), ('DONE', 3, 1))
            self.assertEqual((status, result['conflict'], result['applied']), (200, 3, 1))
            self.assertIn('가상 완료 지점 확인', after['state']['memos'])
            status, bulk, _ = self.post('conflicts/bulk', {'action': 'APPLY', 'reason': '가상 확인', 'import_id': result['import_id']}, self.admin_cookie)
            self.assertEqual((status, bulk['resolved'], len(bulk['failed'])), (200, 0, 3))

    def test_manual_review_allows_distinct_stale_addition_and_known_later_addition(self):
        r, _ = self.request()
        seal, event = self.phone(r)
        data = self.body(r, 0)
        data['field_items'] = [{'fee_rate_id': self.fee, 'confirmed_qty': 1, 'qty': 1}]
        version = self.post('requests/' + r['id'] + '/collection', data, self.admin_cookie)[1]['req_ver']
        different = event('FIELD_ITEM_ADD', {'line_id': str(uuid.uuid4()), 'name': '가상 별도', 'unit': 'EA', 'qty': 1})
        self.assertEqual(self.post('import/result', seal([different]), self.admin_cookie)[1]['conflict'], 1)
        self.assertEqual(self.post('conflicts/' + different['event_id'], {'action': 'APPLY', 'reason': '다른 품목 확인'}, self.admin_cookie)[0], 200)
        # PC에서 다음 방문을 기록해 버전이 올랐더라도 휴대폰은 전에 추가한 이 품목을 이미 알고 있다.
        self.assertEqual(self.post('requests/' + r['id'] + '/collection', self.body(r, 0), self.admin_cookie)[0], 200)
        known = event('FIELD_ITEM_ADD', {'line_id': str(uuid.uuid4()), 'fee_rate_id': self.fee, 'qty': 1, 'pc_collection_ver': version})
        known['base_req_ver'] = version
        self.assertEqual(self.post('import/result', seal([known]), self.admin_cookie)[1]['conflict'], 1)
        self.assertEqual(self.post('conflicts/' + known['event_id'], {'action': 'APPLY', 'reason': '실제 별도 품목 확인'}, self.admin_cookie)[0], 200)


if __name__ == '__main__':
    unittest.main()
