"""PC 지도용 OSRM 중계와 확정 배정 메타데이터. 임시 SQLCipher와 로컬 가상 OSRM."""
import http.client
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import parse_qs, urlsplit

from test_pc_session_revocation import SessionHTTPTestCase
from test_osrm_runtime import loopback
from dgpc import area, service as S

DEPOT = {'lat': 35.147, 'lon': 126.919, 'label': '가상 동 차고지'}
UNLOAD = {'lat': 35.154, 'lon': 126.929, 'label': '가상 반입지'}
END = {'lat': 35.149, 'lon': 126.933, 'label': '가상 종료지'}


def point(location):
    return [location['lon'], location['lat']]


class RouteMapHTTPTest(SessionHTTPTestCase):
    def batches(self, date, *, route_map=False, cookie=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('GET', '/api/batches?date=' + date + ('&route_map=1' if route_map else ''),
                         headers={'Cookie': cookie or self.admin_cookie, 'X-DG-CSRF': '1'})
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            return json.loads(r.read())
        finally:
            conn.close()

    def seed(self):
        day = S.today_kst()
        a = S.save_crew(self.con, {'name': '가상 지도 A팀'})
        b = S.save_crew(self.con, {'name': '가상 지도 B팀'})
        area.save_dong(self.con, {'name': '가상 지도동', 'crew_id': a, 'depot': DEPOT})
        S.save_settings(self.con, {'unload_site': UNLOAD, 'end_site': END})
        locations = [{'lat': 35.148, 'lon': 126.925}, {'lat': 35.151, 'lon': 126.921}, {'lat': 35.155, 'lon': 126.931}]
        receipts = [S.create_request(self.con, {**self.request_body(), **p, 'name': f'가상 지도 {i}', 'dong': '가상 지도동' if i < 2 else ''})
                    for i, p in enumerate(locations)]
        confirmed = S.confirm_plan(self.con, day, {
            a: [{'request_id': receipts[0]['id'], 'unload_after': {'label': UNLOAD['label'], 'eta': '10:30'}}, {'request_id': receipts[1]['id']}],
            b: [{'request_id': receipts[2]['id']}],
        })
        return day, a, b, locations, receipts, confirmed

    def test_valid_path_and_confirmed_sites_follow_current_order(self):
        day, _, _, locations, receipts, confirmed = self.seed()
        aid = confirmed[0]['batch_id']
        points = list(map(point, [DEPOT, locations[0], UNLOAD, locations[1], END]))
        expected_url = '/route/v1/driving/' + ';'.join(f'{lon:.6f},{lat:.6f}' for lon, lat in points)
        shape = {'type': 'LineString', 'coordinates': [points[0], [126.919, 35.149], *points[1:]]}
        result = {'code': 'Ok', 'routes': [{'distance': 2750, 'geometry': shape}]}
        before = S.rows(self.con, 'SELECT * FROM assignment_batch ORDER BY rowid')
        with loopback(lambda _: (200, {}, result)) as (url, calls):
            S.save_settings(self.con, {'osrm_url': url, 'external': {'travel_method': 'OSRM'}})
            status, data, _ = self.post('route-path', {'batch_id': aid, 'points': points}, self.admin_cookie)
            self.assertEqual(status, 200, data)
            self.assertEqual(data, {'source': 'OSRM', 'distance_km': 2.75, 'geometry': shape, 'toll_excluded': True})
            self.assertEqual(len(calls), 1)
            self.assertEqual(urlsplit(calls[0]).path, expected_url)
            self.assertIn('geometries=geojson', calls[0])
            self.assertEqual(parse_qs(urlsplit(calls[0]).query).get('exclude'), ['toll'])
        plain, = [row for row in self.batches(day) if row['id'] == aid]
        self.assertNotIn('map_route', plain)
        batches = self.batches(day, route_map=True)
        batch, = [row for row in batches if row['id'] == aid]
        self.assertEqual(batch['map_route'], {'depot': DEPOT, 'end': END, 'complete': True})
        self.assertEqual([s['request_id'] for s in batch['stops']], [r['id'] for r in receipts[:2]])
        self.assertEqual(batch['stops'][0]['via'], UNLOAD)
        self.assertNotIn('via', batch['stops'][1])
        self.assertEqual(S.rows(self.con, 'SELECT * FROM assignment_batch ORDER BY rowid'), before)
        self.assertEqual(S.rows(self.con, 'SELECT * FROM transfer_bundle'), [])

    def test_roles_bad_geometry_and_network_failure_are_bounded(self):
        _, a, _, locations, _, confirmed = self.seed()
        S.create_user(self.con, 'map_field', '가상 지도 현장팀', 'FIELD', 'map-field-test-123', crew_id=a)
        field = self.login('map_field', 'map-field-test-123')
        points = list(map(point, [DEPOT, locations[0], END]))
        returned = {'code': 'Ok', 'routes': [{'distance': 2750, 'geometry': {'type': 'LineString', 'coordinates': [[126.9, 190], points[-1]]}}]}
        with loopback(lambda _: (200, {}, returned)) as (url, calls):
            S.save_settings(self.con, {'osrm_url': url, 'external': {'travel_method': 'OSRM'}})
            self.assertEqual(self.post('route-path', {'points': points}, field)[0], 403)
            self.assertEqual(self.post('route-path', {'batch_id': confirmed[1]['batch_id'], 'points': points}, field)[0], 403)
            self.assertEqual(self.post('route-path', {'points': [[126.9, True], points[0]]}, self.worker_cookie)[0], 400)
            self.assertEqual(self.post('route-path', {'points': points * 200}, self.worker_cookie)[0], 400)
            self.assertEqual(calls, [])
            code, result, _ = self.post('route-path', {'batch_id': confirmed[0]['batch_id'], 'points': points}, field)
            self.assertEqual((code, result['source'], len(calls)), (200, 'ESTIMATE', 1))
        code, result, _ = self.post('route-path', {'points': points}, self.worker_cookie)
        self.assertEqual((code, result['source']), (200, 'ESTIMATE'))
        with loopback(lambda _: (400, {}, {'code': 'NoRoute'})) as (url, calls):
            S.save_settings(self.con, {'osrm_url': url})
            code, result, _ = self.post('route-path', {'points': points}, self.worker_cookie)
            self.assertEqual((code, result), (200, {'source': 'ESTIMATE', 'reason': 'NoRoute'}))
            S.save_settings(self.con, {'external': {'travel_method': 'ESTIMATE'}})
            code, result, _ = self.post('route-path', {'points': points}, self.worker_cookie)
            self.assertEqual((code, result['source'], len(calls)), (200, 'ESTIMATE', 1))

    def test_print_rejects_a_stop_changed_during_road_lookup(self):
        _, _, _, _, receipts, confirmed = self.seed()
        batch_id = confirmed[0]['batch_id']
        entered, finish = Event(), Event()

        def road_reply(_):
            entered.set()
            finish.wait(4)
            return 400, {}, {'code': 'NoRoute'}

        def print_sheet():
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=8)
            try:
                conn.request('GET', f'/print/batch/{batch_id[:8]}?mode=plain', headers={'Cookie': self.admin_cookie})
                response = conn.getresponse()
                return response.status, response.read().decode()
            finally:
                conn.close()

        with loopback(road_reply) as (url, _), ThreadPoolExecutor(max_workers=1) as executor:
            S.save_settings(self.con, {'osrm_url': url, 'external': {'travel_method': 'OSRM'}})
            printing = executor.submit(print_sheet)
            try:
                self.assertTrue(entered.wait(3), '배차표 도로 조회를 시작하지 않았습니다.')
                S.cancel_request(self.con, receipts[0]['id'], code='RESIDENT')
            finally:
                finish.set()
            status, body = printing.result(timeout=6)
        self.assertEqual(status, 409)
        self.assertIn('배차표를 다시 열어 주세요.', json.loads(body)['error'])


if __name__ == '__main__':
    unittest.main()
