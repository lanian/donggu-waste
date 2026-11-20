"""검증된 오프라인 보정 자료로 새 Windows OSRM 도로망을 만든다."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory


EDIT_KIT = 'osrm-edit-kit'
SOURCE = 'base.osm.pbf'
CATALOG = 'roads.json.gz'
TOOLS = ('osrm-extract', 'osrm-partition', 'osrm-customize')
REQUIRED = {SOURCE, CATALOG, 'SOURCES.md', 'profiles/car.lua', *(f'bin/{exe}.exe' for exe in TOOLS)}
_KIT_MEMBER = re.compile(r'(?:base\.osm\.pbf|roads\.json\.gz|SOURCES\.md|'
                         r'bin/(?:osrm-(?:extract|partition|customize)\.exe|[\w.-]+\.dll)|'
                         r'profiles/(?:lib/)*[\w.-]+\.lua|licenses/[\w.-]+\.(?:txt|md))')
_HASH = re.compile(r'[0-9a-f]{64}')
_VERSION = re.compile(r'\d+\.\d+\.\d+')
_WAY_ID = re.compile(r'[1-9][0-9]{0,15}')
_DIRECTION = {'forward': 1, 'reverse': -1}
_MAX_CATALOG = 64 * 1024 * 1024
_BUILD_TIMEOUT = 90


class RoadKitError(ValueError):
    """업무 화면에 그대로 표시할 수 있는 보정 자료 오류."""


def file_digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _regular(path: Path) -> bool:
    return not path.is_symlink() and path.resolve() == path and path.is_file() and path.stat().st_size > 0


def graph_fingerprint(pack: Path) -> str:
    """기본 도로망의 전체 파일 이름과 내용을 순서대로 확인한다."""
    data = pack.resolve() / 'data'
    try:
        files = sorted(data.glob('gwangju.osrm*'), key=lambda path: path.name)
        required = {'gwangju.osrm.properties', 'gwangju.osrm.mldgr'}
        if (data.is_symlink() or data.resolve() != data or not required <= {path.name for path in files}
                or any(not _regular(path) for path in files)):
            raise RoadKitError('기본 도로 팩의 도로망 파일을 확인해 주세요.')
        digest = hashlib.sha256()
        for path in files:
            digest.update(f'data/{path.name}\0{file_digest(path)}\n'.encode('utf-8'))
        return digest.hexdigest()
    except OSError as exc:
        raise RoadKitError('기본 도로 팩의 도로망 파일을 읽을 수 없습니다.') from exc


def _verified_manifest(directory: Path, expected_fingerprint: str | None = None) -> dict:
    manifest_file = directory / 'manifest.json'
    if not _regular(manifest_file) or manifest_file.stat().st_size > 128 * 1024:
        raise RoadKitError('도로 통행 보정 자료의 검증 목록을 찾을 수 없습니다.')
    try:
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        fingerprint, files = manifest['base_fingerprint'], manifest['files']
        version, provenance = manifest['version'], manifest['provenance']
        if (type(manifest['schema']) is not int or manifest['schema'] != 1 or not _HASH.fullmatch(fingerprint)
                or not _VERSION.fullmatch(version) or not isinstance(provenance, dict) or not isinstance(files, dict)
                or not REQUIRED <= files.keys() or not any(name.startswith('licenses/') for name in files)
                or any(not _KIT_MEMBER.fullmatch(name) or not _HASH.fullmatch(digest) for name, digest in files.items())):
            raise ValueError
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise RoadKitError('도로 통행 보정 자료의 검증 목록 형식이 잘못되었습니다.') from exc
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise RoadKitError('도로 통행 보정 자료와 현재 도로 팩 버전이 다릅니다.')
    try:
        actual = set()
        for path in directory.rglob('*'):
            if path.is_symlink() or path.resolve() != path:
                raise RoadKitError('도로 통행 보정 자료에 다른 위치를 가리키는 파일이 있습니다.')
            if not path.is_dir():
                actual.add(path.relative_to(directory).as_posix())
        if actual != set(files) | {'manifest.json'} or any(
                not _regular(directory / name) or file_digest(directory / name) != digest
                for name, digest in files.items()):
            raise RoadKitError('도로 통행 보정 자료의 구성이나 파일 해시가 맞지 않습니다.')
    except OSError as exc:
        raise RoadKitError('도로 통행 보정 자료를 읽을 수 없습니다.') from exc
    return manifest


def kit_files(directory: Path, expected_fingerprint: str) -> list[Path]:
    """배포 시 검증된 보정 자료의 상대 경로 목록."""
    manifest = _verified_manifest(directory.resolve(), expected_fingerprint)
    return sorted([Path('manifest.json'), *(Path(name) for name in manifest['files'])])


def valid_osm_id(value: str) -> bool:
    return isinstance(value, str) and bool(_WAY_ID.fullmatch(value)) and int(value) < 2**53


def _read_catalog(directory: Path) -> dict:
    try:
        with gzip.open(directory / CATALOG, 'rb') as source:
            raw = source.read(_MAX_CATALOG + 1)
        if len(raw) > _MAX_CATALOG:
            raise ValueError
        roads = json.loads(raw)
        if not isinstance(roads, dict) or not roads:
            raise ValueError
        for road_id, road in roads.items():
            if not valid_osm_id(road_id) or set(road) != {'id', 'name', 'nodes', 'coords'}:
                raise ValueError
            if road['id'] != road_id or not isinstance(road['name'], str) or len(road['name']) > 200:
                raise ValueError
            coords, nodes = road['coords'], road['nodes']
            if (not isinstance(coords, list) or not isinstance(nodes, list) or not 2 <= len(coords) == len(nodes) <= 2000
                    or any(type(node) is not int or not 0 < node < 2**53 for node in nodes) or nodes[0] == nodes[-1]):
                raise ValueError
            for point in coords:
                if (not isinstance(point, list) or len(point) != 2
                        or any(type(axis) not in (int, float) or not math.isfinite(axis) for axis in point)
                        or not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90)):
                    raise ValueError
    except (OSError, EOFError, KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise RoadKitError('도로 구간 목록이 비어 있거나 형식이 올바르지 않습니다.') from exc
    return roads


def load_kit(directory: Path, expected_fingerprint: str) -> dict:
    """원본 도로 팩에 맞는 검증된 도로 구간과 노드 순서를 읽는다."""
    directory = directory.resolve()
    _verified_manifest(directory, expected_fingerprint)
    return _read_catalog(directory)


def _profile(rules: dict[str, str]) -> str:
    edits = ''.join(f'    [{road_id}] = {_DIRECTION[direction]},\n' for road_id, direction in sorted(rules.items()))
    return f'''local p = "profiles"
package.path = p .. "/?.lua;" .. p .. "/?/init.lua;" .. package.path
local car = dofile(p .. "/car.lua")
local process_way = car.process_way
local directions = {{
{edits}}}
car.process_way = function(profile, way, result, relations)
    process_way(profile, way, result, relations)
    local allowed = directions[way:id()]
    if allowed == 1 then
        result.backward_mode = mode.inaccessible
    elseif allowed == -1 then
        result.forward_mode = mode.inaccessible
    end
end
return car
'''


def build_graph(directory: Path, out: Path, rules: dict[str, str], *,
                expected_fingerprint: str | None = None) -> Path:
    """독립 폴더에 원본의 허용 진행 방향만 바꾼 MLD 도로망을 생성한다."""
    directory, out = directory.resolve(), out.resolve()
    manifest = _verified_manifest(directory, expected_fingerprint)
    roads = _read_catalog(directory)
    if (not isinstance(rules, dict) or not rules or any(not valid_osm_id(road_id) or road_id not in roads
            or not isinstance(direction, str) or direction not in _DIRECTION for road_id, direction in rules.items())):
        raise RoadKitError('보정할 도로 구간과 허용 방향을 확인해 주세요.')
    try:
        out.mkdir(parents=True, exist_ok=True)
        if any(out.iterdir()):
            raise RoadKitError('새 도로 팩을 만들려면 빈 작업 폴더가 필요합니다.')
    except OSError as exc:
        raise RoadKitError('새 도로 팩 작업 폴더를 열 수 없습니다.') from exc
    graph = 'trial.osrm'
    steps = [('도로망 읽기', 'osrm-extract', ['-p', 'rules.lua', 'trial.osm.pbf']),
             ('도로망 분할', 'osrm-partition', [graph]),
             ('이동시간 계산', 'osrm-customize', [graph])]
    stamp = manifest['provenance'].get('data_version')
    if isinstance(stamp, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', stamp):
        steps[0][2].extend(['--data_version', stamp])
    try:
        with TemporaryDirectory(prefix='road-build-', dir=out) as workspace:
            # Windows OSRM의 명령행은 한글 경로에서 손상될 수 있어 작업 위치와 상대 이름을 구분한다.
            work = Path(workspace).resolve()
            if work.parent != out:
                raise RoadKitError('새 도로 팩 작업 폴더가 올바르지 않습니다.')
            shutil.copyfile(directory / SOURCE, work / 'trial.osm.pbf')
            shutil.copytree(directory / 'profiles', work / 'profiles')
            (work / 'rules.lua').write_text(_profile(rules), encoding='ascii')
            for title, tool, args in steps:
                try:
                    subprocess.run([str(directory / 'bin' / f'{tool}.exe'), '-t', '4', *args], cwd=work,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=_BUILD_TIMEOUT, check=True)
                except subprocess.TimeoutExpired as exc:
                    raise RoadKitError(f'{title} 단계가 제한 시간을 넘었습니다.') from exc
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise RoadKitError(f'{title} 단계에서 새 도로 팩을 만들 수 없습니다.') from exc
            if any(not _regular(work / f'{graph}.{suffix}')
                   for suffix in ('properties', 'partition', 'cells', 'cell_metrics', 'mldgr')):
                raise RoadKitError('새 도로 팩 파일이 모두 만들어지지 않았습니다.')
            generated = list(work.glob(f'{graph}.*'))
            if any(path.is_symlink() or path.resolve() != path or not path.is_file() for path in generated):
                raise RoadKitError('새 도로 팩에 예상과 다른 파일이 있습니다.')
            for path in generated:
                path.rename(out / path.name)
    except OSError as exc:
        raise RoadKitError('새 도로 팩 작업 자료를 읽거나 쓸 수 없습니다.') from exc
    return out / graph
