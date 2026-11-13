"""행정동의 고정 담당팀과 팀별 실제 출발지를 노선 계산에 연결한다."""
from __future__ import annotations

import hashlib
import json
import math
from dgpc import core as C


def point(value, label='동 차고지'):
    if value is None:
        return None
    if not isinstance(value, dict) or any(isinstance(value.get(k), bool) for k in ('lat', 'lon')):
        raise C.UserError('차고지 위도·경도와 이름을 확인해 주세요.')
    try:
        lat, lon = float(value['lat']), float(value['lon'])
    except (KeyError, TypeError, ValueError):
        raise C.UserError('차고지 위도와 경도를 모두 숫자로 적어 주세요.')
    if not math.isfinite(lat) or not math.isfinite(lon) or not (33 <= lat <= 39 and 124 <= lon <= 132):
        raise C.UserError('차고지 좌표가 국내 범위를 벗어났습니다.')
    name = value.get('label') or label
    if not isinstance(name, str) or len(name.strip()) > 80:
        raise C.UserError('차고지 이름을 80자 이내로 적어 주세요.')
    return {'lat': lat, 'lon': lon, 'label': name.strip() or label}


def same(a, b):
    return a['lat'] == b['lat'] and a['lon'] == b['lon']


def resolve(requests, crews, dongs, default, end_site=None):
    by_name = {d['name']: d for d in dongs}
    names = {r.get('dong') for r in requests}
    required = {}
    for req in requests:
        dong = by_name.get(req.get('dong'))
        if dong and dong.get('depot'):
            if not dong.get('crew_id'):
                raise C.UserError(f"{dong['name']} 차고지의 담당 수거팀을 먼저 지정해 주세요.")
            required[req['id']] = dong['crew_id']
    depots = {}
    for crew in crews:
        sources = [d for d in dongs if d.get('depot') and d.get('crew_id') == crew['id'] and (d.get('active', True) or d['name'] in names)]
        if sources:
            chosen = point(sources[0]['depot'], sources[0]['name'] + ' 차고지')
            if any(not same(chosen, point(d['depot'], d['name'] + ' 차고지')) for d in sources[1:]):
                raise C.UserError(f"{crew['name']}에 서로 다른 동 차고지가 연결되어 있습니다. 행정동의 담당팀·차고지를 확인해 주세요.")
            depots[crew['id']] = chosen
        else:
            depots[crew['id']] = dict(default)
    context = {'crew_depots': depots, 'crew_required': required, 'end_site': end_site}
    context['signature'] = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return context


def current(con, request_ids, crew_ids):
    from dgpc.area import list_dongs
    reqs = []
    ids = list(dict.fromkeys(request_ids))
    for chunk in C._chunks(ids):
        reqs.extend(C.rows(con, f"SELECT id,dong FROM request WHERE id IN ({','.join('?' for _ in chunk)})", chunk))
    if len(reqs) != len(ids):
        raise C.StaleError('계산한 접수가 바뀌었습니다. 노선을 다시 계산해 주세요.')
    wanted = set(crew_ids)
    crews = [c for c in C.rows(con, 'SELECT id,name FROM crew WHERE active=1') if c['id'] in wanted]
    return resolve(reqs, crews, list_dongs(con), C.get_setting(con, 'depot'), C.get_setting(con, 'end_site'))
