"""로컬 OSRM 통신과 오프라인 팩의 공통 계약."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

PACK_DIR = 'osrm-pack'
EXECUTABLE = Path('bin/osrm-routed.exe')
DATASET = Path('data/gwangju.osrm')
MLD_FILES = {DATASET.with_suffix(ending) for ending in ('.osrm.partition', '.osrm.cells', '.osrm.cell_metrics', '.osrm.mldgr')}
MAX_PACK_TABLE_POINTS = 200
MAX_PACK_TABLE_CALLS = 64
TABLE_TIMEOUT = 10
MAX_RESPONSE = 16 * 1024 * 1024
TOLL_EXCLUSION_UNAVAILABLE = 'TOLL_EXCLUSION_UNAVAILABLE'

# EXE 업무 서버의 수명 동안만 설정된다. 예약 포트나 프로세스 정보는 DB에 저장하지 않는다.
bundled = None


def pack_files(root: Path) -> list[Path]:
    """동봉 가능한 상대 파일 경로. 실제 도로 자료 호환 여부는 osrm-routed가 기동할 때 검사한다."""
    root = root.resolve()
    patterns = {
        'bin': r'osrm-routed\.exe|[\w.-]+\.dll',
        'data': r'gwangju\.osrm(?:\.[a-z0-9_]+)*',
        'licenses': r'[\w.-]+\.(?:txt|md)',
    }

    def regular(path):
        return not path.is_symlink() and path.resolve() == path and path.is_file() and path.stat().st_size > 0

    found = set()
    source = root / 'SOURCES.md'
    try:
        if not regular(source) or {p.name for p in root.iterdir()} != {'bin', 'data', 'licenses', 'SOURCES.md'}:
            raise ValueError('OSRM 팩의 bin, data, SOURCES.md, licenses를 확인해 주세요.')
        found.add(source.relative_to(root))
        for folder, pattern in patterns.items():
            directory = root / folder
            if directory.is_symlink() or directory.resolve() != directory:
                raise ValueError('OSRM 팩의 폴더는 팩 안에 있어야 합니다.')
            for path in directory.iterdir():
                if path.suffix.lower() == '.pbf' or not re.fullmatch(pattern, path.name, re.IGNORECASE) or not regular(path):
                    raise ValueError(f'OSRM 팩에 포함할 수 없는 파일: {path.relative_to(root).as_posix()}')
                found.add(path.relative_to(root))
        if not {EXECUTABLE, *MLD_FILES} <= found or not any(p.parent.name == 'licenses' for p in found):
            raise ValueError('OSRM 팩의 실행 파일·MLD 자료·라이선스가 필요합니다.')
    except OSError as error:
        raise ValueError('OSRM 팩의 입력 파일을 읽지 못했습니다.') from error
    return sorted(found)


_URL = re.compile(r'http://(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?/?')


def _address(url):
    match = _URL.fullmatch(url) if isinstance(url, str) else None
    if match and (port := int(match[2] or '80')) in range(1, 65536):
        return match[1], port
    return None


def valid_url(url: str) -> bool:
    return _address(url) is not None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url: str, target: str, *, timeout: float) -> dict:
    """주소를 재검사하고 루프백 숫자 주소로만 연결한다. 프록시·리다이렉트·DNS를 쓰지 않는다."""
    address = _address(url)
    if not address:
        return {}
    host, port = address
    hosts = ('127.0.0.1', '[::1]') if host == 'localhost' else ('127.0.0.1',)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    for literal in hosts:
        try:
            try:
                response = opener.open(f'http://{literal}:{port}{target}', timeout=timeout)
            except urllib.error.HTTPError as error:
                if error.code != 400:
                    error.close()
                    return {}
                response = error  # NoRoute·NoSegment·TooBig는 JSON 400으로 올 수 있다.
            with response:
                raw = response.read(MAX_RESPONSE + 1)
            data = json.loads(raw) if len(raw) <= MAX_RESPONSE else None
            return data if isinstance(data, dict) else {}
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            continue
    return {}


def coordinate_path(points):
    """내부 (위도, 경도) → OSRM URL의 경도,위도. 한국 좌표는 소수 6자리로 보낸다."""
    return ';'.join(f'{lon:.6f},{lat:.6f}' for lat, lon in points)


def fetch_road(url: str, path: str, query: str, *, timeout: float) -> tuple[dict, object]:
    """업무 노선·시간 조회는 같은 제외 조건을 사용한다."""
    data = fetch(url, f'{path}?{query}&exclude=toll', timeout=timeout)
    code, message = data.get('code'), data.get('message')
    if code in ('InvalidValue', 'NotImplemented', 'InvalidQuery', 'InvalidOptions') and isinstance(message, str):
        if 'exclude' in message.casefold():
            code = TOLL_EXCLUSION_UNAVAILABLE
    return data, code


def table(url, points, sources, destinations, *, timeout):
    path = f'/table/v1/driving/{coordinate_path(points)}'
    radiuses = ';'.join(['200'] * len(points))
    query = f'annotations=duration&radiuses={radiuses}'
    if sources != list(range(len(points))):
        query += '&sources=' + ';'.join(map(str, sources))
    if destinations != list(range(len(points))):
        query += '&destinations=' + ';'.join(map(str, destinations))
    return fetch_road(url, path, query, timeout=timeout)


def missing_segment_index(data, size):
    message = data.get('message')
    if isinstance(message, str):
        match = re.fullmatch(r'Could not find a matching segment for coordinate ([0-9]{1,6})\.?', message)
        if match and (index := int(match[1])) < size:
            return index
    return None
