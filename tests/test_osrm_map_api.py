"""PC 지도용 OSRM 중계와 확정 배정 메타데이터. 임시 SQLCipher와 로컬 가상 OSRM."""
import http.client
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import parse_qs, urlsplit

from test_pc_session_revocation import SessionHTTPTestCase
from test_osrm_runtime import loopback
from dgpc import area, privacy, server, service as S

DEPOT = {'lat': 35.147, 'lon': 126.919, 'label': '가상 동 차고지'}
UNLOAD = {'lat': 35.154, 'lon': 126.929, 'label': '가상 반입지'}
END = {'lat': 35.149, 'lon': 126.933, 'label': '가상 종료지'}


def point(location):
    return [location['lon'], location['lat']]


class RouteMapHTTPTest(SessionHTTPTestCase):
    post_timeout = 20

    def test_no_route_blocks_global_dong_and_changed_assignment(self):
        day = '2099-03-02'
        crew = S.save_crew(self.con, {'name': '가상 통행 검토팀', 'shift_end': '23:00'})
        S.save_settings(self.con, {'depot': DEPOT, 'unload_site': UNLOAD, 'external': {'travel_method': 'OSRM'}})
        visit = S.create_request(self.con, {**self.request_body(), 'planned_date': day, 'lat': 35.15, 'lon': 126.925})
        base = {'date': day, 'crew_ids': [crew], 'time_limit': 1}
        outcome = {'routes': [{'crew_id': crew, 'crew_name': '가상 통행 검토팀', 'depot': DEPOT, 'start': '10:00',
                               'end': '10:40', 'stops': [{'request_id': visit['id'], 'eta': '10:10',
                                   'unload_after': {'label': UNLOAD['label'], 'eta': '10:20', 'final': True}}]}],
                   'unassigned': [], 'matrix_source': 'OSRM', 'unload_site': UNLOAD}
        with loopback(lambda _: (400, {}, {'code': 'NoRoute'})) as (url, cuts), \
                patch.object(S, 'local_osrm', return_value=(url, 'CONFIGURED')), \
                patch.object(server.routing, 'plan', side_effect=lambda *args, **kwargs: deepcopy(outcome)):
            for endpoint, confirm_endpoint in (('route', 'assign'), ('dong-work/route', 'dong-work/assign')):
                with self.subTest(endpoint=endpoint):
                    status, result, _ = self.post(endpoint, base, self.admin_cookie)
                    self.assertEqual(status, 200, result)
                    plan = {r['crew_id']: r['stops'] for r in result['routes']}
                    proof = {'depot_basis': result['depot_basis']} if endpoint == 'route' else {'basis': result['basis']}
                    status, denied, _ = self.post(confirm_endpoint, {'date': day, 'plan': plan, **proof}, self.admin_cookie)
                    self.assertEqual(status, 409, denied)
                    self.assertEqual(denied.get('code'), 'ROUTE_CONNECTION_BLOCKED', denied)
                    self.assertEqual(result.get('route_blockers', [])[0]['crew_id'], crew)
                    self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignment_batch').fetchone()[0], 0)
            status, denied, _ = self.post('assign', {'date': day, 'plan': plan}, self.admin_cookie)
            self.assertEqual(status, 409, denied)
            self.assertIn('도로', denied['error'])
            batch, = S.confirm_plan(self.con, day, {crew: [{'request_id': visit['id']}]})
            status, review, _ = self.post('route', {'date': day, 'time_limit': 1, 'assignment_change': {
                'op': 'reorder', 'crew_id': crew, 'order': [visit['id']], 'recalculate': True, 'start_after': '10:00',
                'assign_ver': {crew: batch['assign_ver']}, 'expected_order': {crew: [visit['id']]},
            }}, self.admin_cookie)
            self.assertEqual(status, 200, review)
            self.assertNotIn('review_basis', review)
            self.assertEqual(review['route_blockers'][0]['crew_id'], crew)
        segment = ';126.929000,35.154000;126.919000,35.147000'
        self.assertTrue(all(urlsplit(c).path.endswith(segment) for c in cuts), cuts)

    def test_unavailable_route_keeps_estimate_but_altered_plan_is_rejected(self):
        day = '2099-03-02'
        crew = S.save_crew(self.con, {'name': '가상 추정팀', 'shift_end': '23:00'})
        S.save_settings(self.con, {'depot': DEPOT, 'end_site': END, 'external': {'travel_method': 'OSRM'}})
        S.create_request(self.con, {**self.request_body(), 'planned_date': day, 'lat': 35.15, 'lon': 126.925})
        with loopback(lambda _: (503, {}, {})) as (url, _), patch.object(S, 'local_osrm', return_value=(url, 'CONFIGURED')):
            status, result, _ = self.post('route', {'date': day, 'crew_ids': [crew], 'time_limit': 2}, self.admin_cookie)
            self.assertEqual(status, 200, result)
            route, = result['routes']
            self.assertEqual(route['road_summary']['source'], 'ESTIMATE')
            self.assertFalse(result.get('route_blockers'))
            payload = {'date': day, 'depot_basis': result['depot_basis']}
            altered = [{**s, 'eta': '23:59'} for s in route['stops']]
            status, denied, _ = self.post('assign', {**payload, 'plan': {crew: altered}}, self.admin_cookie)
            self.assertEqual(status, 409, denied)
            status, saved, _ = self.post('assign', {**payload, 'plan': {crew: route['stops']}}, self.admin_cookie)
            self.assertEqual(status, 200, saved)
            batch, = self.batches(day, route_map=True)
            self.assertEqual(batch['map_route']['history']['path']['source'], 'ESTIMATE')

    def test_confirmed_route_keeps_the_calculated_geometry_and_scrubs_it_on_purge(self):
        day = '2099-03-02'
        crew = S.save_crew(self.con, {'name': '가상 이력팀', 'shift_end': '23:00'})
        S.save_settings(self.con, {'depot': DEPOT, 'end_site': END})
        location = {'lat': 35.15, 'lon': 126.925}
        receipt = S.create_request(self.con, {**self.request_body(), 'planned_date': day, **location})
        points = list(map(point, [DEPOT, location, END]))
        shape = {'type': 'LineString', 'coordinates': points}
        path = {'source': 'OSRM', 'distance_km': 4.23, 'geometry': shape, 'toll_excluded': True}
        S.save_settings(self.con, {'external': {'travel_method': 'OSRM'}})
        with patch.object(S, 'managed_road_revision', return_value='trial-a'), patch.object(S, 'local_osrm', return_value=('http://127.0.0.1:9', 'READY')), \
                patch.object(server.routing, 'build_matrix', return_value=([[0, 5, 7], [5, 0, 6], [8, 6, 0]], 'OSRM',
                             {'_osrm_estimate_mask': bytes([2, 0]), 'fallback_pairs': 1})), \
                patch.object(server.routing, 'road_path', return_value=path) as lookup:
            status, result, _ = self.post('route', {'date': day, 'crew_ids': [crew], 'time_limit': 2}, self.admin_cookie)
            self.assertEqual(status, 200, result)
            plan = {route['crew_id']: [{k: s[k] for k in ('request_id', 'eta')} for s in route['stops']] for route in result['routes']}
            status, saved, _ = self.post('assign', {'date': day, 'plan': plan, 'depot_basis': result['depot_basis']}, self.admin_cookie)
            self.assertEqual(status, 200, saved)
            batch_id = saved[0]['batch_id']
            batch, = self.batches(day, route_map=True)
            self.assertEqual(batch['map_route']['history']['state'], 'CURRENT')
            self.assertEqual(batch['map_route']['history']['path'], path)
            self.assertEqual(batch['map_route']['history']['matrix_source'], 'OSRM')
            self.assertEqual(batch['map_route']['history']['estimated_legs'], ['출발 → 1번'])
            self.assertEqual(batch['stops'][0]['eta'], plan[crew][0]['eta'])
            lookup.assert_called_once()
        with patch.object(S, 'managed_road_revision', return_value='trial-b'), patch.object(server.routing, 'road_path') as lookup:
            batch, = self.batches(day, route_map=True)
            self.assertEqual(batch['map_route']['history']['state'], 'ROAD_CHANGED')
            self.assertEqual(batch['map_route']['history']['path']['geometry'], shape)
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
            try:
                conn.request('GET', f'/print/batch/{batch_id[:8]}?mode=plain', headers={'Cookie': self.admin_cookie})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                page = response.read().decode()
                self.assertIn('확정 당시', page)
                self.assertIn('4.23 km', page)
                self.assertIn('이동시간 직선 추정 구간 출발 → 1번', page)
                self.assertNotIn('class="print-route-arrow"', page)
            finally:
                conn.close()
            lookup.assert_not_called()
        with S.tx(self.con):
            privacy.pseudonymize(self.con, receipt['id'], S.now())
        stored = json.loads(S.one(self.con, 'SELECT route_json FROM assignment_batch WHERE id=?', (batch_id,))['route_json'])
        self.assertNotIn('geometry', json.dumps(stored))
        self.assertNotIn('waypoints', json.dumps(stored))

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
