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
        status, result, _ = self.post(path, data, cookie)
        self.assertEqual(status, 200, result)
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


if __name__ == '__main__':
    unittest.main()
