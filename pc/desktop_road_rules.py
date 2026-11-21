"""로컬 도로 통행 규칙을 검증한 뒤 OSRM 도로망을 교체하고 복원한다."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
import uuid
import zlib

from dgpc import osrm


_SCHEMA = 1
_BASE = 'base'
_ID = re.compile(r'[0-9a-f]{32}\Z')
_DIRECTIONS = ('forward', 'reverse')
_SUFFIXES = ('.properties', '.partition', '.cells', '.cell_metrics', '.mldgr')
_STATE_LIMIT = 256 * 1024
_RULE_LIMIT = 200
_BACKUP_BLOCK = 1024 * 1024
_BACKUP_BYTES = 512 * _BACKUP_BLOCK
_BACKUP_ROWS = 2048
_BACKUP_NAME = re.compile(r'state\.json|[0-9a-f]{32}/(?:rules\.json|trial\.osrm\.[A-Za-z0-9_.-]{0,63}[A-Za-z0-9])')
_SEARCH_METRES = 150
_SNAP_METRES = 3
_KST = timezone(timedelta(hours=9))
_fetch = osrm.fetch


class RoadRulesError(ValueError):
    """관리 화면에서 표시할 수 있는 도로 통행 정보 오류."""


def load_kit(directory, expected_fingerprint):
    from desktop_road_build import load_kit as load

    return load(directory, expected_fingerprint)


def build_graph(directory, out, rules, *, expected_fingerprint):
    from desktop_road_build import build_graph as build

    return build(directory, out, rules, expected_fingerprint=expected_fingerprint)


def graph_fingerprint(pack):
    from desktop_road_build import graph_fingerprint as fingerprint

    return fingerprint(pack)


def _file_hash(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def _id(value):
    return isinstance(value, str) and bool(_ID.fullmatch(value))


def _local_path(path):
    try:
        return path.resolve() == path
    except (OSError, RuntimeError):
        return False


def _lease_directory(directory):
    """같은 상태 폴더를 사용하는 두 실행 앱의 도로 그래프 전환을 방지한다."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'road-rules.lock'
        if not _local_path(path):
            raise OSError
        file = path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return file
        except OSError:
            file.close()
            raise
    except OSError as error:
        raise RoadRulesError('도로 정보 폴더를 열 수 없습니다. 다른 실행 앱이 이 폴더를 사용하는지 확인하세요.') from error


def _document_bytes(path):
    if not _local_path(path) or not path.is_file() or path.stat().st_size > _STATE_LIMIT:
        raise ValueError('invalid road rules document')
    return path.read_bytes()


def _parse_document(raw):
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get('schema') != _SCHEMA:
        raise ValueError('invalid road rules schema')
    return document


def _read_document(path):
    return _parse_document(_document_bytes(path))


def _write_document(path, document):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, prefix='.road-rules-', delete=False) as file:
            temporary = Path(file.name)
            json.dump(document, file, ensure_ascii=False, separators=(',', ':'))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise RoadRulesError('도로 통행 정보를 저장할 수 없습니다. 저장 공간과 쓰기 권한을 확인하세요.') from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _graph_hashes(dataset):
    if not _local_path(dataset.parent):
        raise ValueError('nonlocal generation')
    files = sorted(dataset.parent.glob(dataset.name + '.*'))
    if not {dataset.with_name(dataset.name + suffix) for suffix in _SUFFIXES}.issubset(files):
        raise ValueError('incomplete generation')
    hashes = {}
    for file in files:
        if not _local_path(file) or not file.is_file() or not file.stat().st_size:
            raise ValueError('incomplete generation')
        hashes[file.name] = _file_hash(file)
    return hashes


def _generation_record(root, generation, fingerprint):
    dataset = root / generation / 'trial.osrm'
    document = _read_document(dataset.parent / 'rules.json')
    if document.get('graph_hashes') != _graph_hashes(dataset):
        raise RoadRulesError('도로 그래프가 미리보기 때와 다릅니다. 도로 팩을 다시 확인하세요.')
    records = document.get('rules')
    if (document.get('base_fingerprint') != fingerprint or not isinstance(records, list)
            or len(records) > _RULE_LIMIT):
        raise ValueError('invalid recorded rules')
    seen = set()
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get('road_id'), str)
                or not record['road_id'].isdigit() or record['road_id'] in seen
                or record.get('direction') not in _DIRECTIONS
                or not all(isinstance(record.get(key), str) for key in ('name', 'checked_on', 'evidence'))):
            raise ValueError('invalid rule')
        seen.add(record['road_id'])
    return document


