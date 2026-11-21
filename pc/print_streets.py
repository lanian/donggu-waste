"""배차표 인쇄 범위에 드는 공개 도로선을 동봉된 광주 자료에서 읽는다."""
from functools import lru_cache
import gzip
from itertools import pairwise
import json
from math import floor
from threading import Lock
import zlib

from dgpc import paths


_CLASS_ORDER = {"major": 0, "street": 1, "minor": 2}
_MAX_PATHS = 600
_MAX_POINTS = 8000
_MAX_ASSET_BYTES = 64 * 1024 * 1024
_LOAD_LOCK = Lock()


def _point(x, y):
    return type(x) in (int, float) and type(y) in (int, float) and -180 <= x <= 180 and -90 <= y <= 90


def _cells(bounds, maximum):
    """약 2km 격자. 광역 값은 모든 도로를 한 번 순회하는 편이 더 작다."""
    left, bottom, right, top = (floor(v * 50) for v in bounds)
    if (right - left + 1) * (top - bottom + 1) > maximum:
        return None
    return ((x, y) for x in range(left, right + 1) for y in range(bottom, top + 1))


@lru_cache(maxsize=1)
def _index(asset, modified):
    # 캐시 키는 공개 배포 파일뿐이다. 배차 좌표와 인쇄 결과는 저장하지 않는다.
    with gzip.open(asset, "rb") as stream:
        raw = stream.read(_MAX_ASSET_BYTES + 1)
    if len(raw) > _MAX_ASSET_BYTES:
        raise ValueError("도로 자료 크기 초과")
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("schema") != 1 or not isinstance(document.get("roads"), list):
        raise ValueError("도로 자료 형식 오류")
    rows, grid, wide = [], {}, []
    for kind, name, coords in document["roads"]:
        if (kind not in _CLASS_ORDER or not isinstance(name, str) or not isinstance(coords, list)
                or len(coords) < 2 or not all(_point(*p) for p in coords)):
            continue
        xs, ys = zip(*coords)
        bounds = min(xs), min(ys), max(xs), max(ys)
        index = len(rows)
        rows.append((kind, name, coords, bounds))
        cells = _cells(bounds, 128)
        if cells is None:
            wide.append(index)
        else:
            for cell in cells:
                grid.setdefault(cell, []).append(index)
    return rows, grid, wide


def _clip(a, b, bounds):
    """선분을 사각형의 네 변에 대해 자른다 (Liang–Barsky)."""
    west, south, east, north = bounds
    x, y = a
    dx, dy = b[0] - x, b[1] - y
    start, end = 0., 1.
    for p, q in ((-dx, x - west), (dx, east - x), (-dy, y - south), (dy, north - y)):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            start = max(start, q / p)
        else:
            end = min(end, q / p)
        if start >= end:
            return None
    first = (x, y) if start == 0 else (min(east, max(west, x + start * dx)), min(north, max(south, y + start * dy)))
    last = tuple(b) if end == 1 else (min(east, max(west, x + end * dx)), min(north, max(south, y + end * dy)))
    return (first, last) if first != last else None


def _fragments(coords, bounds):
    line = []
    for a, b in pairwise(coords):
        if a == b:
            continue
        segment = _clip(a, b, bounds)
        if line and (segment is None or line[-1] != segment[0]):
            yield line
            line = []
        if segment is not None:
            if not line:
                line.append(segment[0])
            line.append(segment[1])
    if line:
        yield line


def _print_points(line, x_step, y_step):
    """인쇄 한 픽셀 안의 세부 꼭짓점을 생략하되 각 잘린 선의 양끝을 보존한다."""
    selected = [line[0]]
    for point in line[1:-1]:
        x, y = selected[-1]
        if abs(point[0] - x) >= x_step or abs(point[1] - y) >= y_step:
            selected.append(point)
    selected.append(line[-1])
    return selected


def roads_in_bounds(bounds):
    """(서, 남, 동, 북) WGS84 범위를 잘라 (도로 등급, 도로명, 좌표열) 목록을 반환한다."""
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        return []
    west, south, east, north = bounds
    if not (_point(west, south) and _point(east, north) and west < east and south < north):
        return []
    try:
        asset = paths.resource_root() / "data" / "print_streets.json.gz"
        with _LOAD_LOCK:
            rows, grid, wide = _index(asset, asset.stat().st_mtime_ns)
    except (OSError, EOFError, ValueError, TypeError, RecursionError, zlib.error):
        return []

    cells = _cells(bounds, 1024)
    if cells is None:
        indexes = range(len(rows))
    else:
        indexes = set(wide)
        for cell in cells:
            indexes.update(grid.get(cell, ()))
    candidates = []
    for index in indexes:
        kind, _, _, (left, bottom, right, top) = rows[index]
        if right < west or left > east or top < south or bottom > north:
            continue
        span = max((right - left) / (east - west), (top - bottom) / (north - south))
        candidates.append((_CLASS_ORDER[kind], -span, index))
    result, remaining = [], _MAX_POINTS
    x_step, y_step = (east - west) / 960, (north - south) / 330
    for _, _, index in sorted(candidates):
        kind, name, coords, _ = rows[index]
        for fragment in _fragments(coords, bounds):
            points = _print_points(fragment, x_step, y_step)[:remaining]
            result.append((kind, name, points))
            remaining -= len(points)
            if remaining < 2 or len(result) >= _MAX_PATHS:
                return result
    return result
