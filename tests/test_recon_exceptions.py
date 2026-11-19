"""수납 대사 불일치의 반입·재조회·검토 권한과 원장 분리 (임시 SQLCipher)."""
import http.client
import json
import tempfile
import unittest
from unittest.mock import patch

from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import finance as F, service as S, store


class ReconExceptionTest(SessionHTTPTestCase):
    def get(self, path, cookie=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request('GET', '/api/' + path, headers={'Cookie': cookie or self.worker_cookie, 'X-DG-CSRF': '1'})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def upload(self, raw, preview=False, token=''):
        code, body, _ = self.post(f'recon?preview={int(preview)}&preview_token={token}', raw, self.worker_cookie,
                                  {'Content-Type': 'application/octet-stream'})
        return code, body

    def mixed_file(self):
        day = S.today_kst()
        normal, different, cancelled = [S.create_request(self.con, self.request_body()) for _ in range(3)]
        amount = S.payment_status(self.con, normal['id'])['levied']
        S.cancel_request(self.con, cancelled['id'], '가상 대사 테스트')
        pending_data = self.request_body()
        pending_data['items'] = [{'name': '가상 미확정 품목', 'qty': 1}]
        pending = S.create_request(self.con, pending_data)
        raw = ('부과번호,금액,납부일,영수번호,납부방법\n'
               f'{normal["levy_no"]},{amount},{day},가상-정상,가상계좌\n'
               f'{different["levy_no"]},{amount + 1},{day},가상-비공개참조,가상-비공개방법\n'
               f'{cancelled["levy_no"]},{amount},{day},,\n'
               f'{pending["levy_no"]},{amount},{day},,\n'
               f'2099-ZZ-00001,{amount},{day},,\n'
               f'가상-원본셀-비공개,{amount},{day},,\n'
               f',{amount},{day},,\n'
               f'{different["levy_no"]},{amount + 1},{day},,\n').encode()
        return raw, normal, different, cancelled, amount, day

    def test_mixed_import_and_retry(self):
        raw, normal, different, cancelled, amount, day = self.mixed_file()
        status, preview = self.upload(raw, preview=True)
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview['counts'], {'MATCH': 1, 'AMOUNT_DIFF': 1, 'CANCELLED': 1, 'AMOUNT_PENDING': 1,
                                             'UNMATCHED': 2, 'BAD': 1, 'DUPLICATE': 1})
        self.assertEqual(preview['exception_count'], 7)
        self.assertEqual(self.get('recon-exceptions')[1]['rows'], [])
        self.assertEqual(S.rows(self.con, 'SELECT * FROM payment'), [])

        audit = F.C.audit

        def fail_import_audit(con, entity, *args):
            if entity == 'revenue':
                raise RuntimeError('가상 반입 쓰기 실패')
            return audit(con, entity, *args)

        with patch.object(F.C, 'audit', fail_import_audit), self.assertRaisesRegex(RuntimeError, '가상 반입 쓰기 실패'):
            F.import_recon(self.con, raw, preview=False)
        self.assertEqual(self.get('recon-exceptions')[1]['rows'], [])
        self.assertEqual(self.get('recon-history')[1], [])
        self.assertEqual(S.rows(self.con, 'SELECT * FROM payment'), [])

        status, result = self.upload(raw)
        self.assertEqual(status, 200, result)
        self.assertEqual((result['recorded'], result['exception_count']), (1, 7))
        iid = result['import_id']
        code, page = self.get('recon-exceptions?import_id=' + iid)
        self.assertEqual(code, 200, page)
        self.assertEqual(len(page['rows']), 7)
        by_line = {r['file_line']: r for r in page['rows']}
        self.assertEqual((by_line[3]['kind'], by_line[3]['request_id'], by_line[3]['levied'], by_line[3]['amount']),
                         ('AMOUNT_DIFF', different['id'], amount, amount + 1))
        self.assertEqual((by_line[4]['kind'], by_line[4]['request_id']), ('CANCELLED', cancelled['id']))
        self.assertEqual((by_line[6]['levy_no'], by_line[7]['levy_no']), ('2099-ZZ-00001', None))
        self.assertTrue(all(r['status'] == 'OPEN' for r in page['rows']))
        persisted = json.dumps(S.rows(self.con, 'SELECT * FROM recon_exception'), ensure_ascii=False)
        self.assertNotIn('비공개', persisted)
        self.assertNotIn('비공개', json.dumps(page, ensure_ascii=False))
        ledger = S.rows(self.con, 'SELECT request_id, amount, paid_on, source, receipt_ref FROM payment')
        self.assertEqual(ledger, [{'request_id': normal['id'], 'amount': amount, 'paid_on': day,
                                   'source': '대사', 'receipt_ref': '가상-정상'}])
        self.assertEqual(self.upload(raw, preview=True)[1]['already_imported']['id'], iid)
        self.assertEqual(self.upload(raw)[0], 400)
        self.assertEqual(len(self.get('recon-exceptions')[1]['rows']), 7)
        self.assertEqual(len(self.get('recon-history')[1]), 1)
        self.assertEqual(S.rows(self.con, 'SELECT request_id, amount, paid_on, source, receipt_ref FROM payment'), ledger)

    def test_review_history_access_and_ledger(self):
        raw, normal, _, _, amount, day = self.mixed_file()
        self.assertEqual(self.upload(raw)[0], 200)
        case = next(x for x in self.get('recon-exceptions')[1]['rows'] if x['kind'] == 'CANCELLED')
        path = 'recon-exceptions/' + case['id']
        code, refund, _ = self.post('refunds/request', {'request_id': normal['id'], 'amount': amount,
                                                       'note': '가상 결재 유지 확인'}, self.worker_cookie)
        self.assertEqual(code, 200, refund)

        passwords = 'recon-test-pass-123'
        dong_id = S.one(self.con, 'SELECT id FROM dong ORDER BY id LIMIT 1')['id']
        for role in ('LEAD', 'FIELD', 'DONG'):
            user = 'recon_' + role.lower()
            S.create_user(self.con, user, '가상 ' + role, role, passwords, dong_ids=[dong_id] if role == 'DONG' else None)
            cookie = self.login(user, passwords)
            read_code = 200 if role == 'LEAD' else 403
            self.assertEqual(self.get('recon-exceptions', cookie)[0], read_code)
            self.assertEqual(self.get(path, cookie)[0], read_code)
            self.assertEqual(self.post(path, {'status': 'RESOLVED', 'basis': 'LEDGER_CHECKED', 'version': 1}, cookie)[0], 403)
            if role == 'LEAD':
                lead_cookie = cookie

        def financial_state():
            return (S.rows(self.con, 'SELECT * FROM payment'), S.rows(self.con, 'SELECT * FROM refund_case'),
                    F.revenue(self.con, day, day, 'day'), F.revenue(self.con, day, day, 'month'))

        before = financial_state()
        self.assertEqual(self.post(path, [], self.worker_cookie)[0], 400)
        self.assertEqual(self.post(path, {'status': 'IN_REVIEW', 'basis': '가상 주민정보', 'version': 1}, self.worker_cookie)[0], 400)
        working = {'status': 'IN_REVIEW', 'basis': 'CANCELLATION_CHECKED', 'version': 1}
        self.assertEqual(self.post(path, working, self.worker_cookie)[0], 200)
        self.assertEqual(self.post(path, working, self.worker_cookie)[0], 200)
        resolved = {'status': 'RESOLVED', 'basis': 'LEDGER_CHECKED', 'version': 2}
        self.assertEqual(self.post(path, {**resolved, 'version': 1}, self.admin_cookie)[0], 409)
        self.assertEqual(self.post(path, resolved, self.admin_cookie)[0], 200)
        self.assertNotIn(case['id'], {r['id'] for r in self.get('recon-exceptions')[1]['rows']})
        self.assertIn(case['id'], {r['id'] for r in self.get('recon-exceptions?status=RESOLVED')[1]['rows']})
        code, detail = self.get(path, lead_cookie)
        self.assertEqual(code, 200, detail)
        self.assertEqual(detail['case']['status'], 'RESOLVED')
        self.assertEqual([(x['actor'], x['from'], x['to'], x['basis']) for x in detail['history']],
                         [('session_admin', 'IN_REVIEW', 'RESOLVED', 'LEDGER_CHECKED'),
                          ('session_worker', 'OPEN', 'IN_REVIEW', 'CANCELLATION_CHECKED')])
        self.assertEqual(self.post(path, {'status': 'OPEN', 'basis': 'PAYMENT_RECHECK', 'version': 3}, self.worker_cookie)[0], 200)
        self.assertIn(case['id'], {r['id'] for r in self.get('recon-exceptions')[1]['rows']})
        self.assertEqual(financial_state(), before)
        cid = refund['id']
        self.assertEqual(self.post(f'refunds/{cid}/approve', {'approve': True}, self.worker_cookie)[0], 403)
        self.assertEqual(self.post(f'refunds/{cid}/approve', {'approve': True}, lead_cookie)[0], 200)
        self.assertEqual(S.one(self.con, 'SELECT status FROM refund_case WHERE id=?', (cid,))['status'], 'APPROVED')

    def test_legacy_history_and_exceptions_only(self):
        self.con.execute('''INSERT INTO recon_import (id, sha256, at, actor, rows, matched, summary_json)
                            VALUES ('old', 'old-sha', ?, 'old-actor', 3, 1, ?)''',
                         (S.now(), json.dumps({'MATCH': 1, 'AMOUNT_DIFF': 2})))
        previous = self.get('recon-history')[1][0]
        self.assertEqual((previous['id'], previous['matched'], previous['counts'], previous['exception_count']),
                         ('old', 1, {'MATCH': 1, 'AMOUNT_DIFF': 2}, None))
        self.assertEqual(self.get('recon-exceptions?import_id=old')[1]['rows'], [])
        raw = f'부과번호,금액,납부일\n2099-ZZ-00002,100,{S.today_kst()}\n'.encode()
        status, result = self.upload(raw)
        self.assertEqual(status, 200, result)
        self.assertEqual((result['recorded'], result['exception_count']), (0, 1))
        self.assertEqual(S.rows(self.con, 'SELECT * FROM payment'), [])

    def test_changed_preview_requires_confirmation(self):
        day = S.today_kst()
        receipt = S.create_request(self.con, self.request_body())
        amount = S.payment_status(self.con, receipt['id'])['levied']
        header = '부과번호,금액,납부일\n'
        invalid = f'{header}{receipt["levy_no"]},{amount},2026-02-30\n'.encode()
        status, result = self.upload(invalid)
        self.assertEqual((status, result.get('counts'), result.get('recorded')), (200, {'BAD': 1}, 0))
        self.assertIsNone(self.get('recon-exceptions')[1]['rows'][0]['paid_on'])

        raw = f'{header}{receipt["levy_no"]},{amount - 1},{day}\n'.encode()
        code, preview = self.upload(raw, preview=True)
        self.assertEqual((code, preview.get('counts')), (200, {'AMOUNT_DIFF': 1}))
        S.add_payment(self.con, receipt['id'], {'amount': 1, 'method': '가상계좌', 'paid_on': day})
        self.assertEqual(self.upload(raw, token=preview['preview_token'])[0], 409)
        self.assertEqual(len(self.get('recon-history')[1]), 1)
        self.assertEqual(S.rows(self.con, 'SELECT amount, source FROM payment'), [{'amount': 1, 'source': '수기'}])
        code, preview = self.upload(raw, preview=True)
        self.assertEqual((code, preview.get('counts')), (200, {'MATCH': 1}))
        code, applied = self.upload(raw, token=preview['preview_token'])
        self.assertEqual((code, applied.get('recorded'), applied.get('exception_count')), (200, 1, 0))


