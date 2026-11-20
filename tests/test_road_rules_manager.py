"""도로 통행 정보의 미리보기·활성화·복원과 경쟁 요청을 가상 엔진으로 검증한다."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc'))
import desktop_road_rules as rules


ROAD = {'id': '101', 'name': '가상길', 'nodes': [1, 2, 3],
        'coords': [[126.9200, 35.1500], [126.9206, 35.1500], [126.9212, 35.1500]]}
SECOND = {'id': '102', 'name': '가상2길', 'nodes': [4, 5, 6],
          'coords': [[126.9300, 35.1500], [126.9306, 35.1500], [126.9312, 35.1500]]}


class SimulatedEngine:
    def __init__(self, dataset, port):
        self.dataset = dataset
        self.endpoint = f'http://127.0.0.1:{port}'
        self.status = 'READY'
        self.closed = False

    def local_endpoint(self, *, retry=False):
        return self.endpoint, self.status

    def wait_ready(self, timeout):
        return self.local_endpoint()

    def close(self):
        self.closed = True


class RoadRulesTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.pack, self.state = root / 'osrm-pack', root / 'state'
        (self.pack / 'data').mkdir(parents=True)
        (self.pack / 'data/gwangju.osrm.properties').write_bytes(b'baseline')
        (self.pack / 'data/gwangju.osrm.cells').write_bytes(b'base cells')
        (self.pack / 'data/gwangju.osrm.mldgr').write_bytes(b'base graph')
        self.kit = root / 'osrm-edit-kit'
        self.kit.mkdir()
        (self.kit / 'manifest.json').write_bytes(b'kit test manifest')
        self.engines = []
        self.catalog = {'101': ROAD, '102': SECOND}
        self.failed_previews = False
        self.fail_next_engine = False
        for name, replacement in [('load_kit', lambda *_: self.catalog), ('build_graph', self.build), ('_fetch', self.fetch)]:
            patcher = patch.object(rules, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.manager = self.open_manager()

    def open_manager(self):
        manager = rules.RoadRulesManager(self.pack, self.state, self.engine)
        self.addCleanup(manager.close)
        return manager

    def engine(self, dataset):
        engine = SimulatedEngine(dataset, 5200 + len(self.engines))
        self.engines.append(engine)
        if self.fail_next_engine:
            self.fail_next_engine = False
            engine.status, engine.endpoint = 'FAILED', ''
        return engine

    def build(self, _kit, out, records, *, expected_fingerprint=None):
        assert records and all(direction == 'forward' for direction in records.values())
        assert expected_fingerprint == rules.graph_fingerprint(self.pack)
        for suffix in ('properties', 'partition', 'cells', 'cell_metrics', 'mldgr'):
            (out / f'trial.osrm.{suffix}').write_bytes(b'graph')
        return out / 'trial.osrm'

    def fetch(self, endpoint, url, *, timeout):
        engine, = (e for e in self.engines if e.endpoint == endpoint)
        pair = url.split('?', 1)[0].split('/')[-1].split(';')
        a, b = ([float(n) for n in point.split(',')] for point in pair)
        forward = a[0] < b[0]
        detour = engine.dataset is not None and not forward and not self.failed_previews
        source_nodes = (SECOND if min(a[0], b[0]) > 126.925 else ROAD)['nodes']
        nodes = [source_nodes[-1], 100, source_nodes[0]] if detour else source_nodes if forward else source_nodes[::-1]
        coords = [a, [126.925, 35.154], b] if detour else [a, b]
        return {'code': 'Ok', 'waypoints': [{'distance': 0, 'location': p} for p in (a, b)],
                'routes': [{'distance': 483 if detour else 102, 'duration': 40 if detour else 7,
                            'geometry': {'type': 'LineString', 'coordinates': coords},
                            'legs': [{'annotation': {'nodes': nodes}}]}]}

    def preview(self, manager=None):
        manager = manager or self.manager
        return manager.preview({'road_id': '101', 'direction': 'forward', 'checked_on': date.today().isoformat(),
                                'evidence': '시험용 표지', 'base_id': manager.revision})

    def test_missing_kit_preserves_base_without_creating_state(self):
        empty = self.state.parent / 'without-kit'
        pack = empty / 'osrm-pack'
        (pack / 'data').mkdir(parents=True)
        (pack / 'data/gwangju.osrm.properties').write_bytes(b'baseline')
        state = empty / 'state'
        manager = rules.RoadRulesManager(pack, state, self.engine)
        self.addCleanup(manager.close)
        result = manager.summary()
        self.assertFalse(result['available'])
        self.assertEqual(manager.local_endpoint(), (self.engines[-1].endpoint, 'READY'))
        self.assertFalse(state.exists())

    def test_preview_apply_restart_and_rollback(self):
        self.assertEqual(self.manager.roads(35.15, 126.9205)['roads'][0]['id'], '101')
        self.assertEqual(self.manager.roads(35.20, 126.95)['roads'], [])
        preview = self.preview()
        self.assertEqual(preview['before']['reverse']['distance_m'], 102)
        self.assertEqual(preview['after']['reverse']['distance_m'], 483)
        self.assertEqual(self.manager.revision, 'base')
        self.assertTrue(self.engines[-1].closed)
        with self.assertRaises(ValueError):
            self.manager.apply({'preview_id': 'stale'})
        result = self.manager.apply({'preview_id': preview['preview_id']})
        self.assertEqual(result['previous_id'], 'base')
        self.assertEqual(result['rules'][0]['evidence'], '시험용 표지')
        self.manager.close()
        reopened = self.open_manager()
        self.assertEqual(reopened.revision, result['active_id'])
        self.assertIsNotNone(self.engines[-1].dataset)
        with self.assertRaises(ValueError):
            reopened.rollback({'active_id': 'base'})
        restored = reopened.rollback({'active_id': reopened.revision})
        self.assertEqual(restored['active_id'], 'base')
        self.assertEqual(restored['rules'], [])
        reopened.close()
        self.assertTrue(all(engine.closed for engine in self.engines))

    def test_invalid_candidate_or_denied_activation_leaves_old_engine(self):
        self.failed_previews = True
        with self.assertRaises(ValueError):
            self.preview()
        self.assertIsNone(self.manager.summary()['pending'])
        self.assertTrue(self.engines[-1].closed)
        self.failed_previews = False
        preview = self.preview()
        request = {'preview_id': preview['preview_id']}
        self.fail_next_engine = True
        with self.assertRaises(ValueError):
            self.manager.apply(request)

        @contextmanager
        def denied():
            raise ValueError('승인 권한이 바뀌었습니다.')
            yield

        with self.assertRaisesRegex(ValueError, '권한'):
            self.manager.apply(request, guard=denied)
        self.assertEqual(self.manager.revision, 'base')
        self.assertEqual(self.manager.summary()['pending']['preview_id'], preview['preview_id'])
        self.assertFalse(self.engines[0].closed)
        self.assertTrue(all(engine.closed for engine in self.engines[1:]))

    def test_preview_blocks_overlapping_updates_without_interrupting_lookup(self):
        started = threading.Event()
        release = threading.Event()
        problems = []

        def paused(*args, **kwargs):
            started.set()
            if not release.wait(4):
                raise RuntimeError('fixture timeout')
            return self.build(*args, **kwargs)

        def work():
            try:
                self.preview()
            except Exception as error:
                problems.append(error)

        with patch.object(rules, 'build_graph', paused):
            worker = threading.Thread(target=work)
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                self.assertTrue(self.manager.summary()['busy'])
                with self.assertRaises(ValueError):
                    self.preview()
                self.assertEqual(self.manager.local_endpoint(), (self.engines[0].endpoint, 'READY'))
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(problems, [])

    def test_invalid_persisted_state_does_not_open_base(self):
        self.manager.close()
        path = self.state / 'road-rules/state.json'
        path.write_text('{', encoding='utf-8')
        saved = path.parent / ('a' * 32)
        saved.mkdir()
        (saved / 'trial.osrm.properties').write_bytes(b'pre-existing graph')
        reopened = self.open_manager()
        self.assertFalse(reopened.summary()['available'])
        self.assertEqual(reopened.local_endpoint(), ('', 'INVALID_PACK'))
        reopened.close()
        self.assertTrue(saved.is_dir())

    def test_other_manager_cannot_load_or_prune_a_live_generation(self):
        sample = self.preview()
        other = self.open_manager()
        self.assertEqual(other.local_endpoint(), ('', 'INVALID_PACK'))
        other.close()
        ready = self.manager.apply({'preview_id': sample['preview_id']})
        self.assertEqual(ready['active_id'], sample['preview_id'])

    def test_application_refuses_changed_preflight_graph(self):
        sample = self.preview()
        graph = self.state / 'road-rules' / sample['preview_id'] / 'trial.osrm.cells'
        graph.write_bytes(b'after preview')
        with self.assertRaisesRegex(ValueError, '미리보기'):
            self.manager.apply({'preview_id': sample['preview_id']})
        self.assertEqual(self.manager.revision, 'base')

    def test_restart_refuses_a_changed_base_pack_with_identical_properties(self):
        sample = self.preview()
        self.manager.apply({'preview_id': sample['preview_id']})
        self.manager.close()
        (self.pack / 'data/gwangju.osrm.cells').write_bytes(b'changed base cells')
        reopened = self.open_manager()
        self.assertEqual(reopened.local_endpoint(), ('', 'INVALID_PACK'))

    def test_rollback_remains_available_when_edit_kit_is_missing(self):
        sample = self.preview()
        active = self.manager.apply({'preview_id': sample['preview_id']})['active_id']
        (self.kit / 'manifest.json').unlink()
        self.kit.rmdir()
        restored = self.manager.rollback({'active_id': active})
        self.assertFalse(restored['available'])
        self.assertEqual(restored['active_id'], 'base')

    def test_preview_refuses_an_edit_kit_that_changed_since_catalog_lookup(self):
        self.assertTrue(self.manager.summary()['available'])
        (self.kit / 'manifest.json').write_bytes(b'replaced edit kit')
        with self.assertRaisesRegex(ValueError, '편집용 자료'):
            self.preview()
        self.assertIsNone(self.manager.summary()['pending'])

    def test_candidate_that_breaks_a_retained_rule_never_replaces_active_graph(self):
        initial = self.preview()
        current = self.manager.apply({'preview_id': initial['preview_id']})['active_id']
        verified = []
        original = rules._verify_preview

        def verify(road, *args):
            verified.append(road['id'])
            if road['id'] == '101':
                raise rules.RoadRulesError('시험 후보에서 기존 허용 경로가 끊김')
            original(road, *args)

        with patch.object(rules, '_verify_preview', verify):
            with self.assertRaisesRegex(rules.RoadRulesError, '이미 적용한 도로 구간'):
                self.manager.preview({'road_id': '102', 'direction': 'forward', 'checked_on': date.today().isoformat(),
                                      'evidence': '시험용 표지', 'base_id': current})
        self.assertEqual(verified, ['102', '101'])
        self.assertEqual(self.manager.revision, current)
        self.assertIsNone(self.manager.summary()['pending'])


if __name__ == '__main__':
    unittest.main()
