"""도로 통행 정보의 관리자 권한, 작업 응답과 노선 승인 세대 검사."""
import http.client
import json
import time
from unittest.mock import patch

from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import osrm, routing, service as S


class RoadRulesFixture:
    revision = 'base'
    status = 'READY'

    def local_endpoint(self, *, retry=False):
        return '', self.status

    def summary(self):
        return {'available': True, 'active_id': self.revision, 'previous_id': None,
                'rules': [], 'busy': False, 'pending': None}

    def roads(self, lat, lon):
        return {'roads': [{'id': '11', 'name': '가상 도로', 'distance_m': 3}]}

    def preview(self, body):
        return {'preview_id': 'test-preview', 'base_id': body['base_id']}

    def apply(self, body, guard):
        assert body['preview_id'] == 'test-preview'
        with guard():
            self.revision = 'tested'
        return self.summary()

    def rollback(self, body, guard):
        assert body['active_id'] == self.revision
        with guard():
            self.revision = 'base'
        return self.summary()


class RoadRulesAPITest(SessionHTTPTestCase):
    def get(self, path, cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request('GET', '/api/' + path, headers={'Cookie': cookie or self.admin_cookie, 'X-DG-CSRF': '1'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait_job(self, path, body):
        status, result, _ = self.post('road-rules/' + path, body, self.admin_cookie)
        self.assertEqual(status, 200, result)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status, job = self.get('jobs/' + result['job_id'])
            self.assertEqual(status, 200, job)
            if job['status'] == 'DONE':
                return job['result']
            self.assertNotEqual(job['status'], 'FAILED', job)
            time.sleep(.05)
        self.fail('도로 작업 응답 대기 시간 초과')

    def test_management_routes_require_admin_and_local_bundled_mode(self):
        fixture = RoadRulesFixture()
        with patch.object(osrm, 'bundled', fixture):
            self.assertEqual(self.get('road-rules', self.worker_cookie)[0], 403)
            self.assertEqual(self.get('road-rules/roads?lat=35.15&lon=126.93', self.worker_cookie)[0], 403)
            self.assertEqual(self.post('road-rules/preview', {}, self.worker_cookie)[0], 403)
            self.assertEqual(self.get('road-rules'), (200, fixture.summary()))
            self.assertEqual(self.get('road-rules/roads?lat=nan&lon=126.93')[0], 400)
            self.assertEqual(self.get('road-rules/roads?lat=35.15&lon=126.93')[1]['roads'][0]['id'], '11')
            result = self.wait_job('preview', {'road_id': '11', 'base_id': 'base', 'direction': 'forward',
                                               'checked_on': S.today_kst(), 'evidence': '가상 현장 표지'})
            self.assertEqual(result['preview_id'], 'test-preview')
            self.assertEqual(self.wait_job('apply', {'preview_id': result['preview_id']})['active_id'], 'tested')
            self.assertEqual(self.wait_job('rollback', {'active_id': 'tested'})['active_id'], 'base')
            S.save_settings(self.con, {'osrm_url': 'http://127.0.0.1:5000'})
            self.assertFalse(self.get('road-rules')[1]['available'])
            self.assertEqual(self.post('road-rules/apply', {'preview_id': 'test-preview'}, self.admin_cookie)[0], 400)
        self.assertFalse(self.get('road-rules')[1]['available'])

    def test_applied_graph_invalidates_existing_dispatch_review(self):
        fixture = RoadRulesFixture()
        day = S.today_kst()
        crew = S.save_crew(self.con, {'name': '가상 도로정보 팀'})
        request = S.create_request(self.con, {**self.request_body(), 'planned_date': day, 'lat': 35.15, 'lon': 126.93})
        with patch.object(osrm, 'bundled', fixture), patch.object(routing, 'clamp_time_limit', return_value=1):
            status, plan, _ = self.post('route', {'date': day, 'crew_ids': [crew], 'request_ids': [request['id']],
                                                 'time_limit': 1}, self.admin_cookie)
            self.assertEqual(status, 200, plan)
            self.assertEqual(plan['routes'][0]['stops'][0]['request_id'], request['id'])
            status, dong_plan, _ = self.post('dong-work/route', {'date': day, 'dong': 'all', 'crew_ids': [crew]}, self.admin_cookie)
            self.assertEqual(status, 200, dong_plan)
            self.assertEqual(dong_plan['routes'][0]['stops'][0]['request_id'], request['id'])
            fixture.revision = 'new-graph'
            status, _, _ = self.post('assign', {'date': day, 'plan': {crew: plan['routes'][0]['stops']},
                                                'depot_basis': plan['depot_basis']}, self.admin_cookie)
            self.assertEqual(status, 409)
            status, response, _ = self.post('dong-work/assign', {'date': day, 'dong': 'all', 'basis': dong_plan['basis'],
                                                   'plan': {crew: dong_plan['routes'][0]['stops']}}, self.admin_cookie)
            self.assertEqual(status, 409, response)
            self.assertEqual(response.get('code'), 'ROAD_RULES_CHANGED', response)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignment_batch').fetchone()[0], 0)

    def road_case(self, count=1):
        day = S.today_kst()
        crew = S.save_crew(self.con, {'name': '가상 도로정보 팀'})
        ids = [S.create_request(self.con, {**self.request_body(), 'planned_date': day, 'lat': 35.15 + i / 1000, 'lon': 126.93})['id']
               for i in range(count)]
        return day, crew, ids

    def test_changed_graph_has_stable_code_and_keep_order_review_uses_new_graph(self):
        fixture = RoadRulesFixture()
        day, crew, (rid,) = self.road_case()
        with patch.object(osrm, 'bundled', fixture), patch.object(routing, 'clamp_time_limit', return_value=1):
            status, plan, _ = self.post('route', {'date': day, 'crew_ids': [crew], 'time_limit': 1}, self.admin_cookie)
            self.assertEqual(status, 200, plan)
            order = [s['request_id'] for s in plan['routes'][0]['stops']]
            self.assertEqual(order, [rid])
            fixture.revision = 'new-graph'
            status, value, _ = self.post('assign', {'date': day, 'plan': {crew: plan['routes'][0]['stops']},
                                                    'depot_basis': plan['depot_basis']}, self.admin_cookie)
            self.assertEqual((status, value.get('code')), (409, 'ROAD_RULES_CHANGED'), value)
            self.assertTrue(value.get('stale'))
            # 순서 유지 재계산은 예전 계산 근거로도 새 도로망에서 허용되고, 새 근거로는 확정된다.
            status, reviewed, _ = self.post('route', {'date': day, 'crew_ids': [crew], 'time_limit': 1, 'request_ids': [rid],
                                                      'fixed_plan': {crew: order}, 'depot_basis': plan['depot_basis']}, self.admin_cookie)
            self.assertEqual(status, 200, reviewed)
            self.assertEqual([s['request_id'] for s in reviewed['routes'][0]['stops']], order)
            status, value, _ = self.post('assign', {'date': day, 'plan': {crew: reviewed['routes'][0]['stops']},
                                                    'depot_basis': reviewed['depot_basis']}, self.admin_cookie)
            self.assertEqual(status, 200, value)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM assignment_batch WHERE status='ACTIVE'").fetchone()[0], 1)

    def test_graph_change_during_calculation_is_reported_before_response(self):
        fixture = RoadRulesFixture()
        day, crew, _ = self.road_case()
        original = routing.plan

        def switched(*args, **kwargs):
            out = original(*args, **kwargs)
            fixture.revision = 'switched-during-calculation'
            return out

        for path, body in (('route', {'date': day, 'crew_ids': [crew], 'time_limit': 1}),
                           ('dong-work/route', {'date': day, 'dong': 'all', 'crew_ids': [crew]})):
            with self.subTest(path=path):
                fixture.revision = 'base'
                with patch.object(osrm, 'bundled', fixture), patch.object(routing, 'clamp_time_limit', return_value=1),                         patch.object(routing, 'plan', side_effect=switched):
                    status, value, _ = self.post(path, body, self.admin_cookie)
                self.assertEqual((status, value.get('code')), (409, 'ROAD_RULES_CHANGED'), value)
                self.assertNotIn('requests', value)
                self.assertNotIn('routes', value)

    def test_changed_graph_blocks_reviewed_assignment_edit_with_stable_code(self):
        fixture = RoadRulesFixture()
        day, crew, ids = self.road_case(2)
        with patch.object(osrm, 'bundled', fixture), patch.object(routing, 'clamp_time_limit', return_value=1):
            S.confirm_plan(self.con, day, {crew: [{'request_id': rid} for rid in ids]})
            status, review, _ = self.post('route', {'date': day, 'time_limit': 1, 'assignment_change': {
                'op': 'reorder', 'crew_id': crew, 'order': ids[::-1], 'assign_ver': {crew: 1}, 'expected_order': {crew: ids}}},
                self.admin_cookie)
            self.assertEqual(status, 200, review)
            self.assertIn('review_basis', review)
            fixture.revision = 'new-graph'
            status, value, _ = self.post('assign/edit', {'date': day, 'review_basis': review['review_basis'], 'reason': '가상 순서 변경'},
                                         self.admin_cookie)
            self.assertEqual((status, value.get('code')), (409, 'ROAD_RULES_CHANGED'), value)
        self.assertEqual(self.con.execute('SELECT MAX(assign_ver) FROM assignment_batch').fetchone()[0], 1)


if __name__ == '__main__':
    import unittest
    unittest.main()
