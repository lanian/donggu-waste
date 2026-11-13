"""센터 차고지 설정의 실제 HTTP 권한·보존·노선 연결."""
import unittest
from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import area as A, core as C, service as S


class CenterDepotAPITests(SessionHTTPTestCase):
    def setUp(self):
        super().setUp()
        self.crew = S.save_crew(self.con, {'name': '가상 센터 전담팀'})
        dong = A.get_dong(self.con, '충장동')
        A.save_dong(self.con, {'id': dong['id'], 'name': '충장동', 'crew_id': self.crew})

    def test_only_admin_can_fill_missing_center_depots(self):
        self.assertIn(self.post('dongs/centers', {}, None)[0], (401, 403))
        self.assertEqual(self.post('dongs/centers', {}, self.worker_cookie)[0], 403)
        self.assertIsNone(A.get_dong(self.con, '충장동')['depot'])
        status, result, _ = self.post('dongs/centers', {}, self.admin_cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(self.post('dongs/centers', {}, self.admin_cookie)[1]['applied'], 0)

    def test_center_is_used_for_route_start_and_return_without_changing_team(self):
        request = S.create_request(self.con, {**self.request_body(), 'dong': '충장동', 'planned_date': C.today_kst(), 'lat': 35.149141, 'lon': 126.912838})
        self.assertEqual(self.post('dongs/centers', {}, self.admin_cookie)[0], 200)
        status, route, _ = self.post('route', {'date': C.today_kst(), 'crew_ids': [self.crew], 'time_limit': 1}, self.admin_cookie)
        self.assertEqual(status, 200, route)
        self.assertEqual(route['routes'][0]['depot']['label'], '충장동 행정복지센터')
        self.assertEqual(route['routes'][0]['crew_id'], self.crew)
        self.assertEqual([row['request_id'] for row in route['routes'][0]['stops']], [request['id']])
        self.assertIsNone(route['end_site'])

    def test_late_retry_does_not_reapply_after_a_newer_manual_change(self):
        body = {'client_req_id': 'center-test-retry'}
        status, result, _ = self.post('dongs/centers', body, self.admin_cookie)
        self.assertEqual(status, 200, result)
        dong = A.get_dong(self.con, '충장동')
        A.save_dong(self.con, {**dong, 'depot': None})
        self.assertEqual(self.post('dongs/centers', body, self.admin_cookie)[1], result)
        self.assertIsNone(A.get_dong(self.con, '충장동')['depot'])


if __name__ == '__main__':
    unittest.main()
