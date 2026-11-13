"""공식 행정복지센터 위치 기준과 관리자용 차고지 보충. 기존 팀·차고지는 보존한다."""
from __future__ import annotations

from copy import deepcopy
import json
from dgpc import core as C
from dgpc.paths import resource_root

_DATA = json.loads((resource_root() / 'data/dong_centers.json').read_text(encoding='utf-8'))
_CENTERS = {row['dong']: row for row in _DATA['centers']}


def centers():
    return deepcopy(_DATA['centers'])


def center_for(name):
    return deepcopy(_CENTERS.get(''.join((name or '').split())))


def apply_missing(con):
    """명시적으로 실행했을 때만 비어 있는 차고지를 보충한다. 같은 요청 재시도는 무변경이다."""
    from dgpc.area import list_dongs
    applied, skipped = [], []
    with C.tx(con):
        dongs = list_dongs(con)
        active_crews = {row['id'] for row in C.rows(con, 'SELECT id FROM crew WHERE active=1')}
        users = {}
        for dong in dongs:
            if dong['crew_id'] and (dong['active'] or dong['depot']):
                users.setdefault(dong['crew_id'], []).append(dong['id'])
        for dong in dongs:
            center = center_for(dong['name'])
            if not center:
                continue
            reason = ('기존 차고지 유지' if dong['depot'] else '사용 중지 동' if not dong['active']
                      else '담당팀 연결 필요' if not dong['crew_id'] else '담당팀 사용 중지' if dong['crew_id'] not in active_crews
                      else '여러 동에 중복 연결된 담당팀 확인 필요' if len(users.get(dong['crew_id'], [])) != 1 else '')
            if reason:
                skipped.append({'id': dong['id'], 'name': dong['name'], 'reason': reason})
                continue
            depot = {key: center[key] for key in ('label', 'lat', 'lon')}
            con.execute('UPDATE dong SET depot_json=?,updated_at=? WHERE id=?', (json.dumps(depot, ensure_ascii=False), C.now(), dong['id']))
            C.audit(con, 'dong', dong['id'], 'SAVE', {'depot': depot, 'source': center['source'], 'crew_id': dong['crew_id']})
            applied.append(dong['id'])
    return {'applied': len(applied), 'applied_ids': applied, 'skipped': skipped}
