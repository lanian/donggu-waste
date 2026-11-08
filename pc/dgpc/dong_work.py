"""동별 업무의 명시적 권한 경계. 기존 전역 역할·서비스의 의미는 바꾸지 않는다."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import secrets
import time

from . import area as A, core as C, routing, service as S, users as U

READ_ROLES = frozenset({"ADMIN", "INTAKE", "LEAD", "DONG"})
WRITE_ROLES = frozenset({"ADMIN", "INTAKE", "DONG"})
METRICS = ("scheduled", "done", "unassigned", "revisit", "overdue", "no_date")
_PROOF_KEY = secrets.token_bytes(32)
_OPEN = "status NOT IN ('DONE','CANCELLED')"
_REVISIT = "status IN ('PARTIAL','NOT_COLLECTED','ON_HOLD')"


class ScopeError(C.UserError):
    """대상 원문·ID를 오류에 넣지 않는 403 권한 오류."""


def actor(con, user: dict, *, write=False) -> dict:
    uid = user.get("user_id") or user.get("id")
    u = C.one(con, "SELECT id,username,role,active,must_change FROM app_user WHERE id=?", (uid,))
    if not u or not u["active"] or u["role"] not in (WRITE_ROLES if write else READ_ROLES):
        raise ScopeError("이 동별 업무를 처리할 권한이 없습니다.")
    u["dong_ids"] = U.user_dong_ids(con, uid) if u["role"] == "DONG" else []
    if write and u["role"] == "DONG" and not u["dong_ids"]:
        raise ScopeError("담당 동이 없습니다. 운영 관리자에게 담당 동 지정을 요청하세요.")
    u["scope"] = {"mode": "assigned" if u["role"] == "DONG" else "all", "dong_ids": u["dong_ids"]}
    return u


def fingerprint(u: dict) -> str:
    return hashlib.sha256(json.dumps([u["id"], u["role"], u["dong_ids"]], separators=(",", ":")).encode()).hexdigest()


def _selection(con, u: dict, selected="all") -> tuple[str, list]:
    if selected in (None, ""):
        selected = "all"
    if not isinstance(selected, str):
        raise C.UserError("행정동을 목록에서 골라 주세요.")
    if selected == "all":
        if u["role"] != "DONG":
            return "1=1", []
        return "dong IN (SELECT name FROM dong WHERE id IN (SELECT value FROM json_each(?)))", [json.dumps(u["dong_ids"])]
    if selected == "-":
        if u["role"] == "DONG":
            raise ScopeError("담당 동 안에서만 조회할 수 있습니다.")
        return "dong IS NULL", []
    if u["role"] == "DONG" and selected not in u["dong_ids"]:
        raise ScopeError("담당 동 안에서만 조회할 수 있습니다.")
    d = C.one(con, "SELECT name FROM dong WHERE id=?", (selected,))
    if not d:
        raise C.UserError("등록된 행정동을 골라 주세요.")
    return "dong=?", [d["name"]]


def _date(value) -> str:
    value = value or C.today_kst()
    if not isinstance(value, str) or not C._date_ok(value):
        raise C.UserError("기준일을 날짜로 골라 주세요.")
    return value


def safe_dongs(con, u: dict) -> list[dict]:
    keys = ("id", "name", "active", "crew_id", "crew_name", "weekdays", "daily_limit", "daily_limit_m3")
    return [{k: d[k] for k in keys} for d in A.list_dongs(con)
            if u["role"] != "DONG" or d["id"] in u["dong_ids"]]


def safe_crews(con) -> list[dict]:
    keys = ("id", "name", "active", "capacity_m3", "capacity_kg", "shift_start", "shift_end")
    return [{k: c[k] for k in keys} for c in S.list_crews(con)]


def safe_settings(con) -> dict:
    settings = S.settings(con)
    keys = ("contact_info", "disposal_guide", "payment_guide", "reductions", "reduction_round_unit", "intake_notices")
    return {k: settings[k] for k in keys if k in settings}


def _ids(con, u: dict, selected="all", *, date=None, bucket=None) -> set[str]:
    where, args = _selection(con, u, selected)
    clauses = {"day": ("planned_date=? AND status<>'CANCELLED'", [date]),
               "done": ("planned_date=? AND status='DONE'", [date]),
               "unassigned": (f"planned_date=? AND {_OPEN} AND active_crew_id IS NULL", [date]),
               "overdue": (f"planned_date<? AND {_OPEN}", [date]),
               "no_date": (f"planned_date IS NULL AND {_OPEN}", []),
               "revisit": (f"planned_date=? AND {_REVISIT}", [date])}
    if bucket:
        if bucket not in clauses:
            raise C.UserError("접수 목록 종류를 골라 주세요.")
        extra, values = clauses[bucket]
        where += " AND " + extra
        args += values
    return {r["id"] for r in C.rows(con, "SELECT id FROM request WHERE " + where, args)}


def require_request(con, u: dict, rid: str) -> dict:
    where, args = _selection(con, u)
    r = C.one(con, "SELECT id,dong,planned_date,status,req_ver FROM request WHERE id=? AND " + where, [rid, *args])
    if not r:
        raise ScopeError("담당 범위에서 접수를 찾을 수 없습니다.")
    return r


def overview(con, user: dict, date=None, selected="all") -> dict:
    u, date = actor(con, user), _date(date)
    selected = selected or "all"
    where, args = _selection(con, u, selected)
    exprs = ["planned_date=? AND status<>'CANCELLED'", "planned_date=? AND status='DONE'",
             f"planned_date=? AND {_OPEN} AND active_crew_id IS NULL", f"planned_date=? AND {_REVISIT}",
             f"planned_date<? AND {_OPEN}", f"planned_date IS NULL AND {_OPEN}"]
    sql = "SELECT dong," + ",".join(f"SUM(CASE WHEN {e} THEN 1 ELSE 0 END) AS {k}" for k, e in zip(METRICS, exprs))
    grouped = C.rows(con, sql + " FROM request WHERE " + where + " GROUP BY dong", [date] * 5 + args)
    by_name = {r["dong"]: r for r in grouped}
    dongs = safe_dongs(con, u)
    out = []
    for d in dongs:
        if selected not in ("all", d["id"]):
            continue
        values = by_name.get(d["name"], {})
        out.append({"id": d["id"], "name": d["name"], **{k: int(values.get(k) or 0) for k in METRICS}})
    if u["role"] != "DONG" and selected in ("all", "-"):
        values = by_name.get(None, {})
        out.append({"id": "-", "name": "동 미지정", **{k: int(values.get(k) or 0) for k in METRICS}})
    # 전체 범위의 오래된 미등록 동 자료도 총계에는 포함한다.
    summary = {k: sum(int(r[k] or 0) for r in grouped) for k in METRICS}
    return {"date": date, "selected_dong": selected, "scope": u["scope"], "dongs": dongs,
            "crews": safe_crews(con), "summary": summary, "rows": out}


def search(con, user: dict, query: dict) -> dict:
    u, date = actor(con, user), _date(query.get("date"))
    ids = _ids(con, u, query.get("dong") or "all", date=date, bucket=query.get("bucket") or "day")
    return S.search_requests(con, {"q": query.get("q") or "", "page": query.get("page") or 1, "size": 50}, ids, with_counts=False)


def detail(con, user: dict, rid: str) -> dict:
    require_request(con, actor(con, user), rid)
    return S.get_request(con, rid)


def duplicates(con, user: dict, query: dict) -> list[dict]:
    u = actor(con, user)
    allowed = _ids(con, u, query.get("dong") or "all")
    return [r for r in S.find_duplicates(con, query.get("phone") or "", query.get("address") or "", query.get("exclude")) if r["id"] in allowed]


def geocode(con, user: dict, query: dict) -> dict:
    u = actor(con, user)
    selected = query.get("dong")
    if not selected or selected in ("all", "-"):
        raise C.UserError("위치를 찾을 접수의 행정동을 먼저 골라 주세요.")
    _selection(con, u, selected)
    return S.geocode(con, query.get("address") or "") or {"none": True}


def _destination(con, u: dict, data: dict) -> dict:
    did = data.get("dong_id")
    if not isinstance(did, str) or not did or did in ("all", "-"):
        raise C.UserError("접수할 행정동을 명시적으로 골라 주세요.")
    _selection(con, u, did)
    d = C.one(con, "SELECT name,active FROM dong WHERE id=?", (did,))
    if not d or not d["active"]:
        raise C.UserError("사용 중인 행정동을 골라 주세요.")
    return {**data, "dong": d["name"]}


def create(con, user: dict, data: dict) -> dict:
    u = actor(con, user, write=True)
    body = _destination(con, u, data)
    with C.tx(con):
        result = C.idempotent(con, u["username"], "dong-work/requests", data.get("client_req_id"), lambda: S.create_request(con, body))
        require_request(con, u, result["id"])
    return _write_response(con, u, result)


def update(con, user: dict, rid: str, data: dict) -> dict:
    u = actor(con, user, write=True)
    require_request(con, u, rid)
    body = _destination(con, u, data)
    return _write_response(con, u, S.update_request(con, rid, body, body.get("reason") or ""))


def _write_response(con, u: dict, result: dict) -> dict:
    """기존 서비스의 전체 예약 건수 경고를 담당 동 계정에 전달하지 않는다."""
    if u["role"] != "DONG":
        return result
    req = C.one(con, "SELECT id,address,planned_date,dong FROM request WHERE id=?", (result["id"],))
    warnings = S.address_warnings(con, req["address"])
    day = req["planned_date"]
    if day:
        dong = A.get_dong(con, req["dong"])
        reason = A.working_reason(con, day, dong)
        if reason:
            warnings.append(f"{day}은(는) {reason}입니다.")
        if dong:
            count, volume = A.booked(con, day, dong["name"], req["id"])
            over = []
            if dong["daily_limit"] and count + 1 > dong["daily_limit"]:
                over.append(f"{dong['name']} 한도 {dong['daily_limit']}건 (이미 {count}건)")
            if dong["daily_limit_m3"] and volume + A.request_volume(con, req["id"]) > dong["daily_limit_m3"]:
                over.append(f"{dong['name']} 적재 한도 {dong['daily_limit_m3']:g}m³ (이미 약 {volume:g}m³)")
            if over:
                warnings.append(f"{day} 수거 한도를 넘습니다: " + ", ".join(over) + ".")
        if C.close_info(con, day):
            warnings.append(f"{day}은(는) 이미 마감한 날짜입니다.")
    return {**result, "warnings": warnings}


def cancel(con, user: dict, rid: str, data: dict) -> dict:
    u = actor(con, user, write=True)
    require_request(con, u, rid)
    return S.cancel_request(con, rid, data.get("reason") or "", data.get("code") or None, data.get("req_ver"))


def assignments(con, user: dict, date=None, selected="all") -> list[dict]:
    u, date = actor(con, user), _date(date)
    allowed = _ids(con, u, selected or "all")
    out = []
    for batch in S.current_assignment(con, date):
        stops = batch["stops"]
        visible = [s for s in stops if s["request_id"] in allowed]
        if not visible:
            continue
        batch["shared"] = len(visible) != len(stops)
        batch["stops"] = visible
        out.append(batch)
    return out


def _crew_versions(con, date: str, crews: list[str]) -> dict:
    return {cid: C.one(con, "SELECT COALESCE(MAX(assign_ver),0) AS v FROM assignment_batch WHERE work_date=? AND crew_id=?", (date, cid))["v"] for cid in crews}


def _proof(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode() + "." + hmac.new(_PROOF_KEY, raw, hashlib.sha256).hexdigest()


def _read_proof(value) -> dict:
    try:
        raw64, signature = str(value).rsplit(".", 1)
        raw = base64.urlsafe_b64decode(raw64)
        if not hmac.compare_digest(signature, hmac.new(_PROOF_KEY, raw, hashlib.sha256).hexdigest()):
            raise ValueError()
        payload = json.loads(raw)
        if time.time() - payload["at"] > 1800:
            raise ValueError()
        return payload
    except (ValueError, TypeError, KeyError):
        raise C.StaleError("노선 계산 정보가 없거나 만료되었습니다. 다시 계산하세요.")


def prepare_route(con, user: dict, data: dict) -> dict:
    """반영 잠금에서 현재 범위·버전과 계산에 필요한 자료만 준비한다."""
    u, date, selected = actor(con, user, write=True), _date(data.get("date")), data.get("dong") or "all"
    ids = _ids(con, u, selected, date=date, bucket="day")
    requested = data.get("crew_ids")
    if not isinstance(requested, list) or any(not isinstance(cid, str) for cid in requested):
        raise C.UserError("계산할 수거팀을 골라 주세요.")
    wanted = set(requested)
    crews = [c for c in S.list_crews(con) if c["id"] in wanted and c["active"]]
    if not wanted or {c["id"] for c in crews} != wanted:
        raise C.UserError("사용 중인 수거팀을 하나 이상 골라 주세요.")
    reqs, page = [], 1
    while True:
        part = S.search_requests(con, {"size": 500, "page": page}, ids, with_counts=False)
        reqs.extend(r for r in part["rows"] if r["status"] not in ("DONE", "CANCELLED"))
        if page >= part["pages"]:
            break
        page += 1
    versions = {r["id"]: r["req_ver"] for r in reqs}
    for r in reqs:
        r["waiting_days"] = max(0, (time.time() - time.mktime(time.strptime(r["received_at"][:10], "%Y-%m-%d"))) // 86400)
        if r["state"]["has_field_records"]:
            r["items"] = [{"name": line["name"], "spec": line["spec"], "qty": line["remaining"], "fee_rate_id": line["fee_rate_id"]}
                          for line in r["state"]["lines"] if line["remaining"]]
    crew_versions = _crew_versions(con, date, sorted(wanted))
    basis = {"date": date, "dong": selected, "crew_versions": crew_versions}
    signed = {**basis, "scope": fingerprint(u), "requests": versions, "at": time.time()}
    basis["proof"] = _proof(signed)
    method = S.travel_method(con)
    return {"user_scope": fingerprint(u), "basis": basis, "requests": reqs, "crews": crews, "settings": S.settings(con),
            "dongs": A.list_dongs(con), "method": method, "kakao_key": S.secret(con, "kakao_rest_key") if method == "KAKAO" else "",
            "overrides": S.volume_overrides(con)}


def calculate(prepared: dict) -> dict:
    reqs, crews, settings, ov = prepared["requests"], prepared["crews"], prepared["settings"], prepared["overrides"]
    dongs = {d["name"]: d for d in prepared["dongs"]}
    pref = {r["id"]: dongs[r["dong"]]["crew_id"] for r in reqs if r.get("dong") in dongs and dongs[r["dong"]]["crew_id"]} if settings["capacity"]["prefer_dong_crew"] else {}
    result = routing.plan(reqs, crews, settings["depot"], osrm_url=settings["osrm_url"], service_base=settings["service_min_base"],
                          travel_method=prepared["method"], kakao_key=prepared["kakao_key"], service_per_item=settings["service_min_per_item"],
                          time_limit_s=routing.clamp_time_limit(min(20, max(5, len(reqs) // 10))), volume_overrides=ov,
                          unload_site=settings["unload_site"], unload_min=settings["unload_min"], max_unloads=settings["max_unloads"],
                          end_site=settings["end_site"], crew_pref=pref, unload_hours=settings["unload_hours"], lunch=settings["lunch"], base_first=settings["base_first"])
    result["prefer_dong_crew"] = bool(settings["capacity"]["prefer_dong_crew"])
    result["requests"] = {r["id"]: {"receipt_no": r["receipt_no"], "name": r["name"], "address": r["address"], "lat": r["lat"], "lon": r["lon"],
                                     "must_today": r["must_today"], "appt": [r["appt_start"], r["appt_end"]], "status": r["status"], "dong": r["dong"],
                                     "pref_crew_id": pref.get(r["id"]), "items": [f'{i["name"]} {i["spec"]} {i["qty"]}' for i in r.get("items", [])],
                                     "m3": round(sum(routing.volume_of(i, ov) for i in r.get("items", [])), 2),
                                     "kg": int(sum(routing.weight_of(i) for i in r.get("items", []))), "tags": routing.tags_of(r.get("items", []))} for r in reqs}
    result["basis"] = prepared["basis"]
    return result


def validate_route_result(con, user: dict, prepared: dict):
    u = actor(con, user, write=True)
    if fingerprint(u) != prepared["user_scope"]:
        raise C.StaleError("계산 중 담당 범위가 바뀌었습니다. 다시 조회하고 계산하세요.")
    proof = _read_proof(prepared["basis"]["proof"])
    selected = _ids(con, u, proof["dong"], date=proof["date"], bucket="day")
    for rid, version in proof["requests"].items():
        r = require_request(con, u, rid)
        if rid not in selected or r["req_ver"] != version:
            raise C.StaleError("계산 중 접수가 바뀌었습니다. 다시 조회하고 계산하세요.")


def assign(con, user: dict, data: dict) -> list[dict]:
    u, date, selected = actor(con, user, write=True), _date(data.get("date")), data.get("dong") or "all"
    allowed = _ids(con, u, selected, date=date, bucket="day")
    basis = data.get("basis")
    if not isinstance(basis, dict):
        raise C.StaleError("노선을 먼저 계산하세요.")
    proof = _read_proof(basis.get("proof"))
    if proof["scope"] != fingerprint(u) or proof["date"] != date or proof["dong"] != selected:
        raise C.StaleError("계정·담당 범위·기준일이 바뀌었습니다. 다시 계산하세요.")
    if any(basis.get(k) != proof[k] for k in ("date", "dong", "crew_versions")):
        raise C.StaleError("계산 기준이 달라졌습니다. 다시 계산하세요.")
    plan = data.get("plan")
    if not isinstance(plan, dict) or not plan or any(not isinstance(stops, list) for stops in plan.values()):
        raise C.UserError("확정할 팀별 배정 목록을 확인하세요.")
    for cid, stops in plan.items():
        if cid not in proof["crew_versions"] or not C.one(con, "SELECT 1 AS x FROM crew WHERE id=? AND active=1", (cid,)):
            raise C.StaleError("계산한 수거팀이 바뀌었습니다. 다시 계산하세요.")
        for stop in stops:
            if not isinstance(stop, dict) or not isinstance(stop.get("request_id"), str):
                raise C.UserError("배정할 접수를 확인하세요.")
            rid = stop["request_id"]
            r = require_request(con, u, rid)
            if rid not in allowed or rid not in proof["requests"] or r["req_ver"] != proof["requests"][rid] or r["status"] in ("DONE", "CANCELLED"):
                raise C.StaleError("계산한 접수의 동·예정일·내용이 바뀌었습니다. 다시 계산하세요.")

    def apply():
        current = _crew_versions(con, date, list(proof["crew_versions"]))
        if current != proof["crew_versions"]:
            raise C.StaleError("그날의 배정 버전이 바뀌었습니다. 다시 계산하세요.")
        for batch in S.current_assignment(con, date):
            if batch["crew_id"] in plan and any(stop["request_id"] not in allowed for stop in batch["stops"]):
                raise C.StaleError("다른 동의 접수가 함께 배정된 팀입니다. 전체 업무 담당자에게 배정 조정을 요청하세요.")
        return S.confirm_plan(con, date, plan)

    return C.idempotent(con, u["username"], "dong-work/assign", data.get("client_req_id"), apply)


def summary_csv(con, user: dict, date=None, selected="all") -> bytes:
    overview_ = overview(con, user, date, selected)
    out = io.StringIO(newline="")
    writer = C.safe_csv_writer(out)
    writer.writerow(["기준일", "행정동", "예정", "완료", "미배정", "재방문", "지난 미처리", "예정일 없음"])
    for row in overview_["rows"]:
        writer.writerow([overview_["date"], row["name"], *[row[key] for key in METRICS]])
    return out.getvalue().encode("utf-8-sig")
