"""합성 원본과 모의 OSRM 도구에서 전체 도로 방향 보정을 검사한다."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'pc'))
sys.path.insert(0, str(ROOT / 'tools'))
import desktop_road_build as build
from prepare_osrm_edit_kit import write_catalog


SOURCE = b'''<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <node id="1" lat="35.154" lon="126.920"/>
  <node id="2" lat="35.155" lon="126.921"/>
  <node id="3" lat="35.156" lon="126.922"/>
  <way id="100" version="2">
    <nd ref="1"/>
    <nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
  <way id="200" version="1">
    <nd ref="2"/>
    <nd ref="3"/>
    <tag k="highway" v="residential"/>
    <tag k="oneway" v="no"/>
  </way>
  <way id="300">
    <nd ref="1"/>
    <nd ref="3"/>
    <tag k="highway" v="residential"/>
    <tag k="junction" v="roundabout"/>
  </way>
  <way id="400">
    <nd ref="1"/>
    <nd ref="2"/>
    <tag k="highway" v="residential"/>
    <tag k="oneway:motorcar" v="yes"/>
  </way>
  <way id="500">
    <nd ref="1"/>
    <nd ref="2"/>
    <tag k="highway" v="footway"/>
  </way>
  <relation id="900">
    <member type="way" ref="100" role="from"/>
    <member type="node" ref="2" role="via"/>
    <member type="way" ref="200" role="to"/>
    <tag k="type" v="restriction"/>
  </relation>
</osm>
'''


class RoadBuildTest(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name).resolve()
        self.kit = self.root / 'kit'
        self.kit.mkdir()
        self.base = self.kit / 'base.osm.pbf'
        self.base.write_bytes(b'unchanged source PBF fixture')
        source_xml = self.root / 'source.osm.gz'
        source_xml.write_bytes(gzip.compress(SOURCE))
        write_catalog(source_xml, self.kit / 'roads.json.gz')
        for file in ('bin/osrm-extract.exe', 'bin/osrm-partition.exe', 'bin/osrm-customize.exe',
                     'profiles/car.lua', 'licenses/OSRM.txt', 'SOURCES.md'):
            target = self.kit / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('fixture', encoding='utf-8')
        self.fingerprint = 'a' * 64
        self.manifest = {
            'schema': 1, 'base_fingerprint': self.fingerprint, 'version': '26.11.0',
            'provenance': {'data_version': '2026-11-01T21:21:36Z'},
            'files': {p.relative_to(self.kit).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in self.kit.rglob('*') if p.is_file()},
        }
        (self.kit / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_catalog_exposes_full_ordered_geometry_only_for_editable_roads(self):
        roads = build.load_kit(self.kit, self.fingerprint)
        self.assertEqual(set(roads), {'100', '200'})
        self.assertEqual(roads['100'], {'id': '100', 'name': '', 'nodes': [1, 2],
                                        'coords': [[126.92, 35.154], [126.921, 35.155]]})

    def test_verified_pack_rejects_changed_data_wrong_base_and_extra_dll(self):
        with self.assertRaises(ValueError):
            build.load_kit(self.kit, 'b' * 64)
        extra = self.kit / 'bin/extra.dll'
        extra.write_bytes(b'extra')
        with self.assertRaises(ValueError):
            build.load_kit(self.kit, self.fingerprint)
        extra.unlink()
        self.base.write_bytes(gzip.compress(SOURCE + b'\n'))
        with self.assertRaises(ValueError):
            build.load_kit(self.kit, self.fingerprint)

    def test_graph_fingerprint_detects_the_changed_graph_with_same_properties(self):
        pack = self.root / 'pack'
        data = pack / 'data'
        data.mkdir(parents=True)
        properties = data / 'gwangju.osrm.properties'
        properties.write_bytes(b'unchanged properties')
        graph = data / 'gwangju.osrm.mldgr'
        graph.write_bytes(b'before one-way correction')
        fingerprint = build.graph_fingerprint(pack)
        graph.write_bytes(b'after one-way correction')
        self.assertNotEqual(build.graph_fingerprint(pack), fingerprint)
        self.assertEqual(properties.read_bytes(), b'unchanged properties')

    def test_build_rechecks_base_fingerprint_before_native_tools(self):
        with patch.object(build.subprocess, 'run') as native:
            with self.assertRaises(ValueError):
                build.build_graph(self.kit, self.root / 'different-base', {'100': 'forward'},
                                  expected_fingerprint='c' * 64)
            native.assert_not_called()

    def test_profile_restricts_only_selected_full_ways_before_native_steps(self):
        out = self.root / 'candidate'
        commands = []

        def native(arguments, **options):
            commands.append(Path(arguments[0]).stem)
            work = options['cwd']
            self.assertEqual(work.parent, out)
            self.assertTrue((work / 'profiles/car.lua').is_file())
            self.assertFalse(any(Path(arg).is_absolute() for arg in arguments[1:]))
            if commands[-1] == 'osrm-extract':
                source_file, = [Path(arg) for arg in arguments if str(arg).endswith('.osm.pbf')]
                self.assertEqual((work / source_file).read_bytes(), self.base.read_bytes())
                profile, = [Path(arg) for arg in arguments if str(arg).endswith('.lua')]
                wrapper = (work / profile).read_text(encoding='ascii')
                self.assertIn('[100] = 1', wrapper)
                self.assertIn('[200] = -1', wrapper)
                self.assertNotIn('[300]', wrapper)
                self.assertIn('result.backward_mode = mode.inaccessible', wrapper)
                self.assertIn('result.forward_mode = mode.inaccessible', wrapper)
            if commands[-1] == 'osrm-customize':
                for suffix in ('properties', 'partition', 'cells', 'cell_metrics', 'mldgr'):
                    (work / f'trial.osrm.{suffix}').write_bytes(b'fixture')

        with patch.object(build.subprocess, 'run', side_effect=native):
            self.assertEqual(build.build_graph(self.kit, out, {'100': 'forward', '200': 'reverse'}),
                             out / 'trial.osrm')
        self.assertEqual(commands, ['osrm-extract', 'osrm-partition', 'osrm-customize'])
        self.assertEqual(self.base.read_bytes(), b'unchanged source PBF fixture')
        self.assertFalse(any(out.rglob('*.pbf')))
        self.assertFalse(any(out.rglob('*.lua')))

    def test_ineligible_or_unknown_road_never_starts_native_tools(self):
        with patch.object(build.subprocess, 'run') as native:
            for road in ('300', '400', '500', '999'):
                with self.subTest(road=road), self.assertRaises(ValueError):
                    build.build_graph(self.kit, self.root / f'blocked-{road}', {road: 'forward'})
            native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