class ReconMigrationTest(unittest.TestCase):
    def test_v13_import_summary_survives_v14_migration(self):
        with tempfile.TemporaryDirectory(prefix='dg-recon-v13-') as directory, patch.dict(
            'os.environ', DG_STATE=directory, DG_DB_KEY='recon-migration-test-only', PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring'
        ):
            with patch.object(store, 'LATEST', 13), patch.object(store, 'MIGRATIONS', store.MIGRATIONS[:13]):
                before = store.connect(create=True)
            try:
                self.assertEqual(before.execute('PRAGMA user_version').fetchone()[0], 13)
                before.execute('''INSERT INTO recon_import (id, sha256, at, actor, rows, matched, summary_json)
                                  VALUES ('legacy', 'legacy-sha', ?, 'legacy-actor', 2, 1, ?)''',
                               (S.now(), json.dumps({'MATCH': 1, 'CANCELLED': 1})))
            finally:
                before.close()
            after = store.connect()
            try:
                self.assertGreaterEqual(after.execute('PRAGMA user_version').fetchone()[0], 14)
                summary, = F.recon_history(after)
                self.assertEqual((summary['id'], summary['actor'], summary['rows'], summary['matched'],
                                  summary['counts'], summary['exception_count']),
                                 ('legacy', 'legacy-actor', 2, 1, {'MATCH': 1, 'CANCELLED': 1}, None))
                self.assertEqual(F.recon_exceptions(after)['rows'], [])
                self.assertEqual(after.execute('PRAGMA foreign_key_check').fetchall(), [])
            finally:
                after.close()


if __name__ == '__main__':
    unittest.main()
