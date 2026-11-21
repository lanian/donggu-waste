"""가상 접수·SQLCipher·실제 PC HTTP에서 운행 중 노선 계산을 검증한다."""
import unittest
from unittest.mock import patch

import test_assignment_change_review as fixture
import dgproto as P
from dgpc import assign as AS, core as C, server as SV, service as S


class LiveReplanningTest(fixture.AssignmentChangeReviewCase):
    def live(self, crew, order, **position):
        return self.change('reorder', [crew], crew_id=crew, order=order, recalculate=True, start_after='10:00',
                           live_state={crew: {'lat': 35.149, 'lon': 126.91, **position}})

    def test_current_position_starts_remaining_route_and_garage_is_retained_after_save(self):
        crew = self.crews[0]
        first, second = self.receipt('가상 현 위치 1'), self.receipt('가상 현 위치 2')
        self.plan(c0=[first, second])
        C.set_setting(self.con, 'volume_overrides', {self.fee: .2})
        C.set_setting(self.con, 'unload_site', {'lat': 35.144, 'lon': 126.905, 'label': '가상 반입지'})
        C.set_setting(self.con, 'unload_min', 5)
        C.set_setting(self.con, 'travel_method', 'OSRM')
        before = self.current()
        garage, live = (35.14, 126.90), (35.149, 126.91)

        def matrix(points, *args):
            self.assertIn(live, points)
            self.assertIn(garage, points)
            return [[0 if a == b else 2 for b in points] for a in points], 'OSRM', {}

        change = self.live(crew, [first, second], volume_m3=.5)
        with patch.object(SV.routing, 'build_matrix', side_effect=matrix):
            reviewed = self.review(change)
        self.assertEqual(self.current(), before)
        route, = reviewed['routes']
        self.assertEqual((route['depot']['lat'], route['depot']['lon']), live)
        self.assertEqual((route['garage']['lat'], route['garage']['lon']), garage)
        self.assertEqual(route['start'], '10:00')
        self.assertEqual(route['initial_load'], {'volume_m3': .5, 'weight_kg': 0})
        self.assertEqual([s['load_m3'] for s in route['stops']], [.7, .9])
        self.assertEqual(route['stops'][-1]['unload_after']['volume_m3'], .9)
        changed, = self.commit(reviewed)['batches']
        saved = self.current()[crew]['stops']
        self.assertEqual(saved[-1]['unload_after']['depot'], route['garage'])
        payload, _ = AS.build_daylist(self.con, changed['batch_id'])
        compact = P.compact_daylist(payload, S.FEE_ROWS)
        self.assertEqual(compact['schema'], 'dg.daylist/3')
        self.assertEqual(P.expand_daylist(compact, S.FEE_ROWS)['carry_load'], route['initial_load'])

    def test_move_last_visit_keeps_a_signed_unload_and_return_for_loaded_crew(self):
        a, b, _ = self.crews
        left = self.receipt('가상 마지막 수거지')
        self.plan(c0=[left])
        unload = {'lat': 35.144, 'lon': 126.905, 'label': '가상 반입지'}
        C.set_setting(self.con, 'unload_site', unload)
        C.set_setting(self.con, 'unload_min', 5)
        change = self.change('move', [a, b], request_id=left, to_crew_id=b, start_after='10:00',
                             live_state={a: {'lat': 35.149, 'lon': 126.91, 'volume_m3': .5}})
        review = self.review(change)
        loaded = next(r for r in review['routes'] if r['crew_id'] == a)
        self.assertEqual(loaded['stops'], [])
        self.assertEqual(loaded['unloads'], 1)
        self.assertEqual(loaded['final_unload']['volume_m3'], .5)
        saved = self.commit(review)
        batch = next(x for x in saved['batches'] if x['crew_id'] == a)
        self.assertEqual(batch['count'], 0)
        self.assertEqual([s['request_id'] for s in self.current()[b]['stops']], [left])
        payload, _ = AS.build_daylist(self.con, batch['batch_id'])
        compact = P.compact_daylist(payload, S.FEE_ROWS)
        self.assertEqual(compact['schema'], 'dg.daylist/3')
        expanded = P.expand_daylist(compact, S.FEE_ROWS)
        self.assertEqual(expanded['stops'], [])
        self.assertEqual(expanded['final_unload']['stop_count'], 0)
        self.assertEqual(expanded['final_unload']['lat'], unload['lat'])
        self.assertEqual(expanded['final_unload']['depot']['lat'], 35.14)
        self.assertEqual(expanded['carry_load'], {'volume_m3': .5, 'weight_kg': 0})
        mapped = next(row for row in AS.list_batches(self.con, fixture.DAY, route_map=True) if row['id'] == batch['batch_id'])
        self.assertTrue(mapped['map_route']['complete'])
        self.assertEqual(mapped['map_route']['final_unload']['lat'], unload['lat'])

    def test_remove_only_visit_requires_a_feasible_final_unload(self):
        crew = self.crews[0]
        left = self.receipt('가상 남은 수거지')
        self.plan(c0=[left])
        change = self.change('remove', [crew], request_id=left, start_after='10:00',
                             live_state={crew: {'lat': 35.149, 'lon': 126.91, 'volume_m3': .5}})
        unload = {'lat': 35.144, 'lon': 126.905, 'label': '가상 반입지'}
        C.set_setting(self.con, 'unload_site', unload)
        C.set_setting(self.con, 'unload_min', 5)
        C.set_setting(self.con, 'unload_hours', {'start': '13:00', 'end': '13:03'})
        too_short = self.review(change)
        self.assertNotIn('review_basis', too_short)
        self.assertIn('반입지 운영시간', too_short['route_issues'][0]['message'])
        C.set_setting(self.con, 'unload_hours', {'start': '09:00', 'end': '18:00'})
        C.set_setting(self.con, 'end_site', {'lat': 35.145, 'lon': 126.912, 'label': '가상 별도 종료지'})
        explicit_end = self.review(change)
        self.assertNotIn('review_basis', explicit_end)
        self.assertIn('별도 종료지', explicit_end['route_issues'][0]['message'])
        C.set_setting(self.con, 'end_site', None)
        plan = self.review(change)
        self.assertIn('review_basis', plan)
        route, = plan['routes']
        self.assertEqual(route['stops'], [])
        self.assertGreater(route['end'], route['final_unload']['eta'])
        self.assertEqual(self.commit(plan)['batches'][0]['count'], 0)

    def test_existing_intermediate_unload_resets_actual_load_and_weight(self):
        crew = self.crews[0]
        first, second = self.receipt('가상 중간 하차 1'), self.receipt('가상 중간 하차 2')
        self.plan(c0=[first, second])
        S.save_crew(self.con, {'id': crew, 'name': '가상 재계산 1팀', 'capacity_m3': 1, 'capacity_kg': 200, 'shift_end': '18:00'})
        C.set_setting(self.con, 'volume_overrides', {self.fee: .2})
        C.set_setting(self.con, 'unload_site', {'lat': 35.144, 'lon': 126.905, 'label': '가상 반입지'})
        C.set_setting(self.con, 'unload_min', 5)
        C.set_setting(self.con, 'max_unloads', 1)
        result = self.review(self.live(crew, [first, second], volume_m3=.5, weight_kg=60))
        self.assertIn('review_basis', result, result.get('unassigned'))
        route, = result['routes']
        self.assertEqual([s['load_m3'] for s in route['stops']], [.7, .2])
        self.assertEqual([s['load_kg'] - s['kg'] for s in route['stops']], [60, 0])
        self.assertEqual(route['stops'][0]['unload_after']['volume_m3'], .7)
        self.assertEqual(route['unloads'], 2)

    def test_first_leg_required_unload_blocks_commit_with_instruction(self):
        crew = self.crews[0]
        first = self.receipt('가상 선하차')
        self.plan(c0=[first])
        S.save_crew(self.con, {'id': crew, 'name': '가상 재계산 1팀', 'capacity_m3': 1, 'capacity_kg': 200, 'shift_end': '18:00'})
        C.set_setting(self.con, 'volume_overrides', {self.fee: .4})
        C.set_setting(self.con, 'unload_site', {'lat': 35.144, 'lon': 126.905, 'label': '가상 반입지'})
        C.set_setting(self.con, 'max_unloads', 2)
        before = self.current()
        result = self.review(self.live(crew, [first], volume_m3=.8))
        self.assertTrue(result['infeasible'])
        self.assertNotIn('review_basis', result)
        self.assertIn('먼저 반입지에서 하차', result['unassigned'][0]['reason'])
        self.assertEqual(self.current(), before)

    def test_actual_vehicle_fields_are_validated_before_calculation(self):
        a, b, _ = self.crews
        rid = self.receipt('가상 차량 입력')
        self.plan(c0=[rid])
        valid = self.live(a, [rid])
        invalid = [
            {**valid, 'live_state': {b: valid['live_state'][a]}},
            {**valid, 'live_state': {a: {'lat': 35.149}}},
            {**valid, 'live_state': {a: {'lat': 35.149, 'lon': True}}},
            {**valid, 'live_state': {a: {'lat': 35.149, 'lon': 110}}},
            self.live(a, [rid], volume_m3=9),
            self.live(a, [rid], volume_m3=-1),
            self.live(a, [rid], volume_m3=.005),
            self.live(a, [rid], weight_kg=10),
            {**valid, 'start_after': ''},
            {**valid, 'recalculate': 'true'},
            {**valid, 'live_state': [], 'start_after': '10:00'},
        ]
        with patch.object(SV.routing, 'plan', side_effect=AssertionError('잘못된 실제 차량 자료로 계산할 수 없음')):
            for i, change in enumerate(invalid):
                with self.subTest(case=i):
                    self.review(change, expected=400)


if __name__ == '__main__':
    unittest.main()
