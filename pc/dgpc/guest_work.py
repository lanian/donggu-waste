"""배차에 한정한 현장 업무 발급, 암호화된 수거팀 결과의 미리보기와 확인 반영."""

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import time
import uuid
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from dgpc import (
    area,
    assign,
    core,
    devices,
    guest_list,
    pc_collection,
    privacy,
    results,
)

TICKET_TYPE = b"dg.guest-work-ticket/1"
RETURN_SCHEMA = "dg.guest-return/1"
MAX_WIRE = 2 * 1024 * 1024
REVIEW_SECONDS = 10 * 60


def _json(data):
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("ascii")


def _encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value, limit=MAX_WIRE):
    if not isinstance(value, str) or not 0 < len(value) <= limit or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("base64url")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode(raw) != value:
        raise ValueError("base64url")
    return raw


def _pc_secret(con, purpose):
    # 도메인별로 분리한다. 휴대폰에는 이번 발급의 임의 회신 열쇠만 건넨다.
    secret = devices.pc_key(con, "kex").private_numbers().private_value.to_bytes(32, "big")
    return hmac.digest(secret, purpose, "sha256")


def _work_fields(con, daylist, batch, issue_no):
    if core.close_info(con, batch["work_date"]):
        raise core.UserError("이미 마감한 날짜의 현장 업무는 발급할 수 없습니다.")
    stops = {}
    for stop in daylist["stops"]:
        lines = [i["line_id"] for i in stop["items"] if i["qty"] > i["prior_collected"]]
        if lines:
            req = core.one(con, "SELECT * FROM request WHERE id=?", (stop["request_id"],))
            stops[str(stop["seq"])] = {
                "rid": req["id"],
                "version": req["req_ver"],
                "basis": pc_collection.basis(con, req),
                "lines": lines,
            }
    key = _encode(os.urandom(32))
    ticket = {
        "batch": batch["id"],
        "crew": batch["crew_id"],
        "date": batch["work_date"],
        "issue": issue_no,
        "stops": stops,
        "key": key,
    }
    iv = os.urandom(12)
    sealed = AESGCM(_pc_secret(con, TICKET_TYPE)).encrypt(iv, zlib.compress(_json(ticket)), TICKET_TYPE)
    return {"ticket": _encode(iv + sealed), "return_key": key}


def issue(con, body):
    if not isinstance(body, dict):
        raise core.UserError("배차 목록을 선택해 주세요.")
    return guest_list.issue(con, body.get("batch_id"), work_fields=_work_fields)


