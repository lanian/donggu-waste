"""현장 사진: 사진 묶음 가져오기(해시 확인)·보기·보관 기간 정리.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import uuid
from contextlib import nullcontext

from dgpc.core import KST, UserError, audit, get_setting, incremental_vacuum, now, one, rows, set_setting, today_kst, tx
from dgpc.devices import _open_from_device

TYPE_PHOTOS = 4


# ------------------------------------------------------------------ 사진 회수 (보안 매체로 옮긴 사진 묶음 파일)
def prepare_photos(con, data: bytes) -> dict:
    """사진 파일 검증·복호·사진마다 해시 계산(시간이 걸리는 부분). 서버는 DB 잠금 밖에서 한다."""
    sha = hashlib.sha256(data).hexdigest()
    prev = one(con, "SELECT summary_json, release_code FROM photo_import WHERE package_sha256=?", (sha,))
    if prev:
        return {"sha": sha, "duplicate": {"duplicate_file": True, **json.loads(prev["summary_json"]), "release_code": prev["release_code"]}}
    dev_id, head, payload, release = _open_from_device(con, data, TYPE_PHOTOS)
    if payload.get("schema") != "dg.photos/1":
        raise UserError("사진 파일 형식이 아닙니다.")
    photos = []
    for p in payload.get("photos", []):
        try:
            raw = base64.b64decode(p["data"])
        except (KeyError, TypeError, ValueError):
            raw = None
        photos.append({**{k: v for k, v in p.items() if k != "data"}, "raw": raw, "ok": raw is not None and hashlib.sha256(raw).hexdigest() == p.get("sha256")})
    return {"sha": sha, "dev_id": dev_id, "photos": photos, "release": release}


def import_photos(con, data: bytes, prepared: dict | None = None) -> dict:
    prep = prepared or prepare_photos(con, data)
    if prep.get("duplicate"):
        return prep["duplicate"]
    sha, dev_id, release = prep["sha"], prep["dev_id"], prep["release"]
    prev = one(con, "SELECT summary_json, release_code FROM photo_import WHERE package_sha256=?", (sha,))
    if prev:
        return {"duplicate_file": True, **json.loads(prev["summary_json"]), "release_code": prev["release_code"]}
    counts = {"recovered": 0, "already": 0, "hash_mismatch": 0, "unexpected": 0}
    pid = str(uuid.uuid4())
    with tx(con):
        for p in prep["photos"]:
            if not p["ok"]:
                counts["hash_mismatch"] += 1
                continue
            raw = p["raw"]
            cur = one(con, "SELECT sha256, recovery_status, request_id, purged_at FROM photo WHERE photo_id=?", (p["photo_id"],))
            rid = cur["request_id"] if cur else p.get("request_id")
            req = one(con, "SELECT pii_purged_at FROM request WHERE id=?", (rid,))
            if (cur and cur["purged_at"]) or (req and req["pii_purged_at"]):
                # 파기된 사진은 늦게 도착해도 되살리지 않는다. 받은 것으로 확인하되 바이트·좌표는 계속 비워 둔다.
                if cur:
                    con.execute("""UPDATE photo SET data=NULL, lat=NULL, lon=NULL, purged_at=COALESCE(purged_at, ?),
                        recovery_status='RECOVERED', recovered_at=COALESCE(recovered_at, ?) WHERE photo_id=?""", (now(), now(), p["photo_id"]))
                counts["purged"] = counts.get("purged", 0) + 1
                continue
            if cur and cur["recovery_status"] == "RECOVERED":
                counts["already"] += 1
                continue
            if cur and cur["sha256"] != p["sha256"]:
                counts["hash_mismatch"] += 1
                continue
            if not cur:
                counts["unexpected"] += 1  # 결과 파일보다 사진이 먼저 온 경우: 보관하되 표시
                con.execute("INSERT INTO photo (photo_id, request_id, phase, sha256, bytes, device_id) VALUES (?,?,?,?,?,?)",
                            (p["photo_id"], p.get("request_id"), p.get("phase"), p["sha256"], len(raw), dev_id))
            con.execute("UPDATE photo SET data=?, recovery_status='RECOVERED', recovered_at=? WHERE photo_id=?", (raw, now(), p["photo_id"]))
            counts["recovered"] += 1
        rc = release if counts["hash_mismatch"] == 0 else None
        con.execute("INSERT INTO photo_import (id, package_sha256, device_id, imported_at, summary_json, release_code) VALUES (?,?,?,?,?,?)",
                    (pid, sha, dev_id, now(), json.dumps(counts), rc))
        audit(con, "photo_import", sha[:16], "IMPORT", counts)
    # 해시가 맞지 않은 사진이 있으면 휴대폰에서 지우지 않도록 확인 코드를 주지 않는다
    return {**counts, "release_code": release if counts["hash_mismatch"] == 0 else None, "import_id": pid}


def photo_bytes(con, photo_id: str) -> bytes | None:
    r = one(con, "SELECT data FROM photo WHERE photo_id=? AND recovery_status='RECOVERED' AND purged_at IS NULL", (photo_id,))
    return r["data"] if r else None


# ------------------------------------------------------------------ 사진 보관 기간 정리
def purge_candidates(con, today: str | None = None) -> list[dict]:
    """보관 기간이 지난 사진: 회수 완료 + 접수가 끝남(수거 완료·취소) 또는 그날 마감됨 + 회수일로부터 기간 경과."""
    days = get_setting(con, "photo_retention_days")
    t = dt.date.fromisoformat(today or today_kst())
    cutoff = (dt.datetime.combine(t - dt.timedelta(days=days), dt.time()).replace(tzinfo=KST)).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return rows(con, """SELECT p.photo_id, p.request_id, p.bytes, p.recovered_at, r.receipt_no, r.status FROM photo p JOIN request r ON r.id=p.request_id
        WHERE p.recovery_status='RECOVERED' AND p.purged_at IS NULL AND p.data IS NOT NULL AND p.recovered_at < ?
          AND (r.status IN ('DONE','CANCELLED') OR EXISTS (SELECT 1 FROM day_close_log d WHERE d.work_date=r.planned_date))
        ORDER BY p.recovered_at""", (cutoff,))


def purge_photos(con, today: str | None = None, lock=None, batch: int = 500, trigger: str = "MANUAL", progress=None) -> dict:
    """보관 기간 지난 사진 바이트 지우기. 500장씩 한 트랜잭션, 묶음 사이에 lock(서버 DB 잠금)을 놓는다."""
    guard = lock if lock is not None else nullcontext()
    with guard:
        c = purge_candidates(con, today)
        days = get_setting(con, "photo_retention_days")
    n = size = 0
    for i in range(0, len(c), batch):
        ch = c[i:i + batch]
        with guard:
            with tx(con):
                t = now()
                for p in ch:
                    con.execute("UPDATE photo SET data=NULL, purged_at=? WHERE photo_id=? AND purged_at IS NULL", (t, p["photo_id"]))
                audit(con, "photo", "retention", "PURGE", {"count": len(ch), "bytes": sum(p["bytes"] or 0 for p in ch), "retention_days": days, "trigger": trigger})
        n += len(ch); size += sum(p["bytes"] or 0 for p in ch)
        if progress:
            progress(n, len(c))
    with guard:
        with tx(con):
            set_setting(con, "last_photo_purge_run", {"at": now(), "count": n, "trigger": trigger})
        if n:
            incremental_vacuum(con)
    return {"count": n, "bytes": size}


def photo_request(con, photo_id: str) -> str | None:
    r = one(con, "SELECT request_id FROM photo WHERE photo_id=?", (photo_id,))
    return r["request_id"] if r else None
