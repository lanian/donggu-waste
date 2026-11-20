"""동봉 OSRM 설정·프로세스와 Table 대체를 가상 자료로 확인한다."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pc'), str(ROOT / 'tools')]
from dgpc import config, core, routing
import build_desktop


@contextmanager
def loopback(reply):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            status, headers, data = reply(self.path)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}', calls
        finally:
            server.shutdown()
            worker.join()


def fixture_pack(root):
    files = ('bin/osrm-routed.exe', 'bin/fixture.dll', 'data/gwangju.osrm.partition', 'data/gwangju.osrm.cells',
             'data/gwangju.osrm.cell_metrics', 'data/gwangju.osrm.mldgr', 'SOURCES.md', 'licenses/OSRM.txt', 'licenses/ODbL.txt')
    for name in files:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('synthetic, only for automated tests', encoding='utf-8')
    return files


class MatrixTest(unittest.TestCase):
    points = [(35.1462, 126.9233), (35.1721, 126.9461)]

    def test_table_requests_toll_exclusion(self):
        durations = [[0, 120], [180, 0]]
        with loopback(lambda _: (200, {}, {'code': 'Ok', 'durations': durations})) as (url, calls):
            matrix, source, info = routing.build_matrix(self.points, url, 'OSRM')
        self.assertEqual((matrix, source), ([[0, 2], [3, 0]], 'OSRM'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(parse_qs(urlsplit(calls[0]).query).get('exclude'), ['toll'])
        self.assertIn('유료도로 제외', routing.matrix_note(source, info))

    def test_toll_exclusion_rejection_does_not_retry_with_tolls(self):
        for code, message in (
            ('InvalidValue', 'Exclude flag combination is not supported.'),
            ('NotImplemented', 'This algorithm does not support exclude flags.'),
        ):
            with self.subTest(code=code), loopback(lambda _: (400, {}, {'code': code, 'message': message})) as (url, calls):
                matrix, source, info = routing.build_matrix(self.points, url, 'OSRM')
                route = routing.road_path(self.points, url)
            self.assertEqual(matrix, routing.estimate_matrix(self.points))
            self.assertEqual((source, info['status']), ('ESTIMATE', 'TOLL_EXCLUSION_UNAVAILABLE'))
            self.assertEqual(route, {'source': 'ESTIMATE', 'reason': 'TOLL_EXCLUSION_UNAVAILABLE'})
            self.assertIn('유료도로 제외를 지원하지 않음', routing.matrix_note(source, info))
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(parse_qs(urlsplit(path).query).get('exclude') == ['toll'] for path in calls))

    def test_null_is_distinct_from_a_malformed_cell_and_preserves_direction(self):
        points = [*self.points, (35.182, 126.947)]
        durations = [[0, None, 60], [95, 0, 'invalid'], [120, 60, 0]]
        with loopback(lambda _: (200, {}, {'code': 'Ok', 'durations': durations})) as (url, calls):
            matrix, source, info = routing.build_matrix(points, url, 'OSRM')
        self.assertEqual((matrix[0][1], matrix[1][0], matrix[1][2]), (None, 2, routing.estimate_matrix(points)[1][2]))
        self.assertEqual((source, info['road_pairs'], info['fallback_pairs'], info['unreachable_pairs']), ('OSRM', 4, 1, 1))
        self.assertIn('도로 연결 불가 1쌍', routing.matrix_note(source, info))
        self.assertIn('직선 대체 1쌍', routing.matrix_note(source, info))
        self.assertEqual(len(calls), 1)

    def test_all_road_and_all_estimated_including_wrong_matrix_size(self):
        for durations, expected in [([[0, 60], [120, 0]], ('OSRM', 2, 0)),
                                    ([[0]], ('ESTIMATE', 0, 2))]:
            with self.subTest(durations=durations), loopback(lambda _: (200, {}, {'code': 'Ok', 'durations': durations})) as (url, _):
                matrix, source, info = routing.build_matrix(self.points, url, 'OSRM')
                self.assertEqual((source, info.get('road_pairs'), info.get('fallback_pairs')), expected)
                if source == 'ESTIMATE':
                    self.assertEqual(matrix, routing.estimate_matrix(self.points))

    def test_all_null_and_no_table_are_not_service_failures(self):
        for http, body in ((200, {'code': 'Ok', 'durations': [[None, None], [None, None]]}),
                           (400, {'code': 'NoTable', 'message': 'No route found'})):
            with self.subTest(body=body), loopback(lambda _: (http, {}, body)) as (url, _):
                matrix, source, info = routing.build_matrix(self.points, url, 'OSRM')
            self.assertEqual(matrix, [[0, None], [None, 0]])
            self.assertEqual((source, info['road_pairs'], info['fallback_pairs'], info['unreachable_pairs']), ('OSRM', 0, 0, 2))
            self.assertEqual(info['status'], body['code'])
            self.assertIn('도로 연결 불가 2쌍', routing.matrix_note(source, info))

    def test_service_failure_still_uses_straight_estimates(self):
        with loopback(lambda _: (503, {}, {})) as (url, _):
            matrix, source, info = routing.build_matrix(self.points, url, 'OSRM')
        self.assertEqual(matrix, routing.estimate_matrix(self.points))
        self.assertEqual((source, info['fallback_pairs'], info['unreachable_pairs']), ('ESTIMATE', 2, 0))

    def test_redirect_cannot_relay_coordinates(self):
        response = {'code': 'Ok', 'durations': [[0, 60], [60, 0]]}
        with loopback(lambda _: (200, {}, response)) as (other, leaked):
            with loopback(lambda _: (302, {'Location': f'{other}/redirect'}, {})) as (configured, _):
                _, source, _ = routing.build_matrix(self.points, configured, 'OSRM')
        self.assertEqual(source, 'ESTIMATE')
        self.assertEqual(leaked, [])

    def test_managed_table_handles_above_default_limit_and_reports_oversize(self):
        points = [(35.14 + i / 10000, 126.92) for i in range(201)]
        durations = [[0 if i == j else 60 for j in range(101)] for i in range(101)]
        with loopback(lambda _: (200, {}, {'code': 'Ok', 'durations': durations})) as (url, calls):
            _, source, info = routing.build_matrix(points[:101], url, 'OSRM', osrm_status='READY')
            self.assertEqual((source, info['road_pairs'], info['fallback_pairs']), ('OSRM', 101 * 100, 0))
            _, source, info = routing.build_matrix(points, url, 'OSRM', osrm_status='READY')
            self.assertEqual((source, info['fallback_pairs']), ('ESTIMATE', 201 * 200))
            self.assertIn('200개 좌표', routing.matrix_note(source, info))
            self.assertEqual(len(calls), 1)
        with loopback(lambda _: (400, {}, {'code': 'TooBig'})) as (url, calls):
            _, source, info = routing.build_matrix(points[:101], url, 'OSRM', osrm_status='CONFIGURED')
            self.assertIn('지점 수 초과', routing.matrix_note(source, info))
            self.assertEqual((source, info['fallback_pairs'], len(calls)), ('ESTIMATE', 101 * 100, 1))


class DirectedRoutingTest(unittest.TestCase):
    depot = {'lat': 35.140, 'lon': 126.920}
    a = {'id': 'A', 'lat': 35.141, 'lon': 126.920, 'items': [], 'must_today': True}
    b = {'id': 'B', 'lat': 35.142, 'lon': 126.920, 'items': [], 'must_today': True}
    team = {'id': 'crew', 'name': '가상 팀', 'capacity_m3': 8}
    one_way = [[0, 60, 720], [720, 0, None], [60, 720, 0]]

    def plan(self, durations, *, requests=None, crews=None, sites=(), depots=None, **options):
        requests = requests if requests is not None else [self.a, self.b]
        crews = crews if crews is not None else [self.team]
        points = list(dict.fromkeys((p['lat'], p['lon']) for p in [self.depot, *requests, *sites, *(depots or {}).values()]))
        expected_path = '/table/v1/driving/' + routing.osrm.coordinate_path(points)
        self.assertEqual(len(durations), len(points))
        with loopback(lambda _: (200, {}, {'code': 'Ok', 'durations': durations})) as (url, calls):
            result = routing.plan(requests, crews, self.depot, osrm_url=url, service_base=0, service_per_item=0,
                                  time_limit_s=1, crew_depots=depots,
                                  crew_required={'A': 'crew', 'B': 'crew'}, **options)
        self.assertEqual([call.split('?')[0] for call in calls], [expected_path])
        return result

    def test_auto_uses_only_the_legal_direction(self):
        result = self.plan(self.one_way)
        route, = result['routes']
        self.assertEqual(([s['request_id'] for s in route['stops']], route['drive_min']), (['B', 'A'], 36))
        self.assertEqual(result['unassigned'], [])
        self.assertEqual(result['matrix_info']['unreachable_pairs'], 1)

    def test_auto_can_link_optional_stops_that_cannot_be_visited_alone(self):
        requests = [{**r, 'must_today': False} for r in (self.a, self.b)]
        sparse = [[0, None, 720], [720, 0, None], [None, 720, 0]]
        result = self.plan(sparse, requests=requests)
        self.assertEqual([s['request_id'] for s in result['routes'][0]['stops']], ['B', 'A'])
        self.assertEqual(result['unassigned'], [])

    def test_fixed_order_rejects_the_illegal_arc_and_the_reverse_works(self):
        bad = self.plan(self.one_way, fixed_plan={'crew': ['A', 'B']})
        self.assertTrue(bad.get('infeasible'), bad)
        self.assertTrue(any(u['request_id'] == 'B' and '도로 연결 불가' in u['reason'] for u in bad['unassigned']), bad)
        ok = self.plan(self.one_way, fixed_plan={'crew': ['B', 'A']})
        self.assertEqual(ok['routes'][0]['drive_min'], 36)

    def test_all_null_leaves_optional_self_loops_and_flags_unassigned(self):
        result = self.plan([[None] * 3 for _ in range(3)], requests=[self.a, {**self.b, 'must_today': False}])
        self.assertEqual(result['matrix_source'], 'OSRM')
        self.assertEqual(result['routes'][0]['stops'], [])
        self.assertCountEqual([u['request_id'] for u in result['unassigned']], ['A', 'B'])
        self.assertTrue(all('도로 연결 불가' in u['reason'] for u in result['unassigned']))

    def test_distinct_vehicle_end_and_unused_start_end_arc(self):
        end = {'lat': 35.147, 'lon': 126.920}
        idle = {'lat': 35.149, 'lon': 126.920}
        crews = [self.team, {**self.team, 'id': 'idle'}]
        durations = [[0, 60, 60, None], [60, 0, 60, None], [60, 60, 0, None], [None, None, None, 0]]
        for fixed in (None, {'crew': ['A'], 'idle': []}):
            with self.subTest(fixed=fixed):
                result = self.plan(durations, requests=[self.a], crews=crews, sites=[end], depots={'idle': idle}, end_site=end, fixed_plan=fixed)
                used, unused = result['routes']
                self.assertEqual(([s['request_id'] for s in used['stops']], used['drive_min']), (['A'], 2))
                self.assertEqual((unused['stops'], unused['drive_min'], unused['distance_km']), ([], 0, 0))
        durations[1][2] = None
        bad = self.plan(durations, requests=[self.a], crews=crews, sites=[end], depots={'idle': idle}, end_site=end, fixed_plan={'crew': ['A']})
        self.assertTrue(bad.get('infeasible'), bad)
        self.assertTrue(any('종료지' in u['reason'] and '도로 연결 불가' in u['reason'] for u in bad['unassigned']), bad)

    def test_nearby_group_cannot_hide_a_blocked_internal_or_exit_arc(self):
        near = {**self.b, 'lat': 35.1411}
        for durations in (self.one_way, [[0, 60, 60], [60, 0, 60], [None, 60, 0]]):
            with self.subTest(durations=durations):
                result = self.plan(durations, requests=[self.a, near])
                self.assertEqual([(s['request_id'], s['same_place']) for s in result['routes'][0]['stops']], [('B', 1), ('A', 1)])

    def test_required_unload_checks_both_directions(self):
        site = {'lat': 35.144, 'lon': 126.920}
        items = [{'name': '가상 품목', 'fee_rate_id': 'sample', 'qty': 1}]
        requests = [{**r, 'items': items} for r in (self.a, self.b)]
        for frm, to in ((1, 3), (3, 2)):
            with self.subTest(frm=frm, to=to):
                durations = [[0 if i == j else 60 for j in range(4)] for i in range(4)]
                durations[frm][to] = None
                result = self.plan(durations, requests=requests, crews=[{**self.team, 'capacity_m3': 1}],
                                   sites=[site], unload_site=site, max_unloads=1, volume_overrides={'sample': .7},
                                   fixed_plan={'crew': ['A', 'B']})
                self.assertTrue(result.get('infeasible'), result)
                self.assertTrue(any('반입지' in u['reason'] and '도로 연결 불가' in u['reason'] for u in result['unassigned']), result)

        durations = [[0 if i == j else 60 for j in range(4)] for i in range(4)]
        durations[1][2] = None
        legal = self.plan(durations, requests=requests, crews=[{**self.team, 'capacity_m3': 1, 'capacity_kg': 20}],
                          sites=[site], unload_site=site, max_unloads=1, volume_overrides={'sample': .7},
                          fixed_plan={'crew': ['A', 'B']})
        route, = legal['routes']
        self.assertEqual((route['drive_min'], route['unloads']), (4, 1))
        self.assertEqual(route['stops'][0]['unload_after']['volume_m3'], .7)
        self.assertEqual(route['stops'][1]['load_m3'], .7)

    def test_unload_cannot_hide_a_blocked_departure_with_an_empty_vehicle(self):
        site = {'lat': 35.144, 'lon': 126.920}
        durations = [[0, None, 60], [60, 0, 60], [60, 60, 0]]
        result = self.plan(durations, requests=[self.a], sites=[site], unload_site=site, unload_min=0,
                           fixed_plan={'crew': ['A']})
        self.assertTrue(result.get('infeasible'), result)
        self.assertTrue(any('출발지' in u['reason'] and '도로 연결 불가' in u['reason'] for u in result['unassigned']), result)


class PackTest(unittest.TestCase):
    def test_build_copies_selected_files_and_notices(self):
        copy = getattr(build_desktop, 'copy_osrm_pack', None)
        self.assertTrue(callable(copy), '빌더의 선택 동봉 경로가 필요합니다.')
        with tempfile.TemporaryDirectory(prefix='dg osrm build ') as directory:
            root = Path(directory)
            pack, app = root / 'input', root / 'Donggu-PC'
            files = fixture_pack(pack)
            app.mkdir()
            copy(pack, app)
            self.assertEqual({f.relative_to(app / 'osrm-pack').as_posix() for f in build_desktop.assert_public_files(app)}, set(files))
            (pack / 'data/pc.db').write_bytes(b'synthetic')
            with self.assertRaises(ValueError):
                copy(pack, root / 'other')
            self.assertFalse((root / 'other/osrm-pack').exists())

    def test_missing_and_incomplete_pack_explain_straight_fallback(self):
        import desktop_osrm
        with tempfile.TemporaryDirectory(prefix='dg osrm missing ') as directory, sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            root = Path(directory) / 'pack'
            with desktop_osrm.bundled_osrm(root):
                self.assertEqual(config.travel_method(con), 'ESTIMATE')
                core.set_setting(con, 'travel_method', 'OSRM')
                self.assertEqual(config.local_osrm(con), ('', 'MISSING'))
            fixture_pack(root)
            (root / 'data/gwangju.osrm.cells').unlink()
            with desktop_osrm.bundled_osrm(root) as bundle:
                url, state = config.local_osrm(con, retry=True)
                self.assertEqual((url, state), ('', 'INVALID_PACK'))
                self.assertIsNone(bundle.thread)
                _, source, info = routing.build_matrix(MatrixTest.points, url, 'OSRM', osrm_status=state)
                self.assertIn('실행 파일 확인', routing.matrix_note(source, info))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows OSRM 실행 실패 상태')
    def test_spawn_error_stays_degraded_until_explicit_retry(self):
        import desktop_osrm
        with tempfile.TemporaryDirectory(prefix='dg osrm launch failure ') as directory:
            root = Path(directory)
            fixture_pack(root)
            with patch.object(desktop_osrm, '_spawn', side_effect=OSError('가상 실행 파일 실패')) as spawn:
                with desktop_osrm.bundled_osrm(root) as bundle:
                    bundle.local_endpoint()
                    self.assertEqual(bundle.wait_ready(5), ('', 'FAILED'))
                    self.assertEqual(bundle.local_endpoint(), ('', 'FAILED'))
                    self.assertEqual(spawn.call_count, 1)
                    bundle.local_endpoint(retry=True)
                    self.assertEqual(bundle.wait_ready(5), ('', 'FAILED'))
                    self.assertEqual(spawn.call_count, 2)

    def test_explicit_choices_and_package_runtime_are_in_memory(self):
        from dgpc import osrm
        import desktop_osrm
        with tempfile.TemporaryDirectory(prefix='dg osrm package ') as directory, sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            path = Path(directory) / 'pack'
            fixture_pack(path)
            with patch.object(desktop_osrm, '_spawn', side_effect=AssertionError('명시 선택 중 기동하면 안 됩니다.')):
                with desktop_osrm.bundled_osrm(path) as bundle:
                    for method in ('ESTIMATE', 'KAKAO'):
                        core.set_setting(con, 'travel_method', method)
                        self.assertEqual(config.travel_method(con), method)
                        self.assertEqual(config.local_osrm(con), ('', 'DISABLED'))
                    core.set_setting(con, 'travel_method', 'OSRM')
                    core.set_setting(con, 'osrm_url', 'http://localhost:6215')
                    self.assertEqual(config.local_osrm(con), ('http://localhost:6215', 'CONFIGURED'))
                    self.assertEqual(bundle.endpoint, '')
                    self.assertIs(osrm.bundled, bundle)
            self.assertIsNone(osrm.bundled)

    @unittest.skipUnless(sys.platform == 'win32', '동봉 Windows 프로세스와 포트 소유자 검사')
    def test_bundle_starts_on_an_owned_port_and_stops(self):
        from dgpc import osrm
        import desktop_osrm
        with tempfile.TemporaryDirectory(prefix='dg osrm runtime ') as directory, sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            con.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)')
            core.set_setting(con, 'travel_method', 'OSRM')
            path = Path(directory) / 'pack'
            fixture_pack(path)
            # Windows 프로세스와 HTTP를 사용하고, 준비한 가상 자식 역시 실제 포트를 점유한다.
            worker = path.parent / 'fixture.py'
            worker.write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"code":"Ok","waypoints":[{}],"durations":[[0,120],[60,0]]}')
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
''', encoding='utf-8')
            launched = []

            def spawn(args):
                self.assertEqual(Path(args[0]), path / 'bin/osrm-routed.exe')
                self.assertEqual(args[args.index('--ip') + 1], '127.0.0.1')
                self.assertEqual(args[args.index('--algorithm') + 1], 'mld')
                self.assertEqual(args[args.index('--max-table-size') + 1], '200')
                self.assertEqual(Path(args[-1]), path / 'data/gwangju.osrm')
                process = subprocess.Popen([sys._base_executable, str(worker), args[args.index('--port') + 1]],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                launched.append(process)
                return process

            reserve = desktop_osrm._free_port
            with loopback(lambda _: (200, {}, {'code': 'Ok', 'waypoints': [{}]})) as (foreign, foreign_calls):
                occupied_port = int(foreign.rsplit(':', 1)[1])
                owned_port, restart_port = reserve(), reserve()
                ports = iter((occupied_port, owned_port, restart_port))
                with patch.object(desktop_osrm, '_free_port', side_effect=lambda: next(ports)), patch.object(desktop_osrm, '_spawn', spawn):
                    with desktop_osrm.bundled_osrm(path) as bundle:
                        self.assertEqual(config.travel_method(con), 'OSRM')
                        core.set_setting(con, 'travel_method', None)
                        self.assertEqual(config.travel_method(con), 'OSRM')
                        stop = time.monotonic() + 12
                        while time.monotonic() < stop:
                            url, status = config.local_osrm(con)
                            if status in ('READY', 'FAILED'):
                                break
                            time.sleep(.05)
                        self.assertEqual((url, status), (f'http://127.0.0.1:{owned_port}', 'READY'))
                        self.assertEqual(core.get_setting(con, 'osrm_url'), '')
                        self.assertEqual(routing.build_matrix(MatrixTest.points, url, 'OSRM')[:2], ([[0, 2], [1, 0]], 'OSRM'))
                        launched[-1].terminate()
                        launched[-1].wait(timeout=5)
                        stop = time.monotonic() + 5
                        while time.monotonic() < stop and config.local_osrm(con)[1] == 'READY':
                            time.sleep(.05)
                        self.assertEqual(config.local_osrm(con), ('', 'FAILED'))
                        config.local_osrm(con, retry=True)
                        url, status = bundle.wait_ready(6)
                        self.assertEqual((url, status), (f'http://127.0.0.1:{restart_port}', 'READY'))
                    self.assertIsNone(osrm.bundled)
                self.assertEqual(foreign_calls, [])
            self.assertEqual(len(launched), 3)
            self.assertTrue(all(p.poll() is not None for p in launched))
            with socket.socket() as probe:
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', int(url.rsplit(':', 1)[1]))), 0)


if __name__ == '__main__':
    unittest.main()
