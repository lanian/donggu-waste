"""자동 백업: 백업 공개키로 암호화(비밀 없이 무인 실행) · 두 번째 보관 위치 복사 확인 · 세대 보관 · 기간 지난 백업 정리.

방식
- 설정에서 백업 키 쌍(X25519)을 만든다. 개인키는 백업 비밀번호로 암호화한 파일(PEM)로 한 번만 내려받아 오프라인 보관하고,
  PC에는 공개키만 남긴다. 그래서 자동 백업 때 비밀번호가 필요 없고, PC에 비밀번호를 저장하지도 않는다.
- 자동 백업 파일(.dgabk) = 'DGAB1' | 공개키 지문 8B | 임시 공개키 32B | nonce 12B | 길이 2B | 감싼 DB 비밀값 | SQLCipher 백업 바이트.
  매번 새 임의 비밀값으로 SQLCipher 백업을 만들고, 그 비밀값을 ECDH(X25519)+HKDF로 만든 키로 AES-GCM 암호화해 앞에 붙인다.
- 복원: 개인키 파일 + 백업 비밀번호 → 비밀값 → 기존 복원 절차(store.restore_from).
- 보관: 자동 백업은 최근 N일(하루 1개)·최근 M개월(달마다 1개)만 남기고, 수동·자동 모두 정한 일수(기본 90일)가 지나면 지운다.
  예전 백업 파일 안에도 그때의 개인정보가 들어 있으므로, 개인정보 파기와 별개로 이 보관 기간에 따라 지워야 한다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import secrets
import shutil
import time
import struct
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from dgpc import service as S, store

MAGIC = b"DGAB1"
DEFAULTS = {"backup_auto": True, "backup_time": "18:00", "backup_dest2": "", "backup_keep_daily": 14, "backup_keep_monthly": 3, "backup_retention_days": 90}
_NAME_RE = re.compile(r"donggu-(?:backup|auto)-(\d{8})-(\d{6})(?:-\d+)?\.(dgbak|dgabk)$")


def conf(con) -> dict:
    return {k: S.get_setting(con, k, v) for k, v in DEFAULTS.items()}


def save_settings(con, data: dict):
    """호출하는 쪽이 트랜잭션을 연다."""
    if "backup_auto" in data:
        S.set_setting(con, "backup_auto", bool(data["backup_auto"]))
    if "backup_time" in data:
        t = (data["backup_time"] or "").strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise S.UserError("자동 백업 시각은 HH:MM 형식입니다.")
        S.set_setting(con, "backup_time", t)
    if "backup_dest2" in data:
        b = (data["backup_dest2"] or "").strip()
        if b and not Path(b).is_absolute():
            raise S.UserError("두 번째 보관 위치는 전체 경로로 적어 주세요 (예: \\\\서버\\백업, E:\\동구백업).")
        S.set_setting(con, "backup_dest2", b)
    for k, lo, hi, what in (("backup_keep_daily", 1, 365, "일별 보관 개수"), ("backup_keep_monthly", 0, 120, "월별 보관 개수"), ("backup_retention_days", 7, 3650, "백업 보관 일수")):
        if k in data:
            try:
                n = int(data[k])
            except (TypeError, ValueError):
                raise S.UserError(f"{what}를 숫자로 적어 주세요.")
            if not lo <= n <= hi:
                raise S.UserError(f"{what}는 {lo}~{hi} 사이입니다.")
            S.set_setting(con, k, n)


# ------------------------------------------------------------------ 키
def key_info(con) -> dict | None:
    return S.get_setting(con, "backup_pubkey", None)


def _fpr(pub: bytes) -> str:
    return hashlib.sha256(pub).hexdigest()[:16]


def generate_key(con, password: str) -> tuple[bytes, dict]:
    """새 백업 키 쌍. 개인키는 비밀번호로 암호화한 PEM으로 돌려주고 PC에는 저장하지 않는다."""
    if len(password or "") < 12:
        raise S.UserError("개인키 보호 비밀번호는 12자 이상으로 정해 주세요 (DB 키와 다르게).")
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password.encode()))
    info = {"pub": pub.hex(), "fpr": _fpr(pub), "created_at": S.now(), "created_by": S.ACTOR.get()}
    with S.tx(con):
        old = key_info(con)
        S.set_setting(con, "backup_pubkey", info)
        S.audit(con, "backup", info["fpr"], "KEYGEN", {"fpr": info["fpr"], **({"replaced": old["fpr"]} if old else {})})
    return pem, info


def load_private(pem: bytes, password: str) -> X25519PrivateKey:
    try:
        k = serialization.load_pem_private_key(pem, (password or "").encode())
    except (ValueError, TypeError):
        raise S.UserError("개인키 파일을 열 수 없습니다. 개인키 보호 비밀번호를 확인하세요.")
    if not isinstance(k, X25519PrivateKey):
        raise S.UserError("백업 개인키 파일이 아닙니다.")
    return k


def _derive(shared: bytes, eph: bytes, recip: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, None, b"dg-auto-backup-v1" + eph + recip).derive(shared)


def seal_secret(pub_hex: str, secret: bytes) -> bytes:
    recip = bytes.fromhex(pub_hex)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _derive(eph.exchange(X25519PublicKey.from_public_bytes(recip)), eph_pub, recip)
    nonce = secrets.token_bytes(12)
    enc = AESGCM(key).encrypt(nonce, secret, MAGIC + eph_pub)
    return MAGIC + bytes.fromhex(_fpr(recip)) + eph_pub + nonce + struct.pack(">H", len(enc)) + enc


def open_header(raw: bytes, priv: X25519PrivateKey) -> tuple[str, int]:
    """파일 머리 → (DB 비밀값, DB 바이트 시작 위치)."""
    if raw[:5] != MAGIC or len(raw) < 61:
        raise S.UserError("자동 백업 파일(.dgabk)이 아닙니다.")
    fpr, eph_pub, nonce = raw[5:13].hex(), raw[13:45], raw[45:57]
    (n,) = struct.unpack(">H", raw[57:59])
    recip = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if _fpr(recip) != fpr:
        raise S.UserError(f"이 백업은 다른 백업 키(지문 {fpr})로 만들었습니다. 그 개인키 파일이 필요합니다.")
    key = _derive(priv.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, recip)
    try:
        secret = AESGCM(key).decrypt(nonce, raw[59:59 + n], MAGIC + eph_pub)
    except Exception:
        raise S.UserError("백업 파일이 손상되었거나 변조되었습니다.")
    return secret.decode(), 59 + n


def decrypt_to(raw: bytes, pem: bytes, password: str, dest: Path) -> str:
    """자동 백업 → SQLCipher 백업 파일(dest)과 그 비밀값."""
    secret, off = open_header(raw, load_private(pem, password))
    Path(dest).write_bytes(raw[off:])
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return secret


# ------------------------------------------------------------------ 백업 만들기
def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def status(con) -> dict:
    st = S.get_setting(con, "backup_status", {}) or {}
    last = S.get_setting(con, "last_backup_at", None)
    age_h = None
    if last:
        age_h = round((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 3600, 1)
    k = key_info(con)
    return {"last_at": last, "age_hours": age_h, "stale": age_h is None or age_h > 26, "auto": st, "key": {"fpr": k["fpr"], "created_at": k["created_at"]} if k else None,
            **conf(con)}


def mark_backup(con, ok: bool = True):
    if ok:
        S.set_setting(con, "last_backup_at", S.now())


def make_auto(con, reason: str = "SCHEDULE", lock=None) -> dict:
    """공개키로 자동 백업. 실패해도 예외를 밖으로 내지 않고 상태에 남긴다(마감 확인을 막지 않게).

    lock: 서버의 DB 잠금. 주면 DB를 읽는 동안(설정 읽기·암호화 내보내기·결과 기록)만 잡고, 시간이 오래 걸리는
    파일 작업(봉인·해시·두 번째 위치 복사·보관 정리)은 잠금 밖에서 한다 → 백업 중에도 PC 화면이 거의 멈추지 않음."""
    from contextlib import nullcontext
    guard = lock if lock is not None else nullcontext()
    with guard:
        c = conf(con)
        k = key_info(con)
        bdir = S.backup_dir(con)
    res = {"at": S.now(), "reason": reason, "ok": False}
    try:
        if not k:
            raise S.UserError("백업 키가 없어 자동 백업을 하지 않았습니다. 설정 → 백업에서 백업 키를 만드세요.")
        d = bdir
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(S.KST).strftime("%Y%m%d-%H%M%S")
        dest = d / f"donggu-auto-{stamp}.dgabk"
        n = 1
        while dest.exists():
            dest = d / f"donggu-auto-{stamp}-{n}.dgabk"; n += 1
        tmp = d / f".tmp-{secrets.token_hex(6)}.db"
        secret = secrets.token_urlsafe(32)
        try:
            with guard:
                store.backup_to(con, tmp, secret)
            head = seal_secret(k["pub"], secret.encode())
            with open(dest, "wb") as out, open(tmp, "rb") as src:
                out.write(head)
                shutil.copyfileobj(src, out)
        finally:
            for f in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
                if f.exists():
                    f.unlink()
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
        sha = _sha(dest)
        res.update(ok=True, file=dest.name, bytes=dest.stat().st_size, sha256=sha, fpr=k["fpr"])
        # 두 번째 보관 위치(네트워크 드라이브·USB): 복사 뒤 해시 대조
        if c["backup_dest2"]:
            try:
                d2 = Path(c["backup_dest2"])
                d2.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, d2 / dest.name)
                ok2 = _sha(d2 / dest.name) == sha
                res["dest2"] = {"ok": ok2, "path": str(d2), **({} if ok2 else {"error": "복사본 해시가 다릅니다"})}
            except OSError as e:
                res["dest2"] = {"ok": False, "path": c["backup_dest2"], "error": f"복사 실패({e.__class__.__name__})"}
        res["deleted"] = apply_retention(con, c, bdir)
    except S.UserError as e:
        res["error"] = str(e)
    except Exception as e:  # 디스크·권한 문제 등: 내용(경로 외)은 남기지 않는다
        res["error"] = f"자동 백업 실패({e.__class__.__name__})"
    with guard, S.tx(con):
        S.set_setting(con, "backup_status", res)
        if res["ok"]:
            mark_backup(con)
            S.set_setting(con, "auto_backup_day", S.today_kst())
        S.audit(con, "backup", res.get("file") or "-", "AUTO_BACKUP", {"ok": res["ok"], "reason": reason, **({"file": res["file"], "bytes": res["bytes"], "sha256": res["sha256"][:16]} if res["ok"] else {"error": res.get("error", "")[:120]}),
                                                                       **({"dest2_ok": res["dest2"]["ok"]} if res.get("dest2") else {}), **({"deleted": len(res["deleted"])} if res.get("deleted") else {})})
    return res


def due(con, now_kst: dt.datetime | None = None) -> bool:
    c = conf(con)
    if not c["backup_auto"] or not key_info(con):
        return False
    t = now_kst or dt.datetime.now(S.KST)
    if S.get_setting(con, "auto_backup_day", None) == t.date().isoformat():
        return False
    return t.strftime("%H:%M") >= c["backup_time"]


# ------------------------------------------------------------------ 보관 (세대·기간)
def _when(f: Path) -> dt.datetime:
    m = _NAME_RE.search(f.name)
    if m:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=S.KST)
    return dt.datetime.fromtimestamp(f.stat().st_mtime, S.KST)


def plan_retention(files: list[Path], keep_daily: int, keep_monthly: int, max_days: int, now: dt.datetime | None = None) -> list[Path]:
    """지울 파일 목록. 수동(.dgbak)은 기간만, 자동(.dgabk)은 하루 1개 최근 N일 + 달마다 1개 최근 M개월, 둘 다 기간 초과는 지움."""
    now = now or dt.datetime.now(S.KST)
    limit = now - dt.timedelta(days=max_days)
    drop, autos = [], []
    for f in files:
        w = _when(f)
        if w < limit:
            drop.append(f)
        elif f.suffix == ".dgabk":
            autos.append((w, f))
    autos.sort(reverse=True)
    keep, days, months = set(), [], {}
    for w, f in autos:
        dkey = w.date()
        if dkey not in days and len(days) < keep_daily:
            days.append(dkey); keep.add(f)
    for w, f in sorted(autos):  # 달마다 그 달 첫 백업
        months.setdefault(w.strftime("%Y-%m"), f)
    for mkey in sorted(months, reverse=True)[:keep_monthly]:
        keep.add(months[mkey])
    drop += [f for _, f in autos if f not in keep]
    return drop


def apply_retention(con, c: dict | None = None, bdir: Path | None = None) -> list[str]:
    c = c or conf(con)
    out = []
    for d in [bdir or S.backup_dir(con)] + ([Path(c["backup_dest2"])] if c["backup_dest2"] else []):
        if not d.exists():
            continue
        files = sorted([*d.glob("donggu-backup-*.dgbak"), *d.glob("donggu-auto-*.dgabk")])
        for f in plan_retention(files, int(c["backup_keep_daily"]), int(c["backup_keep_monthly"]), int(c["backup_retention_days"])):
            try:
                if time.time() - f.stat().st_mtime < 600:  # 방금 만든(또는 아직 봉인·복사 중인) 백업은 지우지 않는다
                    continue
                f.unlink()
                out.append(f.name)
            except OSError:
                pass
    return out


def run_retention(con) -> list[str]:
    gone = apply_retention(con)
    if gone:
        with S.tx(con):
            S.audit(con, "backup", "retention", "PURGE", {"count": len(gone), "days": conf(con)["backup_retention_days"]})
    return gone


def verify_auto(con, name: str, pem: bytes, password: str) -> dict:
    if "/" in name or "\\" in name or not name.endswith(".dgabk"):
        raise S.UserError("백업 파일 이름이 올바르지 않습니다.")
    src = S.backup_dir(con) / name
    if not src.exists():
        raise S.UserError("백업 파일이 없습니다.")
    tmp = S.backup_dir(con) / f".verify-{secrets.token_hex(6)}.db"
    try:
        secret = decrypt_to(src.read_bytes(), pem, password, tmp)
        try:
            bcon, n = store.open_backup(tmp, secret)
        except ValueError as e:
            raise S.UserError(str(e))
        bcon.close()
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"ok": True, "requests": n}


def restore_auto(src: Path, pem: bytes, password: str) -> dict:
    """PC 업무 화면을 끈 상태에서만(pc_tool restore-auto)."""
    d = store.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".restore-{secrets.token_hex(6)}.db"
    try:
        secret = decrypt_to(Path(src).read_bytes(), pem, password, tmp)
        return store.restore_from(tmp, secret)
    finally:
        if tmp.exists():
            tmp.unlink()
