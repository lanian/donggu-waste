"""가져오기별 현장 확인: 정확한 접수 연결·현재 단가·사무실 권한·읽기 전용."""
import hashlib
import json
import unittest
import uuid

import test_label_usage_api as fixture
from dgpc import core as C, results as R, server as SV, service as S


class ImportFieldReviewAPI(fixture.UsageHTTPCase):
    def make_import(self):
        r, line = self.request(qty=2)
        iid = str(uuid.uuid4())
        missing = str(uuid.uuid4())
        events = [
            (r['id'], 'FIELD_INTAKE', 'APPLIED', {'name': '가상비공개주민', 'address': '가상비공개주소'}),
            (r['id'], 'FIELD_ITEM_ADD', 'APPLIED', {'line_id': 'review-added', 'name': '가상 추가 품목', 'spec': '미확정 규격', 'qty': 2,
                'unit': 'EA', 'unit_label': '개', 'price_status': 'PENDING', 'unit_price': None}),
            (missing, 'FIELD_INTAKE', 'CONFLICT', {'name': '가상 충돌 주민', 'address': '가상 충돌 주소'}),
            (r['id'], 'FIELD_ITEM_ADD', 'REJECTED', {'line_id': 'rejected-line', 'name': '거부 품목', 'qty': 1, 'unit_label': '개'}),
        ]
        with C.tx(self.con):
            self.con.execute('INSERT INTO result_import(id,channel,device_id,package_sha256,imported_at) VALUES (?,?,?,?,?)',
                             (iid, 'MAIL', 'review-test-device', str(uuid.uuid4()), C.now()))
            for seq, (rid, kind, status, body) in enumerate(events, 1):
                self.con.execute('''INSERT INTO result_event(event_id,import_id,device_id,device_seq,request_id,assign_ver,base_req_ver,
                    kind,body_json,apply_status,device_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (str(uuid.uuid4()), iid, 'review-test-device', seq, rid, 0, r['req_ver'], kind,
                     json.dumps(body, ensure_ascii=False), status, C.now()))
        return iid, r, line, missing

    def test_grouped_actual_requests_pending_prices_and_no_mutation(self):
        iid, r, line, missing = self.make_import()
        before = [self.con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('request', 'request_version', 'result_event')]
        status, data = self.get('imports/' + iid + '/field-review', self.worker_cookie)
        self.assertEqual(status, 200, data)
        self.assertEqual(data['import_id'], iid)
        grouped = {item['request_id']: item for item in data['items']}
        self.assertEqual(set(grouped), {r['id'], missing})
        current = grouped[r['id']]
        self.assertTrue(current['exists'])
        self.assertEqual(current['receipt_no'], r['receipt_no'])
        self.assertTrue(current['has_field_intake'])
        self.assertTrue(current['has_rejected'])
        self.assertEqual(current['price_pending'], 1)
        self.assertEqual(current['added_items'][0]['name'], '가상 추가 품목')
        self.assertEqual(current['added_items'][0]['qty'], 2)
        self.assertFalse(grouped[missing]['exists'])
        self.assertTrue(grouped[missing]['has_conflict'])
        self.assertEqual(data['counts']['field_intakes'], 1)
        self.assertEqual(data['counts']['added_items'], 1)
        self.assertEqual(data['counts']['conflicts'], 1)
        raw = json.dumps(data, ensure_ascii=False)
        for private in ('가상비공개주민', '가상비공개주소', '가상 충돌 주민', '가상 충돌 주소', '01000009987'):
            self.assertNotIn(private, raw)
        after = [self.con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                 for table in ('request', 'request_version', 'result_event')]
        self.assertEqual(after, before)
        rate = next(row['id'] for row in S.FEE_ROWS.values()
                    if row['status'] == 'CONFIRMED' and row.get('price', 0) > 0 and row['unit_label'] == '개')
        link_id = S.link_similar(self.con, r['id'], 'review-added', rate, '가상 단가 확인 근거', '가상 확인자')
        status, resolved = self.get('imports/' + iid + '/field-review', self.worker_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(next(item for item in resolved['items'] if item['request_id'] == r['id'])['price_pending'], 0)
        self.assertEqual(resolved['counts']['pending_prices'], 0)
        S.unlink_similar(self.con, link_id, '가상 적용 해제')
        status, pending = self.get('imports/' + iid + '/field-review', self.worker_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(pending['counts']['pending_prices'], 1)

    def test_office_only_and_unknown_import(self):
        iid, _, _, _ = self.make_import()
        path = 'imports/' + iid + '/field-review'
        for role in ('ADMIN', 'INTAKE', 'LEAD', 'FIELD', 'DONG'):
            self.assertEqual(SV.allowed(role, 'GET', path), role in ('ADMIN', 'INTAKE', 'LEAD'), role)
        for role in ('LEAD', 'FIELD'):
            name = 'review_' + role.lower()
            S.create_user(self.con, name, '가상 확인 계정', role, 'review-only-test-123')
            cookie = self.login(name, 'review-only-test-123')
            self.assertEqual(self.get(path, cookie)[0], 200 if role == 'LEAD' else 403)
        self.assertEqual(self.get(path)[0], 401)
        self.assertEqual(self.get('imports/unknown/field-review', self.worker_cookie)[0], 404)

    def test_duplicate_response_retains_original_import_id_and_code(self):
        iid, _, _, _ = self.make_import()
        raw = b'field-review-duplicate-fixture'
        digest = hashlib.sha256(raw).hexdigest()
        with C.tx(self.con):
            self.con.execute('UPDATE result_import SET package_sha256=?,release_code=?,summary_json=? WHERE id=?',
                             (digest, 'ABCDEFGH', '{"field_intakes":1}', iid))
        prepared = R.prepare_result(self.con, raw)
        race = {'sha': digest, 'dev_id': 'review-test-device', 'payload': {}, 'release': 'ABCDEFGH'}
        for result in (prepared['duplicate'], R.import_result(self.con, raw), R.import_result(self.con, raw, prepared=race)):
            self.assertEqual(result.get('import_id'), iid)
            self.assertTrue(result['duplicate_file'])
            self.assertEqual(result['release_code'], 'ABCDEFGH')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM result_import WHERE id=?', (iid,)).fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
