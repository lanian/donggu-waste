"""실제 OR-Tools에서 팀별 차고지·고정 담당·하차·복귀를 검증한다."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'pc'))
from dgpc import routing


class RoutingDepotTests(unittest.TestCase):
    def setUp(self):
        self.a = {'lat': 35.14, 'lon': 126.90, 'label': '가상 A 차고지'}
        self.b = {'lat': 35.20, 'lon': 127.00, 'label': '가상 B 차고지'}
        self.common = {'lat': 35.5, 'lon': 127.5, 'label': '가상 공통'}
        self.crews = [{'id': 'a', 'name': 'A팀', 'capacity_m3': 8, 'shift_start': '09:00', 'shift_end': '17:00'},
                      {'id': 'b', 'name': 'B팀', 'capacity_m3': 8, 'shift_start': '09:00', 'shift_end': '17:00'}]

    def req(self, rid, point, **extra):
        return {'id': rid, 'lat': point['lat'], 'lon': point['lon'], 'items': [], 'must_today': True, **extra}

    def plan(self, reqs, **extra):
        return routing.plan(reqs, self.crews, self.common, crew_depots={'a': self.a, 'b': self.b},
                            service_base=0, service_per_item=0, time_limit_s=1, travel_method='ESTIMATE', **extra)

    def test_distinct_starts_meet_early_appointments_and_return_to_own_garage(self):
        reqs = [self.req('ra', self.a, appt_start='09:00', appt_end='09:05'), self.req('rb', self.b, appt_start='09:00', appt_end='09:05')]
        result = self.plan(reqs, crew_required={'ra': 'a', 'rb': 'b'})
        self.assertEqual(result['unassigned'], [])
        routes = {r['crew_id']: r for r in result['routes']}
        for crew, garage in [('a', self.a), ('b', self.b)]:
            self.assertEqual(routes[crew]['depot'], garage)
            self.assertEqual([s['request_id'] for s in routes[crew]['stops']], ['r' + crew])
            self.assertLessEqual(routing.to_min(routes[crew]['end'], 0), 9 * 60 + 5)

    def test_same_place_requests_for_different_fixed_teams_are_not_grouped(self):
        result = self.plan([self.req('ra', self.a), self.req('rb', self.a)], crew_required={'ra': 'a', 'rb': 'b'})
        self.assertEqual(result['unassigned'], [])
        for route in result['routes']:
            self.assertEqual([s['request_id'] for s in route['stops']], ['r' + route['crew_id']])

    def test_missing_fixed_team_is_unassigned_instead_of_given_to_other_team(self):
        result = self.plan([self.req('missing', self.a), self.req('ra', self.a)], crew_required={'missing': 'not-selected', 'ra': 'a'})
        self.assertEqual([x['request_id'] for x in result['unassigned']], ['missing'])
        self.assertIn('담당', result['unassigned'][0]['reason'])
        self.assertEqual(sum(len(r['stops']) for r in result['routes']), 1)

    def test_explicit_end_site_still_overrides_garage_return(self):
        reqs = [self.req('ra', self.a)]
        normal = self.plan(reqs, crew_required={'ra': 'a'})
        end = self.plan(reqs, crew_required={'ra': 'a'}, end_site=self.b)
        a = next(r for r in normal['routes'] if r['crew_id'] == 'a')
        b = next(r for r in end['routes'] if r['crew_id'] == 'a')
        self.assertGreater(routing.to_min(b['end'], 0), routing.to_min(a['end'], 0))
        self.assertEqual(end['end_site'], self.b)

    def test_unloading_and_legacy_optional_arguments_remain_compatible(self):
        item = {'fee_rate_id': 'FIXTURE', 'qty': 1, 'name': '가상 대형 물건'}
        reqs = [self.req('r' + str(i), self.a, items=[item]) for i in range(3)]
        result = self.plan(reqs, crew_required={r['id']: 'a' for r in reqs}, volume_overrides={'FIXTURE': 5}, unload_site=self.a, unload_min=1, max_unloads=2)
        route = next(r for r in result['routes'] if r['crew_id'] == 'a')
        self.assertEqual(len(route['stops']), 3)
        self.assertEqual(route['unloads'], 2)
        legacy = routing.plan([self.req('old', self.a)], self.crews[:1], self.a, time_limit_s=1, travel_method='ESTIMATE')
        self.assertEqual([s['request_id'] for s in legacy['routes'][0]['stops']], ['old'])

    def test_unlimited_other_truck_does_not_merge_fixed_teams_weight_overload(self):
        self.crews[0]['capacity_kg'] = 30
        self.crews[1]['capacity_kg'] = 0
        item = {'name': '가상 미분류 품목', 'qty': 1}
        self.assertEqual(routing.weight_of(item), 20)
        reqs = [self.req('r1', self.a, items=[item]), self.req('r2', self.a, items=[item])]
        result = self.plan(reqs, crew_required={'r1': 'a', 'r2': 'a'}, unload_site=self.a, unload_min=1, max_unloads=2)
        a = next(r for r in result['routes'] if r['crew_id'] == 'a')
        self.assertEqual(len(a['stops']), 2)
        self.assertEqual(a['unloads'], 1)


if __name__ == '__main__':
    unittest.main()
