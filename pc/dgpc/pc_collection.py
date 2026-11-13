"""PC 직접 수거를 기존 결과 이벤트·동별 실물 번호·서식 집계에 연결한다."""
from __future__ import annotations

import hashlib
import json
import uuid

from dgpc import core as C, collection, label_auto
from dgpc.derive import derive

SOURCE = 'PC-COLLECTION'


def revision(con, rid):
    entry = C.one(con, """SELECT json_extract(body_json,'$.pc_req_ver') AS version FROM result_event
        WHERE request_id=? AND device_id=? AND kind='ATTEMPT' AND apply_status='APPLIED' ORDER BY rowid DESC LIMIT 1""", (rid, SOURCE))
    return entry['version'] if entry else 0


def preview(con, req):
    rid = req['id']
    events = C.rows(con, "SELECT event_id FROM result_event WHERE request_id=? AND apply_status='APPLIED' ORDER BY rowid", (rid,))
    active = C.one(con, 'SELECT batch_id FROM active_assignment WHERE request_id=?', (rid,))
    basis = hashlib.sha256(json.dumps([req['req_ver'], req['status'], active, events], sort_keys=True).encode()).hexdigest()
    state = req.get('state') or derive(con, req)
    trace = collection.read_traces(con, [{'id': rid}])[rid]
    previous = {line['line_id']: line['qty_collected'] for line in trace['lines']}
    lines = []
    for line in state['lines']:
        before = previous.get(line['line_id'], 0)
        confirmed = max(before, line['qty_confirmed'])
        lines.append({'line_id': line['line_id'], 'name': line['name'], 'spec': line['spec'], 'unit_label': line['unit_label'],
                      'confirmed': confirmed, 'previous': before, 'remaining': confirmed - before, 'kind': line['kind']})
    return {'basis': basis, 'lines': lines, 'previous_total': sum(line['previous'] for line in lines)}


def save(con, rid, data):
    if not isinstance(data, dict):
        raise C.UserError('수거 입력 내용을 확인해 주세요.')
    with C.tx(con):
        req = C.one(con, 'SELECT * FROM request WHERE id=?', (rid,))
        if not req or req['status'] == 'CANCELLED' or req.get('pii_purged_at'):
            raise C.UserError('취소·파기되었거나 없는 접수에는 수거 결과를 입력할 수 없습니다.')
        key = data.get('client_req_id')
        if not isinstance(key, str) or not key.strip() or len(key) > 80:
            raise C.UserError('수거 저장 요청 번호가 없습니다. 입력창을 다시 열어 주세요.')
        return C.idempotent(con, C.ACTOR.get(), 'pc-collection/' + rid, key, lambda: _save(con, req, data))


def _save(con, req, data):
    from dgpc.intake import _snapshot
    from dgpc.results import _refresh_status
    rid = req['id']
    C.check_req_ver(con, rid, data.get('req_ver'))
    current = preview(con, req)
    if not isinstance(data.get('basis'), str) or data['basis'] != current['basis']:
        raise C.StaleError('수거 결과나 배정이 바뀌었습니다. 접수를 다시 열어 확인하세요.')
    day = data.get('collected_on')
    if not isinstance(day, str) or not C._date_ok(day) or day < '2020-01-01' or day > C.today_kst():
        raise C.UserError('실제 수거일은 오늘까지의 올바른 날짜로 입력해 주세요.')
    items = data.get('items')
    if not isinstance(items, list) or len(items) != len(current['lines']) or not items:
        raise C.UserError('모든 품목의 이번 수거 개수를 확인해 주세요.')
    values = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('line_id'), str) or item['line_id'] in values:
            raise C.UserError('수거 품목이 중복되었거나 올바르지 않습니다.')
        qty = item.get('qty')
        if isinstance(qty, bool) or not isinstance(qty, int) or not 0 <= qty <= 9999:
            raise C.UserError('이번 수거 개수는 0 이상의 정수로 입력해 주세요.')
        values[item['line_id']] = qty
    if set(values) != {line['line_id'] for line in current['lines']}:
        raise C.UserError('현재 접수의 수거 품목을 모두 확인해 주세요.')
    for line in current['lines']:
        if values[line['line_id']] > line['remaining']:
            raise C.UserError('이번 수거 개수가 남은 수량을 넘습니다.')
    total = current['previous_total'] + sum(values.values())
    remaining = sum(line['remaining'] for line in current['lines']) - sum(values.values())
    outcome = 'DONE' if total and remaining == 0 else 'PARTIAL' if total else 'NOT_COLLECTED'
    reason = data.get('reason') or ''
    if not isinstance(reason, str) or len(reason) > 200 or (remaining and not reason.strip()):
        raise C.UserError('일부 수거·미수거 사유를 200자 이내로 입력해 주세요.')
    if req['status'] == 'DONE' and sum(values.values()) == 0:
        raise C.UserError('이미 모두 수거한 접수입니다.')
    at = C.now() if day == C.today_kst() else day + 'T03:00:00Z'
    imported = C.now()
    imp = 'pc-' + str(uuid.uuid4())
    version = req['req_ver'] + 1
    sequence = first_sequence = C.one(con, 'SELECT COALESCE(MAX(device_seq),0) AS n FROM result_event')['n']
    con.execute('INSERT INTO request_version VALUES (?,?,?,?,?)',
                (rid, req['req_ver'], json.dumps(_snapshot(con, rid), ensure_ascii=False), 'PC 수거 입력', imported))
    con.execute('UPDATE request SET req_ver=?,updated_at=? WHERE id=?', (version, imported, rid))
    con.execute("INSERT INTO result_import(id,channel,device_id,package_sha256,imported_at) VALUES (?,'USB',?,?,?)",
                (imp, SOURCE, hashlib.sha256(imp.encode()).hexdigest(), imported))

    def event(kind, body):
        nonlocal sequence
        sequence += 1
        con.execute('''INSERT INTO result_event(event_id,import_id,device_id,device_seq,request_id,assign_ver,base_req_ver,kind,body_json,apply_status,device_ts)
            VALUES (?,?,?,?,?,0,?,?,?,'APPLIED',?)''',
                    (str(uuid.uuid4()), imp, SOURCE, sequence, rid, version, kind, json.dumps(body, ensure_ascii=False), at))
    for line in current['lines']:
        event('ITEM_RESULT', {'line_id': line['line_id'], 'qty_confirmed': line['confirmed'],
                             'qty_collected': line['previous'] + values[line['line_id']], 'reason': 'OTHER' if remaining else '', 'source': 'PC'})
    if reason.strip():
        event('FIELD_MEMO', {'text': 'PC 수거 입력: ' + reason.strip(), 'source': 'PC'})
    event('ATTEMPT', {'outcome': outcome, 'source': 'PC', 'pc_req_ver': version, 'worker': C.ACTOR.get()})
    _refresh_status(con, rid)
    con.execute('UPDATE request SET completed_at=?,updated_at=? WHERE id=?', (at, imported, rid))
    counts = label_auto.sync(con, [rid])
    response = {'id': rid, 'req_ver': version, 'status': outcome, 'collected_on': day, 'added': sum(values.values()), 'total': total,
                'labels_assigned': counts['new_assigned'], 'labels_pending': counts['pending']}
    con.execute('UPDATE result_import SET summary_json=? WHERE id=?', (json.dumps({'source': 'PC', 'applied': sequence - first_sequence, **response}), imp))
    C.audit(con, 'request', rid, 'PC_COLLECTION', {**response, 'reason': reason.strip(), **C._after_close(con, rid)})
    return response
