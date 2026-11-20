"""검증된 원본 PBF와 같은 버전의 Windows OSRM으로 선택형 보정 자료를 준비한다."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from xml.etree import ElementTree


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc'))
from desktop_road_build import CATALOG, SOURCE, TOOLS, file_digest, graph_fingerprint, kit_files, load_kit, valid_osm_id


_CAR_ROADS = {'trunk', 'trunk_link', 'primary', 'primary_link', 'secondary', 'secondary_link', 'tertiary',
              'tertiary_link', 'unclassified', 'residential', 'living_street', 'service', 'road'}
_ACCESS = {'access', 'vehicle', 'motor_vehicle', 'motorcar'}
_RESTRICTED = {'no', 'private', 'emergency', 'customers', 'destination', 'delivery', 'agricultural', 'forestry'}


def _editable(tags: dict[str, str]) -> bool:
    return (tags.get('highway') in _CAR_ROADS and tags.get('oneway') in {None, 'no', '0', 'false'}
            and not any(key.startswith('oneway:') for key in tags)
            and tags.get('junction') not in {'roundabout', 'circular'} and tags.get('area') != 'yes'
            and tags.get('service') not in {'driveway', 'parking_aisle', 'emergency_access'}
            and not any(tags.get(key) in _RESTRICTED or f'{key}:conditional' in tags for key in _ACCESS))


def write_catalog(source_xml: Path, destination: Path) -> int:
    """OSM XML의 도로 ID와 전체 좌표·노드 순서를 압축 목록에 쓴다."""
    node_coords = {}
    count = 0
    opener = gzip.open if source_xml.suffix == '.gz' else open
    with opener(source_xml, 'rb') as source, gzip.open(destination, 'wt', encoding='utf-8') as out:
        events = iter(ElementTree.iterparse(source, events=('start', 'end')))
        _, root = next(events)
        if root.tag != 'osm':
            raise ValueError('도로 원본 XML 형식이 올바르지 않습니다.')
        depth = 1
        out.write('{')
        for event, element in events:
            if event == 'start':
                depth += 1
                continue
            depth -= 1
            if depth != 1:
                continue
            if element.tag == 'node':
                node_coords[int(element.get('id'))] = (float(element.get('lon')), float(element.get('lat')))
            elif element.tag == 'way':
                road_id = element.get('id')
                tags = {tag.get('k'): tag.get('v') for tag in element.findall('tag')}
                if valid_osm_id(road_id) and _editable(tags):
                    nodes = [int(node.get('ref')) for node in element.findall('nd')]
                    if (2 <= len(nodes) <= 2000 and len(nodes) == len(set(nodes))
                            and all(0 < node < 2**53 and node in node_coords for node in nodes)):
                        road = {'id': road_id, 'name': (tags.get('name') or '')[:200], 'nodes': nodes,
                                'coords': [node_coords[node] for node in nodes]}
                        if count:
                            out.write(',')
                        out.write(json.dumps(road_id) + ':')
                        json.dump(road, out, ensure_ascii=False, separators=(',', ':'))
                        count += 1
            root.clear()
        out.write('}')
    if not count:
        raise ValueError('보정할 수 있는 도로 구간이 원본에 없습니다.')
    return count


def _native_version(executable: Path) -> str:
    result = subprocess.run([str(executable), '--version'], capture_output=True, text=True, check=True, timeout=10)
    return result.stdout.strip().removeprefix('v')


def prepare(args) -> int:
    source, pack, source_bin, profiles, destination = [p.resolve() for p in
        (args.source, args.pack, args.osrm_bin, args.profiles, args.out)]
    if destination.exists():
        raise ValueError('도로 통행 보정 자료는 새 폴더 경로에 준비해 주세요.')
    if file_digest(source) != args.source_sha256.lower():
        raise ValueError('원본 도로 자료의 SHA-256이 확인한 값과 다릅니다.')
    base_fingerprint = graph_fingerprint(pack)
    version = _native_version(pack / 'bin/osrm-routed.exe')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version) or any(
            _native_version(source_bin / f'{tool}.exe') != version for tool in TOOLS):
        raise ValueError('원본 도로 팩과 보정 도구의 OSRM 버전이 다릅니다.')
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', args.data_version):
        raise ValueError('원본 도로 자료 기준일을 UTC ISO 형식으로 지정해 주세요.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='road-kit-', dir=destination.parent) as work:
        staging = Path(work) / 'kit'
        staging.mkdir()
        source_xml = Path(work) / 'base.osm.gz'
        shutil.copyfile(source, staging / SOURCE)
        subprocess.run([str(args.osmium.resolve()), 'cat', str(source), '-f', 'osm.gz', '-o', str(source_xml)],
                       check=True, timeout=120)
        count = write_catalog(source_xml, staging / CATALOG)
        (staging / 'bin').mkdir()
        for path in sorted(source_bin.glob('*.dll')):
            shutil.copyfile(path, staging / 'bin' / path.name)
        for tool in TOOLS:
            shutil.copyfile(source_bin / f'{tool}.exe', staging / 'bin' / f'{tool}.exe')
        (staging / 'profiles').mkdir()
        shutil.copyfile(profiles / 'car.lua', staging / 'profiles/car.lua')
        for lua in sorted((profiles / 'lib').rglob('*.lua')):
            relative = lua.relative_to(profiles)
            paths = staging / 'profiles' / relative
            paths.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(lua, paths)
        (staging / 'licenses').mkdir()
        for path in sorted((pack / 'licenses').iterdir()):
            shutil.copyfile(path, staging / 'licenses' / path.name)
        notice = ('# Local one-way correction kit\n\n'
                  'Includes the clean original district extract, an eligible full-way catalog derived from it, '
                  'and the matching Windows tools and Lua profile. Copyright and original sources follow.\n\n'
                  + (pack / 'SOURCES.md').read_text(encoding='utf-8'))
        (staging / 'SOURCES.md').write_text(notice, encoding='utf-8')
        manifest = {'schema': 1, 'base_fingerprint': base_fingerprint, 'version': version,
                    'provenance': {'data_version': args.data_version, 'source_sha256': file_digest(staging / SOURCE),
                                   'source_notice': 'SOURCES.md'},
                    'files': {file.relative_to(staging).as_posix(): file_digest(file)
                              for file in sorted(staging.rglob('*')) if file.is_file()}}
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        kit_files(staging, base_fingerprint)
        if len(load_kit(staging, base_fingerprint)) != count:
            raise ValueError('도로 구간 목록의 검증 건수가 맞지 않습니다.')
        staging.rename(destination)
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--source-sha256', required=True)
    parser.add_argument('--data-version', required=True)
    parser.add_argument('--osmium', required=True, type=Path)
    parser.add_argument('--pack', required=True, type=Path)
    parser.add_argument('--osrm-bin', required=True, type=Path)
    parser.add_argument('--profiles', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    print(f'검증된 도로 통행 보정 자료를 준비했습니다. 편집 가능한 도로 {prepare(args):,}개')


if __name__ == '__main__':
    main()
