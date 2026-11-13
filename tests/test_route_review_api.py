"""임시 암호화 DB/HTTP: 순서 검토와 계산 후 자료 변경을 검증한다."""
import unittest
import datetime as dt
from unittest.mock import patch
import test_dong_depots_api as depots
import test_label_usage_api as usage
from test_pc_session_revocation import SessionHTTPTestCase
from dgpc import core as C, service as S


class RouteReviewAPITests(depots.DepotAPITests):
    def recommendation(self):
        result = self.route('route')
        plan = {r['crew_id']: [{'request_id': s['request_id']} for s in r['stops']] for r in result['routes']}
        return result, plan

    def test_fixed_review_reports_the_fixed_plan_and_required_crews(self):
        result, plan = self.recommendation()
        status, reviewed, _ = self.post('route', {'date': self.day, 'crew_ids': [self.a, self.b], 'time_limit': 1,
            'fixed_plan': {cid: [s['request_id'] for s in stops] for cid, stops in plan.items()}, 'depot_basis': result['depot_basis']}, self.admin_cookie)
        self.assertEqual(status, 200, reviewed)
        self.assertTrue(reviewed.get('fixed_order'))
        self.assertEqual(reviewed['requests'][self.ra]['required_crew_id'], self.a)

    def test_changed_request_invalidates_old_confirmation(self):
        result, plan = self.recommendation()
        self.con.execute('UPDATE request SET req_ver=req_ver+1 WHERE id=?', (self.ra,))
        status, value, _ = self.post('assign', {'date': self.day, 'plan': plan, 'depot_basis': result['depot_basis']}, self.admin_cookie)
        self.assertEqual(status, 409, value)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assignment_batch').fetchone()[0], 0)

    def test_changed_work_settings_invalidate_old_confirmation(self):
        result, plan = self.recommendation()
        C.set_setting(self.con, 'service_min_base', S.settings(self.con)['service_min_base'] + 1)
        status, value, _ = self.post('assign', {'date': self.day, 'plan': plan, 'depot_basis': result['depot_basis']}, self.admin_cookie)
        self.assertEqual(status, 409, value)

    def test_changed_assignment_revision_invalidates_old_confirmation(self):
        result, plan = self.recommendation()
        S.confirm_plan(self.con, self.day, plan)
        status, value, _ = self.post('assign', {'date': self.day, 'plan': plan, 'depot_basis': result['depot_basis']}, self.admin_cookie)
        self.assertEqual(status, 409, value)

    def test_fixed_review_requires_a_matching_candidate_proof(self):
        status, value, _ = self.post('route', {'date': self.day, 'crew_ids': [self.a], 'fixed_plan': {self.a: [self.ra]}}, self.admin_cookie)
        self.assertIn(status, (400, 409), value)

    def test_postponed_excluded_candidate_can_rebase_without_losing_remaining_order(self):
        result, plan = self.recommendation()
        tomorrow = (dt.date.fromisoformat(self.day) + dt.timedelta(days=1)).isoformat()
        status, moved, _ = self.post('requests/bulk', {'action': 'reschedule', 'ids': [self.rb], 'date': tomorrow, 'code': 'OVER_CAPACITY', 'text': '가상 미루기'}, self.admin_cookie)
        self.assertEqual(status, 200, moved)
        status, reviewed, _ = self.post('route', {'date': self.day, 'crew_ids': [self.a, self.b], 'time_limit': 1,
            'fixed_plan': {self.a: [self.ra], self.b: []}, 'depot_basis': result['depot_basis']}, self.admin_cookie)
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(list(reviewed['requests']), [self.ra])
        self.assertEqual([s['request_id'] for s in reviewed['routes'][0]['stops']], [self.ra])

    def test_imported_phone_result_or_location_invalidates_route_without_req_ver_change(self):
        for kind in ('ITEM_RESULT', 'LOCATION_FIX'):
            with self.subTest(kind=kind):
                request = S.get_request(self.con, self.ra)
                seal, event = usage.UsageHTTPCase.phone(self, request)
                result, plan = self.recommendation()
                body = {'line_id': request['items'][0]['line_id'], 'qty_confirmed': 2, 'qty_collected': 1} if kind == 'ITEM_RESULT' else {'lat': 35.141, 'lon': 126.921, 'source': 'ADDR'}
                status, imported, _ = SessionHTTPTestCase.post(self, 'import/result', seal([event(kind, body)]), self.admin_cookie)
                self.assertEqual(status, 200, imported)
                self.assertEqual(S.get_request(self.con, self.ra)['req_ver'], request['req_ver'])
                status, value, _ = self.post('assign', {'date': self.day, 'plan': plan, 'depot_basis': result['depot_basis']}, self.admin_cookie)
                self.assertEqual(status, 409, value)

    def test_all_daily_candidates_are_loaded_beyond_first_500(self):
        for i in range(500):
            S.create_request(self.con, {**self.request_body(), 'name': '가상 대량 후보 ' + str(i), 'dong': '가상A동', 'planned_date': self.day})
        from dgpc import server as SV
        with patch.object(SV.routing, 'plan', return_value={'routes': [], 'unassigned': []}) as solver:
            result = self.route('route')
            self.assertEqual(len(solver.call_args.args[0]), 502)
            self.assertEqual(len(result['requests']), 502)


if __name__ == '__main__':
    unittest.main()
