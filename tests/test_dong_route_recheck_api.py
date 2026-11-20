"""동별 노선의 확정 전 재검사: 차량·작업 조건, 현장 결과, 활성 배정이 바뀐 낡은 계산안은 새 갱신본을 만들지 않는다.

실제 SQLCipher/HTTP에서 동별 계산(dong-work/route)의 응답 직전 검사와 확정(dong-work/assign)의 쓰기 잠금 안 검사를 본다.
모든 자료는 가상이다.
"""
import http.client
import json
import unittest
import uuid
from unittest.mock import patch

from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import area as A, core as C, server as SV, service as S
from dgpc.assign import build_daylist, list_batches
from dgpc.routing import to_min
import dgproto


UNLOAD = {'lat': 35.153, 'lon': 126.932, 'label': '가상 반입지'}
DEPOT = {'lat': 35.148, 'lon': 126.923, 'label': '가상 차고지'}
TEAM_DEPOT = {'lat': 35.1541239, 'lon': 126.9189899, 'label': '가상 동 차고지'}


class DongRouteRecheckTests(SessionHTTPTestCase):
    def post(self, path, body=None, cookie=None, extra_headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        try:
            headers = {'X-DG-CSRF': '1', 'Content-Type': 'application/json', 'Cookie': cookie or ''}
            headers.update(extra_headers or {})
            connection.request('POST', '/api/' + path, json.dumps(body).encode() if body is not None else None, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), response.getheader('Set-Cookie')
        finally:
            connection.close()

    def setUp(self):
        super().setUp()
        self.day = C.today_kst()
        self.a = S.save_crew(self.con, {'name': '가상 재검사 A팀'})
        self.b = S.save_crew(self.con, {'name': '가상 재검사 B팀'})
        self.dong = A.save_dong(self.con, {'name': '가상재검사동', 'crew_id': self.a})
        self.rid = S.create_request(self.con, {**self.request_body(), 'dong': '가상재검사동', 'planned_date': self.day,
                                               'lat': 35.15, 'lon': 126.93})['id']

    def batches(self):
        return self.con.execute('SELECT COUNT(*) FROM assignment_batch').fetchone()[0]

    def route(self):
        status, result, _ = self.post('dong-work/route', {'date': self.day, 'dong': 'all', 'crew_ids': [self.a]}, self.admin_cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], [self.rid])
        return result

    def assign(self, result):
        plan = {r['crew_id']: r['stops'] for r in result['routes']}
        return self.post('dong-work/assign', {'date': self.day, 'dong': 'all', 'plan': plan, 'basis': result['basis']}, self.admin_cookie)

    def assert_rejected(self, result):
        status, value, _ = self.assign(result)
        self.assertEqual(status, 409, value)
        self.assertTrue(value.get('stale'), value)
        self.assertNotEqual(value.get('code'), 'ROAD_RULES_CHANGED', value)
        self.assertEqual(self.batches(), 0)

    def test_unchanged_dong_plan_still_confirms_once(self):
        result = self.route()
        status, value, _ = self.assign(result)
        self.assertEqual(status, 200, value)
        self.assertEqual(self.batches(), 1)

    def test_final_unload_is_persisted_for_map_print_and_phone(self):
        S.save_settings(self.con, {'depot': DEPOT, 'unload_site': UNLOAD})
        A.save_dong(self.con, {'id': self.dong, 'name': '가상재검사동', 'crew_id': self.a, 'depot': TEAM_DEPOT})
        stop = {'request_id': self.rid, 'eta': '09:50', 'unload_after': {
            'eta': '10:00', 'label': UNLOAD['label'], 'minutes': 20, 'volume_m3': 0.3, 'final': True,
        }}
        batch, = S.confirm_plan(self.con, self.day, {self.a: [stop]})
        S.save_settings(self.con, {'unload_site': {**UNLOAD, 'lat': 35.158, 'label': '새 가상 반입지'},
                                   'end_site': {'lat': 35.15, 'lon': 126.94, 'label': '다른 가상 종료지'}})
        A.save_dong(self.con, {'id': self.dong, 'name': '가상재검사동', 'crew_id': self.a, 'depot': None})
        active, = list_batches(self.con, self.day, route_map=True)
        self.assertEqual(active['map_route']['end'], TEAM_DEPOT)
        self.assertEqual(active['stops'][0]['via'], UNLOAD)
        self.assertIs(active['stops'][0].get('final_unload'), True)
        daylist, _ = build_daylist(self.con, batch['batch_id'])
        unload = daylist['stops'][0]['unload_after']
        self.assertIs(unload.get('final'), True)
        compact = dgproto.compact_daylist(daylist, SV.AS.FEE_ROWS)
        self.assertEqual(compact['s'][0]['u'], ['10:00', UNLOAD['label'], 20, 0.3, UNLOAD['lat'], UNLOAD['lon']])
        self.assertEqual(compact['s'][0]['f'], [35.154124, 126.91899, 1])
        expanded = dgproto.expand_daylist(compact, SV.AS.FEE_ROWS)['stops'][0]['unload_after']
        self.assertEqual(expanded, {**unload, 'depot': {'lat': 35.154124, 'lon': 126.91899}})
        for invalid in (None, [35, 126], ['35', 126, 1], [35, 181, 1], [0, 0, 1], [35, 126, 0], [35, 126, True]):
            with self.subTest(invalid=invalid):
                payload = {**compact, 's': [{**compact['s'][0], 'f': invalid}]}
                ignored = dgproto.expand_daylist(payload, SV.AS.FEE_ROWS)['stops'][0]['unload_after']
                self.assertEqual(ignored, {key: expanded[key] for key in ('eta', 'label', 'minutes', 'volume_m3', 'lat', 'lon')})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=8)
        try:
            conn.request('GET', f"/print/batch/{batch['batch_id'][:8]}?mode=plain", headers={'Cookie': self.admin_cookie})
            response = conn.getresponse()
            html = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn('최종 하차', html)
            self.assertIn('차고지 복귀', html)
        finally:
            conn.close()

    def test_general_and_dong_plans_schedule_final_unload_before_garage_return(self):
        S.save_settings(self.con, {'depot': DEPOT, 'unload_site': UNLOAD, 'max_unloads': 0,
                                   'unload_min': 20, 'unload_hours': {'start': '10:00', 'end': '16:00'}})
        status, general, _ = self.post('route', {'date': self.day, 'crew_ids': [self.a]}, self.admin_cookie)
        self.assertEqual(status, 200, general)
        for result in (general, self.route()):
            route, = result['routes']
            stop, = route['stops']
            self.assertEqual(route['depot'], DEPOT)
            self.assertTrue(result.get('final_unload_return'))
            unload = stop['unload_after']
            self.assertIs(unload.get('final'), True)
            self.assertGreaterEqual(to_min(unload['eta'], 0), 600)
            self.assertGreater(to_min(route['end'], 0), to_min(unload['eta'], 0) + 20)

    def test_cancelled_last_stop_keeps_final_unload_after_last_retained_visit(self):
        S.save_settings(self.con, {'depot': DEPOT, 'unload_site': UNLOAD})
        last = S.create_request(self.con, {**self.request_body(), 'address': '가상 마지막 수거지',
                                          'planned_date': self.day, 'lat': 35.157, 'lon': 126.937})['id']
        stop = {'request_id': last, 'unload_after': {'label': UNLOAD['label'], 'eta': '11:00', 'final': True}}
        batch, = S.confirm_plan(self.con, self.day, {self.a: [{'request_id': self.rid}, stop]})
        S.cancel_request(self.con, last, code='RESIDENT')
        daylist, _ = build_daylist(self.con, batch['batch_id'])
        self.assertEqual([row['request_id'] for row in daylist['stops']], [self.rid])
        self.assertIs(daylist['stops'][0]['unload_after'].get('final'), True)
        compact = dgproto.compact_daylist(daylist, SV.AS.FEE_ROWS)
        self.assertEqual(compact['s'][0]['f'], [DEPOT['lat'], DEPOT['lon'], 1])
        active, = list_batches(self.con, self.day, route_map=True)
        self.assertEqual(active['map_route']['end'], DEPOT)
        remaining, cancelled = active['stops']
        self.assertIs(cancelled.get('map_skipped'), True)
        self.assertNotIn('via', cancelled)
        self.assertEqual(remaining['via'], UNLOAD)
        self.assertIs(remaining.get('final_unload'), True)

    def test_saved_final_unload_from_older_pc_uses_the_valid_team_depot(self):
        S.save_settings(self.con, {'depot': DEPOT, 'unload_site': UNLOAD})
        stop = {'request_id': self.rid, 'unload_after': {'label': UNLOAD['label'], 'final': True}}
        batch, = S.confirm_plan(self.con, self.day, {self.a: [stop]})
        with SV.LOCK, S.tx(self.con):
            self.con.execute('UPDATE assignment_batch SET route_json=? WHERE id=?', (json.dumps([stop]), batch['batch_id']))
        daylist, _ = build_daylist(self.con, batch['batch_id'])
        compact = dgproto.compact_daylist(daylist, SV.AS.FEE_ROWS)
        self.assertEqual(compact['s'][0]['f'], [DEPOT['lat'], DEPOT['lon'], 1])

    def test_vehicle_capacity_and_shift_change_block_confirm(self):
        for change in ({'capacity_m3': 2}, {'shift_end': '12:00'}, {'capacity_kg': 300}):
            with self.subTest(change=change):
                result = self.route()
                with SV.LOCK:
                    S.save_crew(self.con, {'id': self.a, 'name': '가상 재검사 A팀', **change})
                self.assert_rejected(result)

    def test_work_and_unload_settings_change_block_confirm(self):
        for change in ({'service_min_base': 17}, {'lunch': {'start': '12:00', 'end': '13:00', 'minutes': 40}}):
            with self.subTest(change=change):
                result = self.route()
                with SV.LOCK:
                    S.save_settings(self.con, change)
                self.assert_rejected(result)

    def test_new_field_result_blocks_confirm(self):
        result = self.route()
        with SV.LOCK, S.tx(self.con):
            imp = str(uuid.uuid4())
            self.con.execute("INSERT INTO result_import (id, channel, device_id, package_sha256, imported_at) VALUES (?,?,?,?,?)",
                             (imp, 'MAIL', 'dev-virtual', uuid.uuid4().hex, C.now()))
            self.con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, kind, body_json, apply_status)
                VALUES (?,?,?,?,?,?,?,?,?)""", (str(uuid.uuid4()), imp, 'dev-virtual', 1, self.rid, 1, 'RESULT', '{}', 'CONFLICT'))
        self.assert_rejected(result)

    def test_active_assignment_made_elsewhere_blocks_confirm_as_stale(self):
        result = self.route()
        with SV.LOCK:
            S.confirm_plan(self.con, self.day, {self.b: [{'request_id': self.rid}]})
        before = self.con.execute('SELECT * FROM assignment_batch ORDER BY id').fetchall()
        status, value, _ = self.assign(result)
        self.assertEqual(status, 409, value)
        self.assertEqual(self.con.execute('SELECT * FROM assignment_batch ORDER BY id').fetchall(), before)

    def test_change_during_calculation_is_rejected_before_response(self):
        from dgpc import dong_work as DW
        original = DW.calculate

        def changed(prepared):
            out = original(prepared)
            with SV.LOCK:
                S.save_crew(self.con, {'id': self.a, 'name': '가상 재검사 A팀', 'capacity_m3': 3})
            return out

        with patch.object(SV.DW, 'calculate', side_effect=changed):
            status, value, _ = self.post('dong-work/route', {'date': self.day, 'dong': 'all', 'crew_ids': [self.a]}, self.admin_cookie)
        self.assertEqual(status, 409, value)
        self.assertNotIn('requests', value)
        self.assertNotIn('routes', value)


if __name__ == '__main__':
    unittest.main()
