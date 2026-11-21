"""배차표에서 방문 순서와 도로 경로를 바탕 지도 없이 인쇄한다."""

from html import escape
from itertools import pairwise
from math import atan2, cos, degrees, hypot, radians


_WIDTH, _HEIGHT, _MARGIN = 960, 330, 34


def _point(value):
    if isinstance(value, (list, tuple)) and len(value) == 2:
        lon, lat = value
        if (type(lon) in (float, int) and type(lat) in (float, int)
                and -180 <= lon <= 180 and -90 <= lat <= 90):
            return lon, lat
    return None


def _visit_order(visits):
    if not visits:
        return '<p>표시할 방문 지점이 없습니다.</p>'
    items = "".join(f"<li>{escape(str(label))}</li>" for label, _ in visits)
    return f'<ol class="print-route-order" aria-label="방문 순서">{items}</ol>'


def _projector(points):
    west, east = min(p[0] for p in points), max(p[0] for p in points)
    south, north = min(p[1] for p in points), max(p[1] for p in points)
    center_lon, center_lat = (west + east) / 2, (south + north) / 2
    lon_factor = max(0.01, cos(radians(center_lat)))
    scale = min((_WIDTH - 2 * _MARGIN) / max(1e-6, (east - west) * lon_factor),
                (_HEIGHT - 2 * _MARGIN) / max(1e-6, north - south))

    def project(point):
        return (round(_WIDTH / 2 + (point[0] - center_lon) * lon_factor * scale, 1),
                round(_HEIGHT / 2 - (point[1] - center_lat) * scale, 1))

    return project


def _line(points):
    return "M" + " L".join(f"{x:g} {y:g}" for x, y in points)


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
        # 겹친 점의 번호를 위아래로 나눠 출발·복귀나 한 주소 방문 순서를 읽게 한다.
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


def route_figure(visits, *, geometry=None, distance_km=None, toll_excluded=False, geographic=False, caption="",
                 note="", historical=False):
    """외부 요청 없이, 확정된 순서나 인쇄 폭에 맞춘 경로 형상을 반환한다."""
    title = "배차 노선도" if geographic else "방문 순서도"
    frame = f'<section class="print-route" aria-labelledby="print-route-title"><h2 id="print-route-title">{title}</h2>'
    if caption:
        frame += f'<p class="print-route-caption">{escape(caption)}</p>'
    if note:
        frame += f'<p class="print-route-note">{escape(note)}</p>'
    if not geographic:
        return frame + _visit_order(visits) + "</section>"

    positioned = [(label, _point(coords)) for label, coords in visits]
    points = [point for _, point in positioned if point is not None]
    missing = len(visits) - len(points)
    missing_note = f'<p class="print-route-note">위치 없는 지점 {missing}곳</p>' if missing else ""
    if not points:
        return frame + missing_note + _visit_order(visits) + "</section>"

    path = geometry.get("coordinates") if isinstance(geometry, dict) and geometry.get("type") == "LineString" else None
    road = [_point(value) for value in path] if isinstance(path, (list, tuple)) and len(path) >= 2 else []
    if missing or None in road:
        road = []
    project = _projector(points + road)
    if road:
        projected = [project(p) for p in road]
        dash = ' stroke-dasharray="8 5"' if historical else ""
        shape = f'<path class="print-route-road" d="{_line(projected)}" stroke="#333" stroke-width="3"{dash}/>'
        if not historical:
            shape += _arrows(projected)
        distance = f" · {distance_km:.2f} km" if type(distance_km) in (float, int) and 0 <= distance_km < float("inf") else ""
        toll = " · 유료도로 제외" if toll_excluded else ""
        legend = f"도로 경로{distance}{toll} · 번호는 배차표 순서"
        if not historical:
            legend += " · 화살표는 진행 방향"
        credit = '<span class="print-route-credit">© OpenStreetMap contributors · openstreetmap.org/copyright</span>'
    else:
        segments = [_line([project(a), project(b)]) for (_, a), (_, b) in pairwise(positioned)
                    if a is not None and b is not None and a != b]
        shape = (f'<path class="print-route-straight" d="{" ".join(segments)}" stroke="#444" '
                 'stroke-width="2.5" stroke-dasharray="7 5"/>') if segments else ""
        legend = "점선은 직선 연결입니다. 실제 운행 경로를 확인하세요." if segments else "확인된 위치만 표시합니다."
        credit = ""
    svg = (f'<svg class="print-route-map" role="img" aria-label="{title}" viewBox="0 0 {_WIDTH} {_HEIGHT}">'
           f'<g fill="none" stroke-linejoin="round" stroke-linecap="round">{shape}</g>{_stops(positioned, project)}'
           '<path d="M938 31 V15 L934 21 M938 15 L942 21" fill="none" stroke="#222" stroke-width="1.6"/>'
           '<text x="938" y="45" text-anchor="middle" font-size="12" fill="#222">N</text></svg>')
    return frame + f'<p class="print-route-note">{legend}</p>' + svg + missing_note + credit + "</section>"