def _open_file(con, file):
    try:
        if (
            not isinstance(file, dict)
            or set(file) != {"schema", "ticket", "iv", "ciphertext"}
            or file["schema"] != RETURN_SCHEMA
        ):
            raise ValueError("schema")
        sealed = _decode(file["ticket"])
        data = AESGCM(_pc_secret(con, TICKET_TYPE)).decrypt(sealed[:12], sealed[12:], TICKET_TYPE)
        ticket = json.loads(zlib.decompress(data))
        iv = _decode(file["iv"], 16)
        if len(iv) != 12:
            raise ValueError("nonce")
        payload = AESGCM(_decode(ticket["key"], 43)).decrypt(
            iv, _decode(file["ciphertext"]), file["ticket"].encode("ascii")
        )
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or set(decoded) != {"entries"}:
            raise ValueError("schema")
        entries = decoded["entries"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
            raise ValueError("entries")
        return ticket, entries
    except (InvalidTag, UnicodeError, ValueError, binascii.Error, zlib.error, RecursionError) as exc:
        raise core.UserError("이 PC에서 발급한 현장 업무의 결과 파일인지 확인해 주세요.") from exc


def _number(value, label, lower=0):
    if type(value) is not int or not lower <= value <= 9999:
        raise core.UserError(f"{label}은 {lower}~9999 사이 정수로 입력해 주세요.")
    return value


def _text(value, label, maximum, *, optional=False):
    if not isinstance(value, str) or len(value) > maximum or (not optional and not value.strip()):
        raise core.UserError(f"{label}을 {maximum}자 이내로 입력해 주세요.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise core.UserError(f"{label}의 글자를 확인해 주세요.") from exc
    return value.strip()


def _items(items, *, additions=False):
    if not isinstance(items, list) or len(items) > 100 or (not additions and not items):
        raise core.UserError("현장 품목을 확인해 주세요. 한 기록에 100종까지 입력할 수 있습니다.")
    rows = []
    for item in items:
        if not isinstance(item, dict) or set(item) != ({"name", "qty", "collected"} if additions else {"name", "qty"}):
            raise core.UserError("현장 품목 형식을 확인해 주세요.")
        name = _text(item["name"], "품목명", 60)
        qty = _number(item["qty"], "현장 수량", 1)
        row = {"name": name, "qty": qty}
        if additions:
            collected = _number(item["collected"], "수거 수량")
            if collected > qty:
                raise core.UserError("수거 수량이 현장 확인 수량을 넘습니다.")
            row.update(confirmed_qty=qty, qty=collected)
        rows.append(row)
    return rows


def _prepare_collection(con, ticket, entry):
    ref = ticket["stops"].get(str(entry["seq"]))
    if type(entry["seq"]) is not int or not ref:
        raise core.UserError("이번 배차에 없는 방문 순번입니다.")
    req = core.one(con, "SELECT * FROM request WHERE id=?", (ref["rid"],))
    active = core.one(con, "SELECT batch_id FROM active_assignment WHERE request_id=?", (ref["rid"],))
    if (
        not req
        or not active
        or active["batch_id"] != ticket["batch"]
        or req["pii_purged_at"]
        or req["status"] in ("DONE", "CANCELLED")
    ):
        raise core.StaleError("방문할 접수 상태가 바뀌었습니다. PC에서 최신 배차를 확인해 주세요.")
    current = pc_collection.preview(con, req)
    if current["basis"] != ref["basis"] or req["req_ver"] != ref["version"]:
        raise core.StaleError("발급 후 접수나 수거량이 바뀌었습니다. PC에서 최신 수거 결과를 확인해 주세요.")
    live = {line["line_id"]: line for line in current["lines"]}
    qty = entry["qty"]
    confirmed = entry.get("confirmed", [live[key]["remaining"] for key in ref["lines"]])
    if not all(isinstance(values, list) and len(values) == len(ref["lines"]) for values in (qty, confirmed)):
        raise core.UserError("발급된 모든 품목의 수량을 확인해 주세요.")
    items = {key: {"line_id": key, "qty": 0} for key in live}
    detail = []
    changed = False
    for key, n, count in zip(ref["lines"], qty, confirmed):
        line = live[key]
        _number(n, "수거 수량")
        _number(count, "현장 확인 수량")
        if n > count or line["previous"] + count > 9999:
            raise core.UserError("현장 확인 수량과 이번 수거 수량을 확인해 주세요.")
        items[key].update(qty=n, confirmed_qty=line["previous"] + count)
        changed |= count != line["remaining"]
        detail.append(f"{line['name']} {line['spec'] or ''} · 잔여 {line['remaining']} → 현장 {count}, 수거 {n}")
    additions = _items(entry["additions"], additions=True)
    reason = _text(entry["reason"], "사유", 200, optional=True)
    for item in additions:
        detail.append(f"추가 {item['name']} · 현장 {item['confirmed_qty']}, 수거 {item['qty']}")
    now_collected = sum(qty) + sum(item["qty"] for item in additions)
    total = current["previous_total"] + now_collected
    remaining = sum(confirmed) + sum(item["confirmed_qty"] for item in additions) - now_collected
    outcome = "수거 완료" if total and not remaining else "부분 수거" if total else "미수거"
    if (changed or additions or outcome != "수거 완료") and not reason:
        raise core.UserError("현장 변동·부분 수거·미수거 사유를 입력해 주세요.")
    data = {
        "req_ver": ref["version"],
        "basis": ref["basis"],
        "client_req_id": entry["id"],
        "collected_on": ticket["date"],
        "items": list(items.values()),
        "field_items": additions,
        "reason": reason,
    }
    if reason:
        detail.append(reason)

    def save():
        return pc_collection.save(con, req["id"], data, guest_entry=entry["id"])["id"]

    return f"{entry['seq']}번 · {outcome}", " / ".join([req["address"] or "", *detail]), save


def _prepare(con, ticket, entry):
    if entry["kind"] == "collection":
        return _prepare_collection(con, ticket, entry)
    if entry["kind"] == "unload":
        site = _text(entry["site"], "반입지", 120)
        kg = entry["weight_kg"]
        if type(kg) not in (float, int) or not 0 < kg <= 1_000_000 or not math.isfinite(kg):
            raise core.UserError("하차 중량은 0보다 크고 1,000,000kg 이하로 입력해 주세요.")
        data = {"crew_id": ticket["crew"], "work_date": ticket["date"], "place": site, "weight_kg": kg}
        return "하차", f"{site} · {kg:g} kg", lambda: assign.add_unload(con, data)
    name = _text(entry["name"], "신청인 이름", 60)
    phone = core.norm_phone(_text(entry["phone"], "연락처", 30))
    if not re.fullmatch(r"0[0-9]{8,10}", phone):
        raise core.UserError("현장 접수 연락처를 확인해 주세요.")
    address = _text(entry["address"], "배출 주소", 300)
    dong = _text(entry["dong"], "행정동", 20, optional=True)
    if dong and not area.get_dong(con, dong):
        raise core.UserError("현장 접수의 행정동을 확인해 주세요.")
    dong = results.field_intake_dong(con, address, dong)
    items = _items(entry["items"])
    data = {
        "name": name,
        "phone": phone,
        "address": address,
        "dong": dong,
        "items": items,
        "planned_date": ticket["date"],
    }
    description = " / ".join(f"{i['name']} {i['qty']}개" for i in items)

    def save():
        rid = str(uuid.uuid4())
        results._field_request(con, rid, data, "GUEST-WORK:" + ticket["crew"])
        return rid

    return "현장 접수", f"{name} · {phone} · {address} {dong} · {description} (단가 확인 대기)", save


_FIELDS = {
    "collection": {"seq", "qty", "reason", "additions"},
    "intake": {"name", "phone", "address", "dong", "items"},
    "unload": {"site", "weight_kg"},
}


def _validate_entry(entry, ids, visits):
    if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str) or entry["kind"] not in _FIELDS:
        raise core.UserError("현장 결과의 종류를 확인해 주세요.")
    kind = entry["kind"]
    fields = set(entry) - {"id", "kind"}
    if kind == "collection":
        fields.discard("confirmed")
    if fields != _FIELDS[kind]:
        raise core.UserError("현장 결과의 입력 항목을 확인해 주세요.")
    eid = entry.get("id")
    try:
        if not isinstance(eid, str) or str(uuid.UUID(eid)) != eid or eid in ids:
            raise ValueError
    except ValueError as exc:
        raise core.UserError("현장 기록 번호가 올바르지 않거나 중복되었습니다.") from exc
    ids.add(eid)
    if kind == "collection":
        seq = entry["seq"]
        if type(seq) is not int or seq in visits:
            raise core.UserError("방문 순번이 올바르지 않거나 중복되었습니다.")
        visits.add(seq)


def _review_token(key, file, user, stamp, reviewed):
    proof = hmac.digest(key, _json([user, stamp, hashlib.sha256(_json(file)).hexdigest(), reviewed]), "sha256")
    return f"{stamp}.{_encode(proof)}"


def review(con, body, user, *, apply=False):
    if not isinstance(body, dict):
        raise core.UserError("현장 결과 파일을 선택해 주세요.")
    file = body.get("file")
    ticket, entries = _open_file(con, file)
    proof_key = _pc_secret(con, b"dg.guest-work-review/1")
    now = int(time.time())
    stamp = now
    if apply:
        proof = body.get("preview_token")
        if not isinstance(proof, str) or not re.fullmatch(r"[0-9]{1,12}\.[A-Za-z0-9_-]{43}", proof):
            raise core.UserError("결과 파일을 먼저 미리 보고 확인해 주세요.")
        stamp = int(proof.split(".")[0])
        if not now - REVIEW_SECONDS <= stamp <= now:
            raise core.StaleError("검토 시간이 지났습니다. 다시 미리 보세요.")
    with core.tx(con):
        batch = core.one(
            con,
            """SELECT b.*,c.name AS crew_name FROM assignment_batch b
            JOIN crew c ON c.id=b.crew_id WHERE b.id=?""",
            (ticket["batch"],),
        )
        if not batch or batch["crew_id"] != ticket["crew"] or batch["work_date"] != ticket["date"]:
            raise core.UserError("이 PC에서 발급한 팀과 배차를 찾을 수 없습니다.")
        if not core.one(
            con,
            "SELECT id FROM audit_log WHERE id=? AND entity='guest_work_list' AND entity_id=? AND action='ISSUE'",
            (ticket["issue"], ticket["batch"]),
        ):
            raise core.UserError("현장 업무 QR 발급 기록을 찾을 수 없습니다.")
        closed = core.close_info(con, ticket["date"])
        ids, visits, rows, actions = set(), set(), [], []
        reviewed = [batch["crew_name"]]
        for entry in entries:
            _validate_entry(entry, ids, visits)
            # 감사 이력에 개인정보 원문이나 사전 대입 가능한 평문 해시를 남기지 않는다.
            try:
                digest = hmac.new(proof_key, _json([ticket["batch"], entry]), hashlib.sha256).hexdigest()
            except (ValueError, RecursionError) as exc:
                raise core.UserError("현장 결과 값이 올바르지 않습니다.") from exc
            prior = core.one(
                con,
                "SELECT detail FROM audit_log WHERE entity='guest_work' AND entity_id=? AND action='APPLY'",
                (entry["id"],),
            )
            if prior:
                receipt = json.loads(prior["detail"])
                if receipt["digest"] != digest:
                    raise core.UserError(
                        "같은 기록 번호로 다른 현장 결과가 제출됐습니다. 먼저 반영된 내용을 확인해 주세요."
                    )
                reviewed.append(receipt["reviewed"])
                rows.append(
                    {
                        "label": {"collection": "수거 결과", "intake": "현장 접수", "unload": "하차"}[entry["kind"]],
                        "detail": "이미 담당자가 반영한 기록입니다.",
                        "duplicate": True,
                    }
                )
                continue
            if closed or batch["status"] != "ACTIVE" or ticket["date"] > core.today_kst():
                raise core.UserError("수거일 전이거나 이미 마감·재배정한 배차입니다. PC 배차 상태를 확인해 주세요.")
            core.require_current_crew(con, ticket["crew"])
            label, detail, action = _prepare(con, ticket, entry)
            rows.append({"label": label, "detail": detail, "duplicate": False})
            view_hash = hmac.digest(proof_key, _json([label, detail]), "sha256").hex()
            reviewed.append(view_hash)
            actions.append((action, entry, digest, view_hash))
        expected = _review_token(proof_key, file, user, stamp, reviewed)
        if not apply:
            privacy.record_access(con, "VIEW", ticket["batch"], len(rows), "수거팀 현장 결과 파일 미리보기")
            return {
                "work_date": ticket["date"],
                "crew": batch["crew_name"],
                "rows": rows,
                "preview_token": expected,
            }
        if not hmac.compare_digest(proof, expected):
            raise core.StaleError("검토한 내용 또는 담당자가 바뀌었습니다. 파일을 다시 미리 보세요.")
        for action, entry, digest, view_hash in actions:
            record_id = action()
            core.audit(
                con,
                "guest_work",
                entry["id"],
                "APPLY",
                {
                    "kind": entry["kind"],
                    "batch_id": ticket["batch"],
                    "record_id": record_id,
                    "digest": digest,
                    "reviewed": view_hash,
                },
            )
        return {"applied": len(actions), "duplicates": len(rows) - len(actions)}
