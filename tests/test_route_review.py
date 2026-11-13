"""실제 OR-Tools: 수동 순서 재계산이 선택·시간·적재 조건을 보존한다."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'pc'))
from dgpc import routing
from dgpc.core import UserError


class RouteReviewTests(unittest.TestCase):
    def setUp(self):
        self.depot = {'lat': 35.14, 'lon': 126.90, 'label': '가상 차고지'}
        self.crews = [{'id': cid, 'name': cid, 'capacity_m3': 1, 'shift_start': '09:00', 'shift_end': '17:00'} for cid in ('a', 'b')]
        self.requests = [{'id': 'r' + str(i), 'lat': 35.14 + i * .001, 'lon': 126.90, 'items': []} for i in range(1, 4)]

    def plan(self, fixed, **options):
        return routing.plan(self.requests, self.crews, self.depot, fixed_plan=fixed, service_base=5, service_per_item=0,
                            time_limit_s=1, travel_method='ESTIMATE', **options)

    def test_order_and_team_are_preserved_and_summary_includes_return(self):
        result = self.plan({'a': ['r2', 'r1'], 'b': ['r3']})
        self.assertFalse(result.get('infeasible'))
        for route in result['routes']:
            expected = ['r2', 'r1'] if route['crew_id'] == 'a' else ['r3']
            self.assertEqual([s['request_id'] for s in route['stops']], expected)
            self.assertGreater(route['distance_km'], 0)
            self.assertGreaterEqual(route['duration_min'], route['drive_min'] + route['work_min'])
            self.assertEqual(route['work_min'], len(expected) * 5)

    def test_same_place_members_keep_the_manual_order(self):
        for row in self.requests:
            row.update(lat=35.14, lon=126.90)
        result = self.plan({'a': ['r3', 'r1', 'r2'], 'b': []})
        self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], ['r3', 'r1', 'r2'])

    def test_excluded_stops_are_not_added_back(self):
        result = self.plan({'a': ['r1'], 'b': []})
        self.assertEqual([s['request_id'] for r in result['routes'] for s in r['stops']], ['r1'])
        self.assertEqual({r['request_id'] for r in result['unassigned']}, {'r2', 'r3'})

    def test_capacity_failure_does_not_silently_drop_a_selected_stop(self):
        for row in self.requests:
            row['items'] = [{'name': '가상 물건', 'fee_rate_id': 'fixture', 'qty': 1}]
        result = self.plan({'a': ['r1', 'r2'], 'b': ['r3']}, volume_overrides={'fixture': .7})
        self.assertTrue(result.get('infeasible'))
        self.assertFalse(result['routes'])

    def test_unload_can_be_inserted_without_changing_order(self):
        for row in self.requests:
            row['items'] = [{'name': '가상 물건', 'fee_rate_id': 'fixture', 'qty': 1}]
        result = self.plan({'a': ['r2', 'r1'], 'b': ['r3']}, volume_overrides={'fixture': .7}, unload_site=self.depot, unload_min=5, max_unloads=2)
        self.assertFalse(result.get('infeasible'))
        route = next(r for r in result['routes'] if r['crew_id'] == 'a')
        self.assertEqual([s['request_id'] for s in route['stops']], ['r2', 'r1'])
        self.assertEqual(route['unloads'], 1)

    def test_time_windows_cannot_reverse_or_drop_selected_stops(self):
        self.requests[0].update(appt_start='09:00', appt_end='09:10')
        self.requests[1].update(appt_start='10:00', appt_end='10:10')
        self.assertTrue(self.plan({'a': ['r2', 'r1'], 'b': ['r3']}).get('infeasible'))

    def test_invalid_manual_plan_and_fixed_team_violation_are_rejected(self):
        for value in ({'a': ['r1', 'r1']}, {'missing': ['r1']}, {'a': ['missing']}, {'a': 'r1'}, [], {'a': []}):
            with self.subTest(value=value), self.assertRaises(UserError):
                self.plan(value)
        with self.assertRaises(UserError):
            self.plan({'a': ['r1'], 'b': []}, crew_required={'r1': 'b'})

    def test_service_longer_than_appointment_window_is_infeasible(self):
        self.requests[0].update(lat=self.depot['lat'], lon=self.depot['lon'], appt_start='09:00', appt_end='09:02')
        self.assertTrue(self.plan({'a': ['r1'], 'b': []}).get('infeasible'))

    def test_automatic_recommendation_excludes_impossible_service_window(self):
        self.requests = [{**self.requests[0], 'lat': self.depot['lat'], 'lon': self.depot['lon'], 'appt_start': '09:00', 'appt_end': '09:02'}]
        result = self.plan(None)
        self.assertFalse(any(route['stops'] for route in result['routes']))
        self.assertIn('약속', result['unassigned'][0]['reason'])

    def test_colocated_individually_feasible_visits_are_not_lost_by_grouping(self):
        self.requests = [{**row, 'lat': self.depot['lat'], 'lon': self.depot['lon'], 'appt_start': '09:00', 'appt_end': '09:05'} for row in self.requests[:2]]
        result = self.plan(None)
        self.assertEqual(sum(len(route['stops']) for route in result['routes']), 2)
        self.assertEqual(result['unassigned'], [])

    def test_unload_longer_than_opening_window_is_infeasible(self):
        for row in self.requests:
            row['items'] = [{'name': '가상 물건', 'fee_rate_id': 'fixture', 'qty': 1}]
        result = self.plan({'a': ['r1', 'r2'], 'b': ['r3']}, volume_overrides={'fixture': .7}, unload_site=self.depot,
            unload_min=5, max_unloads=2, unload_hours={'start': '10:00', 'end': '10:02'})
        self.assertTrue(result.get('infeasible'))


if __name__ == '__main__':
    unittest.main()
