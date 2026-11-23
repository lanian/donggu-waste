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


def basis(con, req):
    rid = req['id']
    events = C.rows(con, "SELECT event_id FROM result_event WHERE request_id=? AND apply_status='APPLIED' ORDER BY rowid", (rid,))
    active = C.one(con, 'SELECT batch_id FROM active_assignment WHERE request_id=?', (rid,))
    return hashlib.sha256(json.dumps([req['req_ver'], req['status'], active, events], sort_keys=True).encode()).hexdigest()


def preview(con, req):
    rid = req['id']
    state = req.get('state') or derive(con, req)
    trace = collection.read_traces(con, [{'id': rid}])[rid]
    previous = {line['line_id']: line['qty_collected'] for line in trace['lines']}
    lines = []
    for line in state['lines']:
        before = previous.get(line['line_id'], 0)
        confirmed = max(before, line['qty_confirmed'])
        lines.append({'line_id': line['line_id'], 'name': line['name'], 'spec': line['spec'], 'unit_label': line['unit_label'],
                      'received': line['qty_received'], 'unit_price': line['price'], 'confirmed': confirmed,
                      'previous': before, 'remaining': confirmed - before, 'kind': line['kind']})
    return {'basis': basis(con, req), 'lines': lines, 'previous_total': sum(line['previous'] for line in lines)}


def save(con, rid, data, *, guest_entry=None):
    if not isinstance(data, dict):
        raise C.UserError('수거 입력 내용을 확인해 주세요.')
    with C.tx(con):
        req = C.one(con, 'SELECT * FROM request WHERE id=?', (rid,))
        if not req or req['status'] == 'CANCELLED' or req.get('pii_purged_at'):
            raise C.UserError('취소·파기되었거나 없는 접수에는 수거 결과를 입력할 수 없습니다.')
        key = data.get('client_req_id')
        if not isinstance(key, str) or not key.strip() or len(key) > 80:
            raise C.UserError('수거 저장 요청 번호가 없습니다. 입력창을 다시 열어 주세요.')
        # 현장 제출의 업무 기록 번호와 담당자 화면의 직접 입력 번호를 구분한다.
        scope = 'guest-collection/' if guest_entry else 'pc-collection/'
        return C.idempotent(con, C.ACTOR.get(), scope + rid, key, lambda: _save(con, req, data, guest_entry))


def _quantity(value, label, minimum=0):
    if type(value) is not int or not minimum <= value <= 9999:
        raise C.UserError(f'{label} 입력란에 {minimum}~9999 사이의 정수를 적어 주세요.')
    return value


def _field_additions(con, items):
    from dgpc.intake import build_items
    if not isinstance(items, list):
        raise C.UserError('현장 추가 품목 목록을 확인해 주세요.')
    if not items:
        return []
    inputs, quantities = [], []
    units = {'EA': '개', 'PIECE': '쪽', 'SET': '세트', 'L20': '20ℓ당'}
    for item in items:
        if not isinstance(item, dict):
            raise C.UserError('현장 추가 품목을 확인해 주세요.')
        confirmed = _quantity(item.get('confirmed_qty'), '추가 품목의 현장 확인 수량', 1)
        qty = _quantity(item.get('qty'), '이번 수거 개수')
        if qty > confirmed:
            raise C.UserError('이번 수거 개수가 현장 확인 수량을 넘습니다.')
        rate = item.get('fee_rate_id')
        row = {'fee_rate_id': rate, 'qty': confirmed}
        if rate is not None and (not isinstance(rate, str) or not rate.strip()):
            raise C.UserError('수수료표 품목을 다시 골라 주세요.')
        if rate is None:
            name, spec, unit = item.get('name'), item.get('spec', ''), item.get('unit', 'EA')
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or not isinstance(spec, str) or len(spec) > 60:
                raise C.UserError('목록에 없는 품목의 이름과 규격을 각각 60자 이내로 입력해 주세요.')
            if not isinstance(unit, str) or unit not in units:
                raise C.UserError('현장 추가 품목의 부과 단위를 확인해 주세요.')
            row.update(name=name.strip(), spec=spec.strip(), unit=unit, unit_label=units[unit])
        inputs.append(row)
        quantities.append(qty)
    return list(zip(build_items(inputs, con), quantities))