def _snapshot_files(root):
    """업무 계산에서 참조하는 적용판·이전판과 그 기준 팩만 선택한다."""
    raw = _document_bytes(root / 'state.json')
    state = _parse_document(raw)
    active, previous, fingerprint = state['active_id'], state['previous_id'], state['base_fingerprint']
    if (not (active == _BASE or _id(active)) or not (previous is None or previous == _BASE or _id(previous))
            or not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
        raise ValueError('invalid road version')
    files = {'state.json': hashlib.sha256(raw).hexdigest()}
    for generation in {active, previous} - {_BASE, None}:
        record = _generation_record(root, generation, fingerprint)
        for name, digest in record['graph_hashes'].items():
            files[f'{generation}/{name}'] = digest
        files[f'{generation}/rules.json'] = _file_hash(root / generation / 'rules.json')
    return state, files


def write_road_backup(con, schema, root):
    """SQLCipher 백업 안에만 적용 도로 정보 파일을 작은 조각으로 보관한다."""
    if not (root / 'state.json').exists() and not (root / 'state.json').is_symlink():
        return
    try:
        _, files = _snapshot_files(root)
        con.execute('SAVEPOINT dg_road_snapshot')
        try:
            con.execute(f"""CREATE TABLE {schema}.dg_backup_road_files (
                name TEXT NOT NULL, part INTEGER NOT NULL, bytes BLOB NOT NULL,
                PRIMARY KEY (name, part)) WITHOUT ROWID""")
            total = count = 0
            for name, expected in files.items():
                if not _BACKUP_NAME.fullmatch(name):
                    raise ValueError('invalid road filename')
                digest = hashlib.sha256()
                with (root / name).open('rb') as file:
                    for part, body in enumerate(iter(lambda: file.read(_BACKUP_BLOCK), b'')):
                        total += len(body)
                        count += 1
                        if total > _BACKUP_BYTES or count > _BACKUP_ROWS:
                            raise ValueError('oversize road archive')
                        digest.update(body)
                        con.execute(f'INSERT INTO {schema}.dg_backup_road_files VALUES (?,?,?)',
                                    (name, part, zlib.compress(body, level=1)))
                if digest.hexdigest() != expected:
                    raise ValueError('road snapshot changed')
            # 다른 프로세스가 생성한 백업도 적용판을 한 시점에만 묶어야 한다.
            if _file_hash(root / 'state.json') != files['state.json']:
                raise ValueError('road state changed')
        except BaseException:
            con.execute('ROLLBACK TO dg_road_snapshot')
            raise
        finally:
            con.execute('RELEASE dg_road_snapshot')
    except (KeyError, OSError, ValueError) as error:
        raise RoadRulesError('적용 도로 정보를 백업하지 못했습니다. 도로 자료와 저장 공간을 확인하세요.') from error


def stage_road_backup(con, root, pack):
    """백업의 상대 이름과 조각 크기를 검사한 뒤 격리 폴더에서 두 도로판을 확인한다."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='dg_backup_road_files'").fetchone():
        return None
    if pack is None:
        raise RoadRulesError('도로망이 포함된 백업입니다. 대상 PC의 기준 도로 팩을 --osrm-pack으로 지정하세요.')
    try:
        fingerprint = graph_fingerprint(Path(pack))
    except (OSError, ValueError) as error:
        raise RoadRulesError('복원할 PC의 기준 도로 팩을 읽지 못했습니다.') from error
    try:
        names, last_name, expected_part, total = set(), None, 0, 0
        rows = con.execute("""SELECT name, part, CASE WHEN length(bytes) <= ? THEN bytes ELSE NULL END
            FROM dg_backup_road_files ORDER BY name, part LIMIT ?""", (_BACKUP_BLOCK + 4096, _BACKUP_ROWS + 1))
        with ExitStack() as stack:
            for idx, (name, part, packed) in enumerate(rows):
                if (idx >= _BACKUP_ROWS or not isinstance(name, str) or not _BACKUP_NAME.fullmatch(name)
                        or not isinstance(packed, bytes)):
                    raise ValueError('invalid archived road file')
                if name != last_name:
                    if last_name is not None:
                        out.flush()
                        os.fsync(out.fileno())
                    stack.close()
                    names.add(name)
                    last_name, expected_part = name, 0
                    path = root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    out = stack.enter_context(path.open('xb'))
                if part != expected_part:
                    raise ValueError('missing archived road block')
                expected_part += 1
                reader = zlib.decompressobj()
                body = reader.decompress(packed, _BACKUP_BLOCK + 1)
                total += len(body)
                if (not body or len(body) > _BACKUP_BLOCK or not reader.eof or reader.unused_data
                        or total > _BACKUP_BYTES or name.endswith('.json') and out.tell() + len(body) > _STATE_LIMIT):
                    raise ValueError('invalid archived road block')
                out.write(body)
            if last_name is not None:
                out.flush()
                os.fsync(out.fileno())
        state, files = _snapshot_files(root)
        if names != files.keys():
            raise ValueError('unexpected archived road file')
    except (KeyError, OSError, ValueError, zlib.error) as error:
        raise RoadRulesError('백업의 도로 정보가 손상됐습니다. 다른 백업 파일을 확인하세요.') from error
    if state['base_fingerprint'] != fingerprint:
        raise RoadRulesError('백업과 기준 도로 팩 버전이 다릅니다. 백업 때 사용한 도로 팩을 지정하세요.')
    return state


@contextmanager
def road_restore_move_plan(state, incoming, retained):
    """활성 엔진을 배제한 채 도로 판을 바꿀 같은 볼륨의 이동 목록을 만든다."""
    root = state.resolve() / 'road-rules'
    if not _local_path(root):
        raise RoadRulesError('대상 도로 자료가 다른 위치를 가리킵니다. 자료 폴더를 확인하세요.')
    with _lease_directory(root):
        old = [p for p in root.iterdir() if p.name == 'state.json' or _id(p.name)]
        if any(not _local_path(p) or (not p.is_file() if p.name == 'state.json' else not p.is_dir()) for p in old):
            raise RoadRulesError('대상 도로 자료의 파일 형식을 확인하세요.')
        if old:
            retained.mkdir()
        staged = sorted(incoming.iterdir(), key=lambda p: p.name == 'state.json')
        moves = [(p, retained / p.name) for p in old] + [(p, root / p.name) for p in staged]
        try:
            yield moves
        finally:
            if old and retained.exists() and not any(retained.iterdir()):
                retained.rmdir()


def _valid_coords(coords):
    return (isinstance(coords, list) and len(coords) == 2
            and type(coords[0]) in (int, float) and -180 <= coords[0] <= 180
            and type(coords[1]) in (int, float) and -90 <= coords[1] <= 90)


def _distance(a, b):
    latitude = math.radians((a[1] + b[1]) / 2)
    return 111_195 * math.hypot((a[0] - b[0]) * math.cos(latitude), a[1] - b[1])


def _segment_distance(point, a, b):
    at_latitude = math.cos(math.radians(point[1]))
    x, y = (a[0] - point[0]) * at_latitude, a[1] - point[1]
    dx, dy = (b[0] - a[0]) * at_latitude, b[1] - a[1]
    length_squared = dx * dx + dy * dy
    offset = max(0, min(1, -(x * dx + y * dy) / length_squared)) if length_squared else 0
    return 111_195 * math.hypot(x + offset * dx, y + offset * dy)


def _probe_points(coords):
    # 교차로 바로 위의 좌표는 이웃 도로에 붙을 수 있어 양 끝에서 구간 안쪽을 고른다.
    lengths = [_distance(a, b) for a, b in zip(coords, coords[1:])]
    length = sum(lengths)
    if length < 12:
        raise RoadRulesError('양방향을 검증하기에 도로 구간이 너무 짧습니다.')
    inset = min(9, length / 5)

    def at(metres):
        for a, b, segment in zip(coords, coords[1:], lengths):
            if segment and metres <= segment:
                fraction = metres / segment
                return [x + (y - x) * fraction for x, y in zip(a, b)]
            metres -= segment
        return coords[-1]

    return at(inset), at(length - inset), length - 2 * inset


def _public_road(road):
    return {key: road[key] for key in ('id', 'name', 'coords')}


def _route(endpoint, a, b, deadline):
    timeout = min(10, deadline - time.monotonic()) if deadline is not None else 10
    if timeout <= 0:
        raise RoadRulesError('적용할 도로의 양방향 확인이 제한 시간을 넘었습니다.')
    pair = osrm.coordinate_path(((a[1], a[0]), (b[1], b[0])))
    result = _fetch(endpoint, f'/route/v1/driving/{pair}?geometries=geojson&overview=full&annotations=nodes'
                    f'&radiuses={_SNAP_METRES};{_SNAP_METRES}', timeout=timeout)
    if result.get('code') == 'NoRoute':
        return {'code': 'NoRoute', 'distance_m': None, 'duration_s': None, 'coordinates': []}, set()
    try:
        if result.get('code') != 'Ok' or len(result['waypoints']) != 2:
            raise ValueError
        for actual, expected in zip(result['waypoints'], (a, b)):
            if not _valid_coords(actual['location']) or _distance(actual['location'], expected) > _SNAP_METRES + 0.25:
                raise ValueError
        route = result['routes'][0]
        geometry = route['geometry']
        coords = geometry['coordinates']
        nodes = route['legs'][0]['annotation']['nodes']
        distance, duration = route['distance'], route['duration']
        if (geometry['type'] != 'LineString' or not isinstance(coords, list) or len(coords) < 2
                or len(coords) > 20_000 or not all(_valid_coords(point) for point in coords)
                or not isinstance(nodes, list) or len(nodes) < 2 or not all(type(n) is int and n > 0 for n in nodes)
                or type(distance) not in (int, float) or not 0 < distance < 1_000_000
                or type(duration) not in (int, float) or not 0 < duration < 1_000_000):
            raise ValueError
    except (KeyError, TypeError, IndexError, ValueError) as error:
        raise RoadRulesError('도로 끝점에서 경로를 검증할 수 없습니다. 구간 또는 도로 자료를 확인하세요.') from error
    return {'code': 'Ok', 'distance_m': distance, 'duration_s': duration, 'coordinates': coords}, set(zip(nodes, nodes[1:]))


def _probe(endpoint, a, b, *, deadline=None):
    forward, forward_edges = _route(endpoint, a, b, deadline)
    reverse, reverse_edges = _route(endpoint, b, a, deadline)
    return {'forward': forward, 'reverse': reverse}, {'forward': forward_edges, 'reverse': reverse_edges}


def _verify_preview(road, direction, length, paths, edges):
    nodes = road['nodes']
    forward = set(zip(nodes, nodes[1:]))
    reverse = {(b, a) for a, b in forward}
    opposite = 'reverse' if direction == 'forward' else 'forward'
    allowed, forbidden = (forward, reverse) if direction == 'forward' else (reverse, forward)
    given = paths[direction]
    if (given['code'] != 'Ok' or not edges[direction] & allowed or edges[direction] & forbidden
            or not 0.8 * length <= given['distance_m'] <= length * 1.25 + 5
            or edges[direction] - allowed):
        raise RoadRulesError('허용 방향이 선택한 도로를 그대로 통과하는지 확인할 수 없습니다.')
    if paths[opposite]['code'] == 'Ok' and (edges[opposite] & forbidden or paths[opposite]['distance_m'] <= length + 3):
        raise RoadRulesError('반대 방향에서 선택 구간을 피하는 경로가 확인되지 않았습니다.')


class RoadRulesManager:
    def __init__(self, base_pack: Path, state: Path, make_runtime):
        self._pack = base_pack.resolve()
        self._kit = self._pack.parent / 'osrm-edit-kit'
        self._root = state.resolve() / 'road-rules'
        self._state_file = self._root / 'state.json'
        self._factory = make_runtime
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._load_lock = threading.Lock()
        self._active = _BASE
        self._previous = None
        self._pending = None
        self._rules = []
        self._catalog = None
        self._bounds = None
        self._kit_manifest_hash = None
        self._active_engine = None
        self._retired = None
        self._lease = None
        self._fingerprint = None
        self._state_error = ''
        self._kit_error = ''
        self._closed = False
        try:
            if not _local_path(self._root):
                raise ValueError('nonlocal state')
            if self._root.exists() or self._kit.is_dir():
                self._lease = _lease_directory(self._root)
                self._fingerprint = graph_fingerprint(self._pack)
            else:
                try:
                    self._fingerprint = graph_fingerprint(self._pack)
                except (OSError, ValueError):
                    pass  # 조회 전용 팩은 런타임에서 별도로 검증한다.
            if self._state_file.exists() or self._state_file.is_symlink():
                document = _read_document(self._state_file)
                active, previous = document['active_id'], document['previous_id']
                if not self._fingerprint or document.get('base_fingerprint') != self._fingerprint:
                    raise ValueError('base pack changed')
                if not (active == _BASE or _id(active)) or previous is not None and not (previous == _BASE or _id(previous)):
                    raise ValueError('invalid generation')
                self._rules = self._generation(active)
                self._active, self._previous = active, previous
            self._active_engine = self._factory(self._dataset(self._active))
        except RoadRulesError as error:
            self._state_error = str(error)
        except (KeyError, OSError, ValueError):
            self._state_error = '저장한 도로 정보나 기준 도로 팩을 읽을 수 없습니다. 적용 자료를 확인하세요.'

    def _dataset(self, generation):
        return None if generation == _BASE else self._root / generation / 'trial.osrm'

    def _generation(self, generation):
        if generation == _BASE:
            return []
        if not _id(generation):
            raise ValueError('invalid generation')
        return _generation_record(self._root, generation, self._fingerprint)['rules']

    @property
    def revision(self):
        with self._lock:
            return self._active

    @property
    def base_fingerprint(self):
        return self._fingerprint

    @property
    def status(self):
        with self._lock:
            return self._active_engine.status if self._active_engine else 'INVALID_PACK'

    def local_endpoint(self, *, retry=False):
        with self._lock:
            return self._active_engine.local_endpoint(retry=retry) if self._active_engine else ('', 'INVALID_PACK')

    def wait_ready(self, timeout):
        with self._lock:
            current = self._active_engine
        if current is None:
            return '', 'INVALID_PACK'
        result = current.wait_ready(timeout)
        with self._lock:
            if self._active_engine is current:
                return result
            return self.local_endpoint()

    def _need_state(self):
        if self._state_error:
            raise RoadRulesError(self._state_error)
        if not self._lease or not self._fingerprint:
            raise RoadRulesError('저장한 도로 정보를 열 수 없습니다.')

    def _kit_hash(self):
        manifest = self._kit / 'manifest.json'
        if not _local_path(manifest) or not manifest.is_file() or manifest.stat().st_size > _STATE_LIMIT:
            raise ValueError('invalid road kit manifest')
        return _file_hash(manifest)

    def _need_kit(self):
        if self._state_error:
            raise RoadRulesError(self._state_error)
        if not self._kit.is_dir():
            raise RoadRulesError('이 PC에 도로 편집용 자료가 설치되어 있지 않습니다.')
        self._need_state()
        with self._load_lock:
            try:
                manifest_hash = self._kit_hash()
                if self._kit_manifest_hash and self._kit_manifest_hash != manifest_hash:
                    raise ValueError('road kit changed')
                if self._catalog is None and not self._kit_error:
                    self._catalog = load_kit(self._kit, self._fingerprint)
                    if self._kit_hash() != manifest_hash:
                        raise ValueError('road kit changed during load')
                    self._kit_manifest_hash = manifest_hash
                    self._bounds = []
                    for road in self._catalog.values():
                        x, y = zip(*road['coords'])
                        self._bounds.append((min(x), min(y), max(x), max(y), road))
            except (OSError, ValueError, KeyError, TypeError):
                self._catalog = None
                self._kit_error = '도로 편집용 자료를 읽을 수 없습니다. 도로 팩과 편집 자료를 확인한 뒤 PC 프로그램을 다시 여세요.'
            if self._kit_error:
                raise RoadRulesError(self._kit_error)
        return self._catalog

    def summary(self):
        try:
            self._need_kit()
            message = ''
        except RoadRulesError as error:
            message = str(error)
        with self._lock:
            result = {'available': not message, 'active_id': self._active, 'previous_id': self._previous,
                      'rules': self._rules, 'busy': self._operation.locked(), 'pending': self._pending}
        if message:
            result['reason'] = message
        return result

    def roads(self, lat, lon):
        try:
            if isinstance(lat, bool) or isinstance(lon, bool):
                raise ValueError
            point = [float(lon), float(lat)]
        except (TypeError, ValueError) as error:
            raise RoadRulesError('도로를 찾을 지도 위치가 올바르지 않습니다.') from error
        if not _valid_coords(point):
            raise RoadRulesError('도로를 찾을 지도 위치가 올바르지 않습니다.')
        self._need_kit()
        y_margin = _SEARCH_METRES / 111_195
        x_margin = y_margin / max(0.01, math.cos(math.radians(point[1])))
        nearby = []
        for west, south, east, north, road in self._bounds:
            if not (west - x_margin <= point[0] <= east + x_margin and south - y_margin <= point[1] <= north + y_margin):
                continue
            coords = road['coords']
            metres = min(_segment_distance(point, a, b) for a, b in zip(coords, coords[1:]))
            if metres <= _SEARCH_METRES:
                nearby.append((metres, road['id'], road))
        nearby.sort(key=lambda match: (match[0], match[1]))
        return {'roads': [{**_public_road(road), 'distance_m': round(metres)} for metres, _, road in nearby[:8]]}

    @contextmanager
    def _change(self):
        if not self._operation.acquire(blocking=False):
            raise RoadRulesError('다른 도로 통행 정보를 처리 중입니다. 완료된 뒤 다시 시도하세요.')
        try:
            with self._lock:
                if self._closed:
                    raise RoadRulesError('PC 서버가 종료 중입니다.')
            yield
        finally:
            self._operation.release()

    def _payload(self, body):
        if not isinstance(body, dict):
            raise RoadRulesError('도로 통행 정보가 올바르지 않습니다.')
        return body

    def _new_generation(self):
        try:
            if not _local_path(self._root):
                raise OSError('nonlocal state')
            self._root.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            directory = self._root / token
            directory.mkdir()
            return token, directory
        except OSError as error:
            raise RoadRulesError('도로 그래프를 만들 폴더를 열 수 없습니다.') from error

    def _prune(self):
        # 운영·복원·미리보기 폴더와 퇴역 엔진이 아직 읽는 자료는 보관한다.
        if self._state_error or self._lease is None or not _local_path(self._root) or not self._root.is_dir():
            return
        with self._lock:
            keep = {self._active, self._previous}
            if self._pending:
                keep.add(self._pending['preview_id'])
            if self._retired:
                keep.add(self._retired[0])
        try:
            directories = list(self._root.iterdir())
        except OSError:
            return
        for directory in directories:
            if _id(directory.name) and directory.name not in keep and _local_path(directory) and directory.is_dir():
                try:
                    shutil.rmtree(directory)
                except OSError:
                    pass

    @staticmethod
    def _ready(runtime):
        endpoint, status = runtime.local_endpoint(retry=True)
        if status == 'STARTING':
            endpoint, status = runtime.wait_ready(30)
        if status != 'READY' or not osrm.valid_url(endpoint):
            raise RoadRulesError('도로 엔진을 시작할 수 없습니다. 도로 팩을 확인하세요.')
        return endpoint

    def preview(self, body):
        body = self._payload(body)
        with self._change():
            catalog = self._need_kit()
            road_id, direction = body.get('road_id'), body.get('direction')
            road = catalog.get(road_id) if isinstance(road_id, str) else None
            if road is None or direction not in _DIRECTIONS:
                raise RoadRulesError('도로 구간과 허용 방향을 고르세요.')
            checked_on, evidence = body.get('checked_on'), body.get('evidence')
            try:
                checked_date = date.fromisoformat(checked_on)
                if checked_on != checked_date.isoformat() or checked_date > datetime.now(_KST).date():
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise RoadRulesError('오늘 또는 그 이전의 확인일을 적어 주세요.') from error
            if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 300:
                raise RoadRulesError('도로 표지 등 확인 근거를 300자 이내로 적어 주세요.')
            record = {'road_id': road_id, 'name': road['name'], 'direction': direction,
                      'checked_on': checked_on, 'evidence': evidence.strip()}
            with self._lock:
                active = self._active
                if body.get('base_id') != active:
                    raise RoadRulesError('도로 정보가 바뀌었습니다. 새로고침한 뒤 다시 확인하세요.')
                previous_rules = {rule['road_id']: rule for rule in self._rules}
                previous_rules[road_id] = record
                if len(previous_rules) > _RULE_LIMIT:
                    raise RoadRulesError('등록할 수 있는 도로 구간 수를 초과했습니다.')
                current = self._active_engine
                self._pending = None
            a, b, expected_length = _probe_points(road['coords'])
            self._prune()
            before, _ = _probe(self._ready(current), a, b)
            generation, directory = self._new_generation()
            candidate = None
            try:
                dataset = build_graph(self._kit, directory, {key: rule['direction'] for key, rule in previous_rules.items()},
                                      expected_fingerprint=self._fingerprint)
                self._need_kit()  # 빌드와 카탈로그가 같은 편집 자료를 가리키는지 재확인한다.
                if dataset != self._dataset(generation):
                    raise RoadRulesError('도로 엔진이 시험 그래프를 만들지 못했습니다.')
                hashes = _graph_hashes(dataset)
                candidate = self._factory(dataset)
                endpoint = self._ready(candidate)
                deadline = time.monotonic() + 30
                after, edges = _probe(endpoint, a, b, deadline=deadline)
                _verify_preview(road, direction, expected_length, after, edges)
                for key, retained in previous_rules.items():
                    if key == road_id:
                        continue
                    try:
                        existing = catalog[key]
                        start, end, length = _probe_points(existing['coords'])
                        paths, pairs = _probe(endpoint, start, end, deadline=deadline)
                        _verify_preview(existing, retained['direction'], length, paths, pairs)
                    except (KeyError, RoadRulesError) as error:
                        name = retained['name'] or '이름 없는 도로'
                        raise RoadRulesError(f'이미 적용한 도로 구간({name})을 함께 검증하지 못했습니다. 변경 구간을 확인하세요.') from error
                _write_document(directory / 'rules.json', {'schema': _SCHEMA, 'base_fingerprint': self._fingerprint,
                                                            'rules': list(previous_rules.values()), 'graph_hashes': hashes})
                summary = {'preview_id': generation, 'base_id': active, 'road': _public_road(road), 'direction': direction,
                           'checked_on': checked_on, 'evidence': record['evidence'], 'before': before, 'after': after}
                with self._lock:
                    self._pending = summary
            except RoadRulesError:
                raise
            except (OSError, ValueError) as error:
                raise RoadRulesError('새 도로 그래프를 준비하지 못했습니다. 편집 자료와 저장 공간을 확인하세요.') from error
            finally:
                if candidate is not None:
                    candidate.close()
                self._prune()
            return summary

    def _switch(self, target, guard):
        try:
            if graph_fingerprint(self._pack) != self._fingerprint:
                raise RoadRulesError('기준 도로 팩이 바뀌었습니다. PC 프로그램을 다시 열어 확인하세요.')
            records = self._generation(target)
        except RoadRulesError:
            raise
        except (OSError, ValueError) as error:
            raise RoadRulesError('적용할 도로 그래프를 읽을 수 없습니다.') from error
        candidate = self._factory(self._dataset(target))
        switched = False
        try:
            self._ready(candidate)
            with guard(), self._lock:
                if self._closed or candidate.status != 'READY':
                    raise RoadRulesError('도로 그래프를 전환할 수 없습니다.')
                previous_engine = self._retired
                old = self._active
                _write_document(self._state_file, {'schema': _SCHEMA, 'base_fingerprint': self._fingerprint,
                                                   'active_id': target, 'previous_id': old})
                self._retired = (old, self._active_engine)
                self._active_engine = candidate
                self._active, self._previous = target, old
                self._rules, self._pending = records, None
                switched = True
        finally:
            if not switched:
                candidate.close()
        if previous_engine:
            previous_engine[1].close()
        self._prune()

    def apply(self, body, guard=nullcontext):
        with self._change():
            self._need_kit()
            preview_id = self._payload(body).get('preview_id')
            with self._lock:
                pending = self._pending
                if not pending or pending['preview_id'] != preview_id or pending['base_id'] != self._active:
                    raise RoadRulesError('미리보기가 바뀌었습니다. 도로 구간을 다시 비교하세요.')
            self._switch(preview_id, guard)
        return self.summary()

    def rollback(self, body, guard=nullcontext):
        with self._change():
            self._need_state()
            active_id = self._payload(body).get('active_id')
            with self._lock:
                if self._active != active_id or self._previous is None:
                    raise RoadRulesError('복원할 도로 정보가 바뀌었습니다. 새로고침해 확인하세요.')
                previous = self._previous
            self._switch(previous, guard)
        return self.summary()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        with self._operation:
            with self._lock:
                current, retired = self._active_engine, self._retired
                self._active_engine, self._retired, self._pending = None, None, None
            try:
                if current:
                    current.close()
                if retired:
                    retired[1].close()
                self._prune()
            finally:
                if self._lease is not None:
                    self._lease.close()
                    self._lease = None
