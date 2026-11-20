"""실제 OR-Tools에서 팀별 차고지·고정 담당·하차·복귀를 검증한다."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'pc'))
from dgpc import route_depots, routing


class RoutingDepotTests(unittest.TestCase):
    def setUp(self):
        self.a = {'lat': 35.14, 'lon': 126.90, 'label': '가상 A 차고지'}
        self.b = {'lat': 35.20, 'lon': 127.00, 'label': '가상 B 차고지'}
        self.common = {'lat': 35.5, 'lon': 127.5, 'label': '가상 공통'}
        self.crews = [{'id': 'a', 'name': 'A팀', 'capacity_m3': 8, 'shift_start': '09:00', 'shift_end': '17:00'},
                      {'id': 'b', 'name': 'B팀', 'capacity_m3': 8, 'shift_start': '09:00', 'shift_end': '17:00'}]

    def req(self, rid, point, **extra):
        return {'id': rid, 'lat': point['lat'], 'lon': point['lon'], 'items': [], 'must_today': True, **extra}

    def plan(self, reqs, *, service_base=0, **extra):
        return routing.plan(reqs, self.crews, self.common, crew_depots={'a': self.a, 'b': self.b},
                            service_base=service_base, service_per_item=0, time_limit_s=1, travel_method='ESTIMATE', **extra)

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

    def test_same_place_split_between_teams_when_shared_appointment_cannot_fit_one_crew(self):
        self.b = self.a
        destination = {**self.a, 'lat': self.a['lat'] + .004}
        reqs = [self.req(rid, destination, appt_start='09:00', appt_end='09:10') for rid in ('r1', 'r2')]
        result = self.plan(reqs, service_base=5)
        self.assertEqual(result['unassigned'], [])
        self.assertCountEqual([r['stops'][0]['request_id'] for r in result['routes'] if len(r['stops']) == 1], ['r1', 'r2'])
        self.assertTrue(all(s['eta'] <= '09:05' for r in result['routes'] for s in r['stops']))

    def test_optional_same_place_visit_does_not_block_a_required_visit(self):
        self.crews = self.crews[:1]
        destination = {**self.a, 'lat': self.a['lat'] + .004}
        must = self.req('must', destination, appt_start='09:00', appt_end='09:10')
        optional = {**must, 'id': 'optional', 'must_today': False}
        result = self.plan([optional, must], service_base=5)
        self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], ['must'])
        self.assertEqual([u['request_id'] for u in result['unassigned']], ['optional'])

    def test_same_place_visits_still_group_when_one_crew_can_finish_together(self):
        reqs = [self.req(rid, self.a, appt_start='09:00', appt_end='09:10') for rid in ('r1', 'r2')]
        result = self.plan(reqs, service_base=5)
        self.assertEqual(result['unassigned'], [])
        self.assertTrue(any([s['same_place'] for s in r['stops']] == [2, 2] for r in result['routes']))

    def test_same_place_grouping_allows_time_to_reach_the_configured_end_site(self):
        self.b = self.a
        for crew in self.crews:
            crew['shift_end'] = '09:12'
        destination = {**self.a, 'lat': self.a['lat'] + .004}
        end_site = {**self.a, 'lat': self.a['lat'] + .0135}
        reqs = [self.req(rid, destination) for rid in ('r1', 'r2')]
        result = self.plan(reqs, service_base=5, end_site=end_site)
        self.assertEqual(result['unassigned'], [])
        self.assertTrue(all(len(r['stops']) == 1 and r['end'] <= '09:12' for r in result['routes']))

    def test_equal_priority_recommendation_prefers_shorter_drive_when_only_one_visit_fits(self):
        self.crews = self.crews[:1]
        self.crews[0]['capacity_m3'] = 6
        near = self.req('near', self.a, items=[{'name': '가상 피아노', 'fee_rate_id': 'SLOW', 'qty': 1}])
        far = self.req('far', {**self.a, 'lat': self.a['lat'] + .003}, items=[{'name': '의자', 'fee_rate_id': 'FAST', 'qty': 1}])
        for request in (near, far):
            request['must_today'] = False
        result = routing.plan([far, near], self.crews, self.a, time_limit_s=1, volume_overrides={'SLOW': 5, 'FAST': 5})
        self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], ['near'])
        self.assertEqual(result['routes'][0]['drive_min'], 0)

    def test_road_travel_time_can_prefer_a_farther_stop_with_a_faster_road(self):
        self.crews = self.crews[:1]
        self.crews[0]['capacity_m3'] = 6
        item = {'name': '가상 의자', 'fee_rate_id': 'FILL', 'qty': 1}
        near = self.req('near', {**self.a, 'lat': self.a['lat'] + .002}, items=[item])
        far = self.req('far', {**self.a, 'lat': self.a['lat'] + .008}, items=[item])
        for request in (near, far):
            request['must_today'] = False
        # 같은 두 지점의 길찾기 시간. 가까운 곳이 우회 때문에 더 오래 걸린다.
        with patch.object(routing, 'build_matrix', return_value=([[0, 9, 2], [8, 0, 7], [3, 7, 0]], 'OSRM', {})):
            result = self.plan([near, far], volume_overrides={'FILL': 5})
        self.assertEqual(result['matrix_source'], 'OSRM')
        self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], ['far'])
        self.assertEqual(result['routes'][0]['drive_min'], 5)

    def test_nearby_visits_with_a_road_detour_are_planned_as_separate_stops(self):
        self.crews = self.crews[:1]
        first = self.req('first', {**self.a, 'lat': self.a['lat'] + .002})
        second = self.req('second', {**self.a, 'lat': self.a['lat'] + .0021})
        with patch.object(routing, 'build_matrix', return_value=([[0, 2, 4], [2, 0, 8], [4, 7, 0]], 'OSRM', {})):
            result = self.plan([first, second], service_base=5)
        route = result['routes'][0]
        self.assertEqual(result['unassigned'], [])
        self.assertEqual([s['same_place'] for s in route['stops']], [1, 1])
        self.assertEqual(route['drive_min'], 13)

    def test_missing_fixed_team_is_unassigned_instead_of_given_to_other_team(self):
        result = self.plan([self.req('missing', self.a), self.req('ra', self.a)], crew_required={'missing': 'not-selected', 'ra': 'a'})
        self.assertEqual([x['request_id'] for x in result['unassigned']], ['missing'])
        self.assertIn('담당', result['unassigned'][0]['reason'])
        self.assertEqual(sum(len(r['stops']) for r in result['routes']), 1)

    def test_unlinked_dong_is_unassigned_only_in_default_auto_mode(self):
        self.crews = self.crews[:1]
        reqs = [self.req('unlinked', self.a, dong='가상 B동'), self.req('unknown', self.a, dong='')]
        dongs = [{'name': '가상 A동', 'crew_id': 'a'}, {'name': '가상 B동', 'crew_id': None}]
        context = route_depots.resolve(reqs, self.crews, dongs, self.a)
        auto = self.plan(reqs, crew_required=context['crew_defaults'] | context['crew_required'])
        self.assertEqual([s['request_id'] for s in auto['routes'][0]['stops']], ['unknown'])
        missing, = auto['unassigned']
        self.assertEqual((missing['request_id'], missing['must_today']), ('unlinked', True))
        self.assertIn('담당 수거팀이 연결되어 있지 않', missing['reason'])

        off = route_depots.resolve(reqs, self.crews, dongs, self.a, keep_dong_crews=False)
        unrestricted = self.plan(reqs, crew_required=off['crew_defaults'] | off['crew_required'])
        self.assertEqual(unrestricted['unassigned'], [])
        manual = self.plan(reqs, crew_required=context['crew_required'], fixed_plan={'a': ['unlinked', 'unknown']})
        self.assertEqual([s['request_id'] for s in manual['routes'][0]['stops']], ['unlinked', 'unknown'])

    def test_idle_vehicle_does_not_have_to_drive_to_the_common_end_site(self):
        self.crews[1]['shift_end'] = '09:05'
        end = {**self.a, 'lat': self.a['lat'] + .001}
        self.assertGreater(routing._estimate_min((self.b['lat'], self.b['lon']), (end['lat'], end['lon'])), 5)
        for fixed in (None, {'a': ['ra']}):
            with self.subTest(fixed=fixed):
                result = self.plan([self.req('ra', self.a)], crew_required={'ra': 'a'}, end_site=end, fixed_plan=fixed)
                self.assertFalse(result.get('infeasible'), result)
                used, idle = result['routes']
                self.assertEqual([s['request_id'] for s in used['stops']], ['ra'])
                self.assertGreater(used['drive_min'], 0)
                self.assertEqual((idle['stops'], idle['drive_min'], idle['distance_km']), ([], 0, 0))

    def test_empty_unload_cannot_bypass_a_direct_appointment_leg(self):
        self.crews = self.crews[:1]
        visit = {**self.a, 'lat': self.a['lat'] + .002}
        site = {**self.a, 'lat': self.a['lat'] + .004}
        req = self.req('ra', visit, appt_end='09:06')
        # 출발→수거 12분, 출발→반입지→수거 2분. 빈 차에 하차를 넣어 약속을 맞추면 안 된다.
        matrix = [[0, 12, 1], [1, 0, 1], [1, 1, 0]]
        with patch.object(routing, 'build_matrix', return_value=(matrix, 'OSRM', {'unreachable_pairs': 0})):
            fixed = self.plan([req], service_base=1, fixed_plan={'a': ['ra']}, unload_site=site, unload_min=1, max_unloads=1)
            self.assertTrue(fixed.get('infeasible'), fixed)
            self.assertIn('방문 약속', fixed['unassigned'][0]['reason'])
            auto = self.plan([req], service_base=1, unload_site=site, unload_min=1, max_unloads=1)
            self.assertTrue(all(not route['stops'] and route['unloads'] == 0 for route in auto['routes']))
            self.assertEqual([u['request_id'] for u in auto['unassigned']], ['ra'])

    def test_weight_only_unload_is_recorded_in_the_fixed_plan(self):
        self.crews = self.crews[:1]
        self.crews[0]['capacity_kg'] = 30
        item = {'name': '가상 물건', 'fee_rate_id': 'SMALL', 'qty': 1}
        reqs = [self.req(rid, self.a, items=[item]) for rid in ('r1', 'r2')]
        result = self.plan(reqs, fixed_plan={'a': ['r1', 'r2']}, volume_overrides={'SMALL': .004},
                           unload_site=self.a, unload_min=1, max_unloads=1)
        route, = result['routes']
        self.assertEqual([s['request_id'] for s in route['stops']], ['r1', 'r2'])
        self.assertEqual(route['unloads'], 1)
        self.assertEqual(route['stops'][0]['unload_kg'], 20)
        self.assertEqual(route['stops'][0]['unload_after']['volume_m3'], 0)

    def test_request_count_precedes_urgent_weight(self):
        self.crews = self.crews[:1]
        self.crews[0]['capacity_m3'] = 1
        reqs = [self.req(rid, {**self.a, 'lat': self.a['lat'] + i * .001}, must_today=False,
                         urgent=rid == 'urgent', items=[{'fee_rate_id': rid, 'qty': 1}])
                for i, rid in enumerate(('urgent', 'r1', 'r2'), 1)]
        result = self.plan(reqs, volume_overrides={'urgent': .9, 'r1': .5, 'r2': .5})
        self.assertCountEqual([s['request_id'] for s in result['routes'][0]['stops']], ['r1', 'r2'])

    def test_shorter_road_time_then_urgency_for_the_same_coverage(self):
        self.crews = self.crews[:1]
        self.crews[0]['capacity_m3'] = 1
        item = {'fee_rate_id': 'FILL', 'qty': 1}
        reqs = [self.req(rid, {**self.a, 'lat': self.a['lat'] + i * .001}, must_today=False,
                         urgent=rid == 'urgent', items=[item]) for i, rid in enumerate(('urgent', 'normal'), 1)]
        for first_leg, expected in ((8, 'normal'), (2, 'urgent')):
            with self.subTest(first_leg=first_leg):
                matrix = [[0, first_leg, 2], [2, 0, 1], [2, 1, 0]]
                with patch.object(routing, 'build_matrix', return_value=(matrix, 'OSRM', {})):
                    result = self.plan(reqs, volume_overrides={'FILL': .9})
                self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], [expected])

    def test_still_maximizes_required_coverage_when_some_must_be_relaxed(self):
        self.crews = self.crews[:1]
        self.crews[0]['capacity_m3'] = 1
        reqs = [self.req(rid, {**self.a, 'lat': self.a['lat'] + i * .001}, must_today=must,
                         priority=0 if must else 10**10, items=[{'fee_rate_id': rid, 'qty': 1}])
                for i, (rid, must) in enumerate((('oversize', True), ('must', True), ('r1', False), ('r2', False)), 1)]
        result = self.plan(reqs, volume_overrides={'oversize': 1.2, 'must': .9, 'r1': .5, 'r2': .5})
        self.assertTrue(result['must_relaxed'])
        self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], ['must'])
        self.assertEqual([u['request_id'] for u in result['unassigned'] if u.get('must_today')], ['oversize'])

    def test_ungrouped_alternative_recovers_four_visits_with_one_matrix(self):
        self.b = self.a
        for crew in self.crews:
            crew['capacity_m3'] = 1
        for must in (False, True):
            with self.subTest(must=must):
                reqs = [self.req(rid, {**self.a, 'lat': self.a['lat'] + offset}, must_today=must,
                                 items=[{'fee_rate_id': rid, 'qty': 1}])
                        for rid, offset in (('r1', .001), ('r2', .00104), ('r3', .002), ('r4', .003))]
                with patch.object(routing, 'build_matrix', wraps=routing.build_matrix) as matrix:
                    result = self.plan(reqs, volume_overrides={'r1': .4, 'r2': .4, 'r3': .6, 'r4': .6})
                matrix.assert_called_once()
                self.assertEqual(result['unassigned'], [])
                self.assertFalse(result['must_relaxed'])
                for route in result['routes']:
                    self.assertEqual(len(route['stops']), 2)
                    self.assertEqual(route['end_load_m3'], 1)

    def test_explicit_end_site_still_overrides_garage_return(self):
        reqs = [self.req('ra', self.a)]
        normal = self.plan(reqs, crew_required={'ra': 'a'})
        end = self.plan(reqs, crew_required={'ra': 'a'}, end_site=self.b)
        a = next(r for r in normal['routes'] if r['crew_id'] == 'a')
        b = next(r for r in end['routes'] if r['crew_id'] == 'a')
        self.assertGreater(routing.to_min(b['end'], 0), routing.to_min(a['end'], 0))
        self.assertEqual(end['end_site'], self.b)

    def test_final_unload_uses_receiving_hours_and_returns_each_team_to_its_own_garage(self):
        self.crews[0]['shift_end'] = self.crews[1]['shift_end'] = '10:30'
        site = {**self.a, 'lat': self.a['lat'] + .005, 'label': '가상 반입지'}
        item = {'fee_rate_id': 'FILL', 'qty': 1}
        reqs = [self.req('ra', self.a, items=[item]), self.req('rb', self.b, items=[item])]
        # 행렬 순서 A에서 수거/A 차고지, B에서 수거/B 차고지, 반입지.
        # 차고지로 직접 가는 왕복과 서로 다른 방향의 하차·복귀 시간도 구별한다.
        matrix = [[0, 19, 2], [17, 0, 4], [7, 12, 0]]
        for fixed in (None, {'a': ['ra'], 'b': ['rb']}):
            with self.subTest(fixed=fixed):
                with patch.object(routing, 'build_matrix', return_value=(matrix, 'OSRM', {})):
                    result = self.plan(reqs, fixed_plan=fixed, crew_required={'ra': 'a', 'rb': 'b'},
                                       service_base=1, volume_overrides={'FILL': 1}, unload_site=site,
                                       unload_min=5, max_unloads=0, unload_hours={'start': '10:00', 'end': '10:05'},
                                       final_unload_return=True)
                self.assertEqual(result['unassigned'], [])
                self.assertEqual(result['unload_site'], site)
                for route, end, drive, garage in zip(result['routes'], ('10:12', '10:17'), (9, 16), (self.a, self.b)):
                    self.assertEqual(route['depot'], garage)
                    self.assertEqual((route['end'], route['drive_min'], route['unloads']), (end, drive, 1))
                    self.assertEqual((route['end_load_m3'], route['end_load_kg']), (0, 0))
                    stop, = route['stops']
                    self.assertEqual(stop['request_id'], 'r' + route['crew_id'])
                    self.assertEqual(stop['unload_after'], {
                        'eta': '10:00', 'label': '가상 반입지', 'volume_m3': 1, 'minutes': 5, 'final': True})

    def test_final_unload_does_not_consume_intermediate_quota_or_move_idle_team(self):
        self.crews[1]['shift_end'] = '09:05'
        site = {**self.a, 'lat': self.a['lat'] + .004}
        reqs = [self.req(rid, self.a, items=[{'fee_rate_id': 'FILL', 'qty': 1}]) for rid in ('r1', 'r2')]
        for fixed in (None, {'a': ['r1', 'r2']}):
            with self.subTest(fixed=fixed):
                result = self.plan(reqs, fixed_plan=fixed, crew_required={'r1': 'a', 'r2': 'a'}, unload_site=site,
                                   unload_min=5, max_unloads=1, volume_overrides={'FILL': 5}, final_unload_return=True)
                self.assertEqual(result['unassigned'], [])
                route, idle = result['routes']
                self.assertEqual((idle['stops'], idle['unloads'], idle['drive_min']), ([], 0, 0))
                self.assertEqual(route['unloads'], 2)
                middle, final = [stop['unload_after'] for stop in route['stops']]
                self.assertFalse(middle.get('final'))
                self.assertTrue(final['final'])
                self.assertEqual(result['summary']['trips_per_crew'], 2)

    def test_final_unload_reports_unreachable_or_late_garage_return(self):
        self.crews = self.crews[:1]
        self.crews[0]['shift_end'] = '09:16'
        site = {**self.a, 'lat': self.a['lat'] + .004}
        reqs = [self.req('ra', self.a, items=[{'fee_rate_id': 'FILL', 'qty': 1}])]
        for back, reason in ((None, '반입지에서 차고지까지 도로 연결 불가'), (11, '근무 종료')):
            with self.subTest(back=back):
                info = {'unreachable_pairs': int(back is None)}
                with patch.object(routing, 'build_matrix', return_value=([[0, 2], [back, 0]], 'OSRM', info)):
                    result = self.plan(reqs, fixed_plan={'a': ['ra']}, service_base=1, unload_site=site,
                                       unload_min=5, max_unloads=0, final_unload_return=True)
                self.assertTrue(result['infeasible'])
                self.assertIn(reason, result['unassigned'][0]['reason'])

    def test_automatic_final_unload_with_lunch_on_garage_return(self):
        self.crews = self.crews[:1]
        self.crews[0]['shift_end'] = '10:30'
        site = {**self.a, 'lat': self.a['lat'] + .004}
        req = self.req('ra', self.a, must_today=False, appt_start='09:50', appt_end='09:54')
        with patch.object(routing, 'build_matrix', return_value=([[0, 6], [15, 0]], 'OSRM', {})):
            result = self.plan([req], service_base=4, unload_site=site, unload_min=5, max_unloads=0,
                               unload_hours={'start': '10:00', 'end': '10:05'},
                               lunch={'start': '10:05', 'end': '10:15', 'minutes': 10}, final_unload_return=True)
        self.assertEqual(result['unassigned'], [])
        route, = result['routes']
        self.assertEqual(route['end'], '10:30')
        self.assertEqual(route['lunch']['start'], '10:05')
        self.assertTrue(route['stops'][-1]['unload_after']['final'])

    def test_final_unload_does_not_override_explicit_end_site(self):
        self.crews = self.crews[:1]
        req = self.req('ra', self.a)
        result = self.plan([req], end_site=self.b, unload_site=self.common, max_unloads=0, final_unload_return=True)
        self.assertEqual(result['unassigned'], [])
        self.assertEqual(result['end_site'], self.b)
        self.assertNotIn('unload_after', result['routes'][0]['stops'][-1])

    def test_too_short_receiving_hours_do_not_create_idle_unload_trips(self):
        self.crews = self.crews[:1]
        req = self.req('ra', self.a, must_today=False)
        result = self.plan([req], unload_site=self.b, unload_min=10, max_unloads=2,
                           unload_hours={'start': '10:00', 'end': '10:05'}, final_unload_return=True)
        self.assertFalse(result.get('infeasible'), result)
        self.assertEqual([u['request_id'] for u in result['unassigned']], ['ra'])
        route, = result['routes']
        self.assertEqual((route['stops'], route['unloads'], route['drive_min']), ([], 0, 0))

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