def _item_results(lines, items):
    if not isinstance(items, list) or len(items) != len(lines) or not items:
        raise C.UserError('모든 품목의 이번 수거 개수를 확인해 주세요.')
    values = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('line_id'), str) or item['line_id'] in values:
            raise C.UserError('수거 품목이 중복되었거나 올바르지 않습니다.')
        values[item['line_id']] = item
    if set(values) != {line['line_id'] for line in lines}:
        raise C.UserError('현재 접수의 수거 품목을 모두 확인해 주세요.')
    results = []
    for line in lines:
        item = values[line['line_id']]
        confirmed = _quantity(item.get('confirmed_qty', line['confirmed']), '현장 확인 수량')
        qty = _quantity(item.get('qty'), '이번 수거 개수')
        if confirmed < line['previous']:
            raise C.UserError('현장 확인 수량은 이미 수거한 수량보다 줄일 수 없습니다.')
        collected = line['previous'] + qty
        if collected > confirmed:
            raise C.UserError('이번 수거 개수가 남은 수량을 넘습니다.')
        results.append({'line_id': line['line_id'], 'qty_confirmed': confirmed, 'qty_collected': collected})
    return results


def _save(con, req, data, guest_entry):
    from dgpc.intake import _snapshot
    from dgpc.results import _refresh_status
    rid = req['id']
    C.check_req_ver(con, rid, data.get('req_ver'))
    current = preview(con, req)
    if not isinstance(data.get('basis'), str) or data['basis'] != current['basis']:
        raise C.StaleError('수거 결과나 배정이 바뀌었습니다. 접수를 다시 열어 확인하세요.')
    if req['status'] == 'DONE':
        raise C.UserError('이미 모두 수거한 접수입니다.')
    day = data.get('collected_on')
    if not isinstance(day, str) or not C._date_ok(day) or day < '2020-01-01' or day > C.today_kst():
        raise C.UserError('실제 수거일은 오늘까지의 올바른 날짜로 입력해 주세요.')
    batch = C.one(con, """SELECT b.id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id
        WHERE a.request_id=? AND b.work_date=?""", (rid, day))
    results = _item_results(current['lines'], data.get('items'))
    additions = _field_additions(con, data.get('field_items', []))
    changed = any(item['qty_confirmed'] != line['confirmed'] for item, line in zip(results, current['lines']))
    results.extend({'line_id': item['line_id'], 'qty_confirmed': item['qty'], 'qty_collected': qty} for item, qty in additions)
    total = sum(item['qty_collected'] for item in results)
    remaining = sum(item['qty_confirmed'] for item in results) - total
    outcome = 'DONE' if total and remaining == 0 else 'PARTIAL' if total else 'NOT_COLLECTED'
    reason = data.get('reason', '')
    if not isinstance(reason, str) or len(reason) > 200 or ((changed or additions or outcome != 'DONE') and not reason.strip()):
        raise C.UserError('현장 변동·일부 수거·미수거 사유를 200자 이내로 입력해 주세요.')
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
    for item, _ in additions:
        event('FIELD_ITEM_ADD', {**item, 'source': 'PC'})
    for item in results:
        event('ITEM_RESULT', {**item, 'reason': 'OTHER' if item['qty_collected'] < item['qty_confirmed'] else '', 'source': 'PC'})
    if reason.strip():
        event('FIELD_MEMO', {'text': 'PC 수거 입력: ' + reason.strip(), 'source': 'PC'})
    event('ATTEMPT', {'outcome': outcome, 'source': 'PC', 'pc_req_ver': version, 'worker': C.ACTOR.get()})
    _refresh_status(con, rid)
    con.execute('UPDATE request SET completed_at=?,updated_at=? WHERE id=?', (at, imported, rid))
    counts = label_auto.sync(con, [rid])
    response = {'id': rid, 'req_ver': version, 'status': outcome, 'collected_on': day, 'added': total - current['previous_total'], 'total': total,
                'labels_assigned': counts['new_assigned'], 'labels_pending': counts['pending']}
    origin = {'guest_entry': guest_entry} if guest_entry else {}
    con.execute('UPDATE result_import SET summary_json=? WHERE id=?', (json.dumps({'source': 'PC', 'applied': sequence - first_sequence, **origin, **response}), imp))
    C.audit(con, 'request', rid, 'PC_COLLECTION', {**response, 'batch_id': batch['id'] if batch else None,
                                                  **origin, 'reason': reason.strip(), **C._after_close(con, rid)})
    return response
