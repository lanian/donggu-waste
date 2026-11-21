"""배차표 수거지 사이의 방문 순서와 경로를 인쇄한다."""

from html import escape
from itertools import pairwise
from math import atan2, cos, degrees, hypot, radians


_WIDTH, _HEIGHT, _MARGIN = 960, 330, 34
_STREET_STYLES = {"minor": ("#c5c9cc", 1.1), "street": ("#adb4b9", 1.5), "major": ("#889299", 2.4)}


def _point(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        lon, lat = value
        if (type(lon) in (float, int) and type(lat) in (float, int)
                and -180 <= lon <= 180 and -90 <= lat <= 90):
            return lon, lat
    return None


def pickup_runs(stops, *, positions):
    """중간 하차와 위치가 없는 수거지에서 지도 경로를 끊고 배차표 번호를 보존한다."""
    runs, run = [], []
    for stop in stops:
        point = _point((stop.get("lon"), stop.get("lat"))) if positions else None
        if point is None and run:
            runs.append(run)
            run = []
        run.append((str(stop["seq"]), point))
        if point is None or stop.get("unload_after"):
            runs.append(run)
            run = []
    if run:
        runs.append(run)
    return runs


def _visit_order(visits):
    items = "".join(f"<li>{escape(str(label))}</li>" for label, _ in visits)
    return f'<ol class="print-route-order" aria-label="방문 순서">{items}</ol>'


def _projector(points):
    west, east = min(p[0] for p in points), max(p[0] for p in points)
    south, north = min(p[1] for p in points), max(p[1] for p in points)
    center_lon, center_lat = (west + east) / 2, (south + north) / 2
    lon_factor = max(0.01, cos(radians(center_lat)))
    # 한 집을 출력해도 동네 골목을 알아볼 수 있도록 사방에 여백을 둔다.
    scale = min((_WIDTH - 2 * _MARGIN) / max(0.002, (east - west) * lon_factor),
                (_HEIGHT - 2 * _MARGIN) / max(0.002, north - south))

    def project(point):
        return (round(_WIDTH / 2 + (point[0] - center_lon) * lon_factor * scale, 1),
                round(_HEIGHT / 2 - (point[1] - center_lat) * scale, 1))

    dx, dy = _WIDTH / (2 * scale * lon_factor), _HEIGHT / (2 * scale)
    return project, (center_lon - dx, center_lat - dy, center_lon + dx, center_lat + dy)


def _line(points):
    return "M" + " L".join(f"{x:g} {y:g}" for x, y in points)


def _street_background(streets, project, pickups):
    """도로는 옅게 깔고, 수거 번호와 겹치지 않는 곳에 도로명을 한 번씩 표시한다."""
    strokes = {kind: [] for kind in _STREET_STYLES}
    candidates = []
    for kind, name, coordinates in streets:
        if kind not in strokes or len(coordinates) < 2:
            continue
        line = [project(point) for point in coordinates]
        strokes[kind].append(_line(line))
        if name:
            a, b = max(pairwise(line), key=lambda ab: hypot(ab[1][0] - ab[0][0], ab[1][1] - ab[0][1]))
            span = hypot(b[0] - a[0], b[1] - a[1])
            candidates.append((kind == "major", span, name, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
    if not any(strokes.values()):
        return ""
    parts = ['<g class="print-map-base" aria-label="주변 도로와 도로명">']
    for kind, lines in strokes.items():
        if lines:
            color, width = _STREET_STYLES[kind]
            parts.append(f'<path class="print-map-street" d="{" ".join(lines)}" fill="none" '
                         f'stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>')
    occupied = [(x - 24, y - 20, x + 24, y + 20) for x, y in map(project, pickups)]
    occupied.append((_WIDTH - 48, 0, _WIDTH, 53))  # 방위표
    placed = set()
    for _, span, name, x, y in sorted(candidates, reverse=True):
        width = 6.5 * len(name) + 4
        left, right = x - width, x + width
        if (name in placed or span < 10 or left < 4 or right > _WIDTH - 4 or not 12 <= y <= _HEIGHT - 10
                or any(left < r and right > l and y - 10 < b and y + 10 > t for l, t, r, b in occupied)):
            continue
        parts.append(f'<text class="print-map-street-name" x="{x:.1f}" y="{y:.1f}" text-anchor="middle" '
                     f'fill="#485158" font-size="12" stroke="#fff" stroke-width="3" '
                     f'paint-order="stroke">{escape(name)}</text>')
        placed.add(name)
        occupied.append((left - 8, y - 14, right + 8, y + 14))
        if len(placed) == 18:
            break
    return "".join(parts) + "</g>"


def _arrows(points):
    segments = [(start, end, hypot(end[0] - start[0], end[1] - start[1]))
                for start, end in pairwise(points) if start != end]
    length = sum(seg[2] for seg in segments)
    if length < 24:
        return ""
    step = max(78, length / 15)
    target, offset = min(step / 2, length / 2), 0
    marks = []
    for (ax, ay), (bx, by), span in segments:
        while target < offset + span and target < length - 10:
            f = (target - offset) / span
            x, y = ax + (bx - ax) * f, ay + (by - ay) * f
            angle = degrees(atan2(by - ay, bx - ax))
            marks.append(f'<path class="print-route-arrow" d="M-7 -5 L2 0 -7 5 -4 0 Z" '
                         f'transform="translate({x:.1f} {y:.1f}) rotate({angle:.1f})" '
                         'fill="#fff" stroke="#222" stroke-width="1.6" stroke-linejoin="round"/>')
            target += step
        offset += span
    return "".join(marks)


def _stops(visits, project):
    groups = {}
    for label, point in visits:
        if point is not None:
            x, y = project(point)
            _, labels = groups.setdefault((round(x / 9), round(y / 9)), ((x, y), []))
            labels.append(escape(str(label)))
    marks = []
    for (x, y), labels in groups.values():
        marks.append(f'<circle cx="{x:g}" cy="{y:g}" r="4" fill="#fff" stroke="#222" stroke-width="2"/>')
        right = x < _WIDTH * 0.88
        tx, anchor = (x + 7, "start") if right else (x - 7, "end")
        row_direction = 1 if y < _HEIGHT / 2 else -1
        # 한 장소의 여러 수거 번호를 위아래로 나눠 표시한다.
        for i, label in enumerate(labels):
            ty = y + row_direction * (5 + 15 * i)
            if not 15 <= ty <= _HEIGHT - 4:
                marks.append(f'<text x="{tx:g}" y="{ty - 15 * row_direction:g}" text-anchor="{anchor}" '
                             'fill="#222" font-size="12" dx="19" stroke="#fff" stroke-width="3" '
                             f'paint-order="stroke">외 {len(labels) - i}</text>')
                break
            marks.append(f'<text class="print-route-stop" x="{tx:g}" y="{ty:g}" text-anchor="{anchor}" '
                         f'fill="#222" font-size="16" font-weight="700" stroke="#fff" stroke-width="4" '
                         f'paint-order="stroke">{label}</text>')
    return "".join(marks)


def route_figure(runs, *, roads=(), geographic=False, caption="", street_lookup=None):
    """수거지로 축척을 잡고 연속해서 방문하는 수거지 사이에만 선을 그린다."""
    visits = [visit for run in runs for visit in run]
    if not visits:
        return ""
    title = "수거지 노선도" if geographic else "수거 방문 순서"
    frame = f'<section class="print-route" aria-labelledby="print-route-title"><h2 id="print-route-title">{title}</h2>'
    if caption:
        frame += f'<p class="print-route-caption">{escape(caption)}</p>'
    if not geographic:
        return frame + _visit_order(visits) + "</section>"

    points = [point for _, point in visits if point is not None]
    missing = len(visits) - len(points)
    missing_note = f'<p class="print-route-note">위치 없는 수거지 {missing}곳</p>' if missing else ""
    if not points:
        return frame + missing_note + _visit_order(visits) + "</section>"

    project, bounds = _projector(points)
    background = _street_background(street_lookup(bounds), project, points) if street_lookup else ""
    shapes, straight, drawn_roads = [], [], []
    for i, run in enumerate(runs):
        if len(run) < 2:
            continue
        result = roads[i] if i < len(roads) else {}
        geometry = result.get("geometry") or {}
        path = geometry.get("coordinates", []) if geometry.get("type") == "LineString" else []
        if len(path) >= 2 and all(_point(p) is not None for p in path) and all(p is not None for _, p in run):
            projected = [project(p) for p in path]
            shapes.append(f'<path class="print-route-road" d="{_line(projected)}" stroke="#333" stroke-width="3"/>')
            shapes.append(_arrows(projected))
            drawn_roads.append(result)
        else:
            straight.extend(_line([project(a), project(b)]) for (_, a), (_, b) in pairwise(run)
                            if a is not None and b is not None and a != b)
    legend = ["번호는 배차표의 수거 순서입니다."]
    credit = ""
    if drawn_roads:
        distances = [r.get("distance_km") for r in drawn_roads]
        distance = (f" {sum(distances):.2f} km" if all(type(d) in (int, float) and 0 <= d < float("inf")
                                                       for d in distances) else "")
        toll = " · 유료도로 제외" if all(r.get("toll_excluded") is True for r in drawn_roads) else ""
        legend.append(f"현재 도로망 기준 수거지 사이 도로 구간{distance}{toll} · 화살표는 진행 방향")
    if background or drawn_roads:
        credit = '<span class="print-route-credit">© OpenStreetMap contributors · openstreetmap.org/copyright</span>'
    if street_lookup and not background:
        legend.append("이 범위의 도로 배경을 표시할 수 없습니다.")
    if straight:
        shapes.append(f'<path class="print-route-straight" d="{" ".join(straight)}" stroke="#444" '
                      'stroke-width="2.5" stroke-dasharray="7 5"/>')
        legend.append("점선은 직선 연결입니다. 실제 운행 경로를 확인하세요.")
    svg = (f'<svg class="print-route-map" role="img" aria-label="{title}" viewBox="0 0 {_WIDTH} {_HEIGHT}">'
           f'{background}<g fill="none" stroke-linejoin="round" stroke-linecap="round">{"".join(shapes)}</g>{_stops(visits, project)}'
           '<path d="M938 31 V15 L934 21 M938 15 L942 21" fill="none" stroke="#222" stroke-width="1.6"/>'
           '<text x="938" y="45" text-anchor="middle" font-size="12" fill="#222">N</text></svg>')
    return frame + f'<p class="print-route-note">{" ".join(legend)}</p>' + svg + missing_note + credit + "</section>"
