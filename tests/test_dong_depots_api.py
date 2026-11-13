"""실제 SQLCipher/HTTP에서 전역·동별 차고지, 권한과 오래된 배정을 검증한다."""
import http.client
import json
import threading
import unittest
from unittest.mock import patch
from test_pc_session_revocation import SessionHTTPTestCase
from test_dong_work import DongWorkHTTPTest
from dgpc import area as A, core as C, server as SV, service as S


class DepotAPITests(SessionHTTPTestCase):
    waiting_post = DongWorkHTTPTest.waiting_post
    def post(self, path, body=None, cookie=None, extra_headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=20)
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
        self.a = S.save_crew(self.con, {'name': '가상 A동 전담팀'})
        self.b = S.save_crew(self.con, {'name': '가상 B동 전담팀'})
        self.pa = {'lat': 35.14, 'lon': 126.90, 'label': '가상 A 차고지'}
        self.pb = {'lat': 35.20, 'lon': 127.00, 'label': '가상 B 차고지'}
        self.da = A.save_dong(self.con, {'name': '가상A동', 'crew_id': self.a, 'depot': self.pa})
        self.db = A.save_dong(self.con, {'name': '가상B동', 'crew_id': self.b, 'depot': self.pb})
        self.ra = S.create_request(self.con, {**self.request_body(), 'dong': '가상A동', 'planned_date': self.day, 'lat': self.pa['lat'], 'lon': self.pa['lon']})['id']
        self.rb = S.create_request(self.con, {**self.request_body(), 'dong': '가상B동', 'planned_date': self.day, 'lat': self.pb['lat'], 'lon': self.pb['lon']})['id']

    def route(self, path='dong-work/route', **body):
        status, result, _ = self.post(path, {'date': self.day, 'dong': 'all', 'crew_ids': [self.a, self.b], 'time_limit': 1, **body}, self.admin_cookie)
        self.assertEqual(status, 200, result)
        return result

    def test_global_and_dong_route_both_start_from_each_teams_garage(self):
        for path in ('route', 'dong-work/route'):
            with self.subTest(path=path):
                result = self.route(path)
                by_crew = {r['crew_id']: r for r in result['routes']}
                self.assertEqual(by_crew[self.a].get('depot'), self.pa)
                self.assertEqual(by_crew[self.b].get('depot'), self.pb)
                self.assertEqual([s['request_id'] for s in by_crew[self.a]['stops']], [self.ra])
                self.assertEqual([s['request_id'] for s in by_crew[self.b]['stops']], [self.rb])

    def test_other_team_does_not_take_dongs_fixed_crew_work(self):
        result = self.route(dong=self.da, crew_ids=[self.b])
        self.assertEqual([x['request_id'] for x in result['unassigned']], [self.ra])
        self.assertFalse(any(r['stops'] for r in result['routes']))

    def test_changed_depot_invalidates_scoped_assignment(self):
        result = self.route(dong=self.da, crew_ids=[self.a])
        A.save_dong(self.con, {'id': self.da, 'name': '가상A동', 'crew_id': self.a, 'depot': {**self.pa, 'lat': 35.141}})
        status, value, _ = self.post('dong-work/assign', {'date': self.day, 'dong': self.da, 'basis': result['basis'], 'plan': {self.a: [{'request_id': self.ra}]}}, self.admin_cookie)
        self.assertEqual(status, 409, value)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignment_batch').fetchone()[0], 0)

    def test_depot_save_permission_and_manual_wrong_team_are_enforced(self):
        self.assertEqual(self.post('dongs', {'id': self.da, 'name': '가상A동', 'crew_id': self.a, 'depot': self.pa}, self.worker_cookie)[0], 403)
        status, value, _ = self.post('assign', {'date': self.day, 'plan': {self.b: [{'request_id': self.ra}]}, 'adjusted': True, 'adjust_reason': '가상 잘못된 팀'}, self.admin_cookie)
        self.assertIn(status, (400, 409), value)

    def test_manual_edit_cannot_move_fixed_work_to_another_dongs_team(self):
        status, assigned, _ = self.post('assign', {'date': self.day, 'plan': {self.a: [{'request_id': self.ra}]}}, self.admin_cookie)
        self.assertEqual(status, 200, assigned)
        before = self.con.execute('SELECT * FROM assignment_batch ORDER BY id').fetchall()
        status, value, _ = self.post('assign/edit', {'date': self.day, 'op': 'move', 'request_id': self.ra,
            'to_crew_id': self.b, 'assign_ver': {self.a: 1}, 'reason': '가상 팀 이동'}, self.admin_cookie)
        self.assertIn(status, (400, 409), value)
        self.assertEqual(self.con.execute('SELECT * FROM assignment_batch ORDER BY id').fetchall(), before)

    def test_changed_depot_invalidates_global_calculation_confirmation(self):
        result = self.route('route')
        self.assertIn('depot_basis', result)
        A.save_dong(self.con, {'id': self.da, 'name': '가상A동', 'crew_id': self.a, 'depot': {**self.pa, 'lat': 35.141}})
        plan = {r['crew_id']: [{'request_id': s['request_id']} for s in r['stops']] for r in result['routes']}
        status, value, _ = self.post('assign', {'date': self.day, 'plan': plan, 'depot_basis': result['depot_basis']}, self.admin_cookie)
        self.assertEqual(status, 409, value)

    def test_logout_during_global_calculation_returns_no_resident_data(self):
        entered, release = threading.Event(), threading.Event()
        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timed out')
            return {'routes': [], 'unassigned': []}
        responses = []
        with patch.object(SV.routing, 'plan', side_effect=paused):
            thread = threading.Thread(target=lambda: responses.append(self.post('route', {'date': self.day, 'crew_ids': [self.a]}, self.worker_cookie)))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.post('logout', {}, self.worker_cookie)[0], 200)
            finally:
                release.set(); thread.join(10)
        self.assertEqual(responses[0][0], 401)
        self.assertNotIn('requests', responses[0][1])

    def test_logout_during_global_body_read_never_starts_solver(self):
        with patch.object(SV.routing, 'plan') as solver:
            with self.waiting_post('route', {'date': self.day, 'crew_ids': [self.a]}, self.worker_cookie) as finish:
                self.assertEqual(self.post('logout', {}, self.worker_cookie)[0], 200)
                self.assertEqual(finish()[0], 401)
            solver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
