"""단지 공동 배출장소와 이사 물량의 현장시간을 가상 접수로 확인한다."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'pc'))
from dgpc import routing, service, store


class PickupRoutingTests(unittest.TestCase):
    def setUp(self):
        self.point = {'lat': 35.15, 'lon': 126.93}
        self.crew = {'id': 'A', 'name': '가상팀', 'capacity_m3': 8, 'shift_start': '09:00', 'shift_end': '12:00'}

    def visit(self, rid, spot, **kwargs):
        return {'id': rid, 'address': '가상길 12 가상아파트', 'spot_note': spot,
                'items': [{'name': '의자', 'qty': 2}], **self.point, **kwargs}

    def calculate(self, visits, **kwargs):
        return routing.plan(visits, [self.crew], self.point, time_limit_s=1, **kwargs)

    def test_same_gate_different_collection_points_stay_separate(self):
        result = self.calculate([self.visit('a', '101동 수거장'), self.visit('b', '102동 수거장'),
                                 self.visit('c', '101동  수거장'), self.visit('d', '')])
        stops = result['routes'][0]['stops']
        by_id = {s['request_id']: s for s in stops}
        self.assertEqual(set(by_id), set('abcd'))
        self.assertEqual([by_id[k]['same_place'] for k in 'abcd'], [2, 1, 2, 1])
        self.assertEqual(by_id['a']['same_pickup'], 2)
        self.assertEqual(abs(stops.index(by_id['a']) - stops.index(by_id['c'])), 1)

    def test_moving_time_is_included_in_auto_and_fixed_order(self):
        r = self.visit('a', '이사 폐기물', extra_work_min=37)
        mins = routing.work_min(r['items']) + 37
        for params in ({}, {'fixed_plan': {'A': ['a']}}):
            result = self.calculate([r], **params)
            route = result['routes'][0]
            self.assertEqual(route['stops'][0]['service_min'], mins)
            self.assertEqual(route['work_min'], mins)
            self.assertEqual(route['stops'][0]['extra_work_min'], 37)
            self.assertEqual(result['summary']['work_min'], mins)
        result = self.calculate([{**r, 'appt_start': '09:00', 'appt_end': '09:30'}])
        self.assertEqual(result['unassigned'][0]['request_id'], 'a')
        self.assertIn('작업 시간', result['unassigned'][0]['reason'])

    def test_time_between_nearby_pins_and_exit_from_last_pickup(self):
        first = self.visit('a', '정문 수거장', lat=35.151)
        second = self.visit('b', '정문 수거장', lat=35.1511)
        # 단지 다음 교차로의 회전 제한으로 2번 위치에서 차고지 복귀가 더 오래 걸린다.
        times = [[0, 3, 3], [2, 0, 1], [7, 1, 0]]
        with patch.object(routing, 'build_matrix', return_value=(times, 'OSRM', {})):
            result = self.calculate([first, second])
        route, = result['routes']
        a, b = route['stops']
        self.assertEqual((a['request_id'], b['request_id'], b['same_pickup']), ('a', 'b', 2))
        self.assertEqual(routing.to_min(b['eta'], 0) - routing.to_min(a['eta'], 0), a['service_min'] + 1)
        self.assertEqual(route['drive_min'], 3 + 1 + 7)
        self.assertEqual(route['duration_min'], route['drive_min'] + route['work_min'])

    def test_apartments_with_shared_or_distinct_doorways(self):
        apartment = '가상길 12 가상아파트'
        def grouped(spot, unit_a, unit_b):
            visits = [self.visit(rid, spot, address=f'{apartment} {unit}') for rid, unit in [('a', unit_a), ('b', unit_b)]]
            return [s.get('same_pickup', 1) for s in self.calculate(visits)['routes'][0]['stops']]
        self.assertEqual(grouped('단지 공동수거장', '101동 201호', '102동 320호'), [2, 2])
        self.assertEqual(grouped('현관 앞', '101동 201호', '101동 202호'), [1, 1])
        self.assertEqual(grouped('분리수거장', '101동 201호', '102동 320호'), [1, 1])
        self.assertEqual(grouped('정문 수거장', '101동 201호', '102동 320호'), [1, 1])
        for note in ('문 앞', '연락 후 수거'):
            stops = self.calculate([self.visit('a', note), self.visit('b', note)])['routes'][0]['stops']
            self.assertEqual([s['same_place'] for s in stops], [1, 1])


class PickupIntakeTests(unittest.TestCase):
    def test_migration_legacy_client_update_and_explicit_clear(self):
        with tempfile.TemporaryDirectory(prefix='dg-pickup-v14-') as state, patch.dict('os.environ', {
                'DG_STATE': state, 'DG_DB_KEY': 'only-for-pickup-tests', 'PYTHON_KEYRING_BACKEND': 'keyring.backends.null.Keyring'}):
            with patch.object(store, 'LATEST', 14), patch.object(store, 'MIGRATIONS', store.MIGRATIONS[:14]):
                old = store.connect(create=True)
            body = dict(name='가상 담당', address='가상로 3', phone='01000000000',
                        items=[{'name': '가상의자', 'qty': 1}])
            try:
                rid = service.create_request(old, body)['id']
            finally:
                old.close()
            con = store.connect()
            try:
                def stored():
                    return con.execute('SELECT req_ver, extra_work_min FROM request WHERE id=?', (rid,)).fetchone()
                self.assertEqual(stored(), (1, 0))
                for field, expected in [({'extra_work_min': '37'}, 37), ({}, 37), ({'extra_work_min': ''}, 0)]:
                    ver = stored()[0]
                    service.update_request(con, rid, {**body, 'req_ver': ver, **field}, '가상 현장 확인')
                    self.assertEqual(stored(), (ver + 1, expected))
                for invalid in (-1, 1441, 2.3, True, '30분'):
                    with self.assertRaises(service.UserError):
                        service.create_request(con, {**body, 'extra_work_min': invalid})
            finally:
                con.close()


if __name__ == '__main__':
    unittest.main()
