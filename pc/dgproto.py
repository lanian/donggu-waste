"""동구 대형폐기물 시제품 - 공통 전송 규격 (PC 측 구현)

규격 v1 요약
- 봉투(envelope): 헤더 52B | 임시 공개키 65B | nonce 12B | 암호문 길이 4B | 암호문+태그 | 서명 64B(r||s)
  헤더 = 'DG' | ver(1) | type(1) | bundle_id(16) | device_id(16) | assign_ver(u32) | data_ver(u32) | signer_key_id(8)
- 키: 묶음마다 임시 ECDH P-256 키 → HKDF-SHA256(salt=bundle_id, info='DG-v1'+type) 64B
       앞 32B = AES-256-GCM 키, 뒤 32B = 수신 확인 코드용 HMAC 키
- AAD = 헤더 52B. 평문 = deflate-raw(JSON UTF-8)
- 서명: ECDSA P-256 SHA-256, 서명 대상 = 서명 앞의 모든 바이트
- 조각(frame): 'DG' | ver | 0x10 | bundle_id(16) | idx(u16) | total(u16) | crc32(u32) | body
  → Base45 → QR 영숫자 모드
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import zlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"DG"
VERSION = 1
TYPE_DAYLIST = 1
TYPE_RESULT = 3
FRAME_CHUNK = 0x10
HEADER_LEN = 52
FRAME_HEAD_LEN = 2 + 1 + 1 + 16 + 2 + 2 + 4  # 28
CURVE = ec.SECP256R1()


class ProtocolError(Exception):
    """규격 위반. code는 화면/로그용 오류 코드."""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}")
        self.code = code


# ---------------------------------------------------------------- Base45 (RFC 9285)
B45 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
B45_IDX = {c: i for i, c in enumerate(B45)}


def b45encode(data: bytes) -> str:
    out = []
    for i in range(0, len(data) - 1, 2):
        n = data[i] * 256 + data[i + 1]
        c, n = n % 45, n // 45
        d, e = n % 45, n // 45
        out += [B45[c], B45[d], B45[e]]
    if len(data) % 2:
        n = data[-1]
        out += [B45[n % 45], B45[n // 45]]
    return "".join(out)


def b45decode(s: str) -> bytes:
    try:
        vals = [B45_IDX[c] for c in s]
    except KeyError as exc:
        raise ProtocolError("QR_BAD_ENCODING", "Base45 문자가 아님") from exc
    out = bytearray()
    for i in range(0, len(vals), 3):
        chunk = vals[i : i + 3]
        if len(chunk) == 3:
            n = chunk[0] + chunk[1] * 45 + chunk[2] * 2025
            if n > 0xFFFF:
                raise ProtocolError("QR_BAD_ENCODING")
            out += bytes([n >> 8, n & 0xFF])
        elif len(chunk) == 2:
            n = chunk[0] + chunk[1] * 45
            if n > 0xFF:
                raise ProtocolError("QR_BAD_ENCODING")
            out.append(n)
        else:
            raise ProtocolError("QR_BAD_ENCODING")
    return bytes(out)


# ---------------------------------------------------------------- 키 유틸
def gen_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(CURVE)


def pub_raw(key) -> bytes:
    pub = key.public_key() if hasattr(key, "public_key") else key
    return pub.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def pub_from_raw(raw: bytes) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(CURVE, raw)


def key_id(sign_pub_raw: bytes) -> bytes:
    return hashlib.sha256(sign_pub_raw).digest()[:8]


def fingerprint(pc_sign: bytes, pc_kex: bytes, device_id: bytes, dev_kex: bytes, dev_sign: bytes) -> str:
    """사람이 두 화면에서 대조하는 9자리 지문. PC 서명·키 합의 공개키, 기기 번호·두 공개키를 모두 덮는다
    (등록 QR의 키 합의 키만 바꿔치기하거나 기기 코드를 바꿔치기하면 숫자가 달라진다)."""
    h = hashlib.sha256(b"DGFP2" + pc_sign + pc_kex + device_id + dev_kex + dev_sign).digest()
    return f"{int.from_bytes(h[:5], 'big') % 1_000_000_000:09d}"


def sign_raw(priv: ec.EllipticCurvePrivateKey, data: bytes) -> bytes:
    r, s = decode_dss_signature(priv.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify_raw(pub: ec.EllipticCurvePublicKey, sig: bytes, data: bytes) -> None:
    if len(sig) != 64:
        raise ProtocolError("AUTH_FAIL", "서명 길이")
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        pub.verify(der, data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ProtocolError("AUTH_FAIL", "서명 불일치") from exc


def _derive(shared: bytes, bundle_id: bytes, btype: int) -> tuple[bytes, bytes]:
    okm = HKDF(hashes.SHA256(), 64, salt=bundle_id, info=b"DG-v1" + bytes([btype])).derive(shared)
    return okm[:32], okm[32:]


def receipt_code(ack_key: bytes, bundle_id: bytes) -> str:
    """수신 확인 코드 8자 (Base32, 혼동 문자 제외 알파벳)."""
    mac = hmac.new(ack_key, b"ack" + bundle_id, hashlib.sha256).digest()
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 31자, I L O 0 1 제외
    n = int.from_bytes(mac[:8], "big")
    out = []
    for _ in range(8):
        n, r = divmod(n, len(alphabet))
        out.append(alphabet[r])
    return "".join(out)


# ---------------------------------------------------------------- 봉투
@dataclass
class Header:
    btype: int
    bundle_id: bytes
    device_id: bytes
    assign_ver: int
    data_ver: int
    signer_key_id: bytes

    def pack(self) -> bytes:
        return (
            MAGIC
            + bytes([VERSION, self.btype])
            + self.bundle_id
            + self.device_id
            + struct.pack(">II", self.assign_ver, self.data_ver)
            + self.signer_key_id
        )

    @classmethod
    def unpack(cls, b: bytes) -> "Header":
        if len(b) < HEADER_LEN or b[:2] != MAGIC:
            raise ProtocolError("BAD_FORMAT", "헤더")
        if b[2] != VERSION:
            raise ProtocolError("BAD_VERSION", f"프로토콜 {b[2]}")
        av, dv = struct.unpack(">II", b[36:44])
        return cls(b[3], b[4:20], b[20:36], av, dv, b[44:52])


def seal(
    payload: dict,
    header: Header,
    recipient_kex_pub: ec.EllipticCurvePublicKey,
    signer: ec.EllipticCurvePrivateKey,
) -> tuple[bytes, str]:
    """평문 dict를 암호화·서명한 봉투 바이트와 수신 확인 코드를 돌려준다."""
    comp = zlib.compressobj(9, zlib.DEFLATED, -15)
    plain = comp.compress(json.dumps(payload, ensure_ascii=False).encode()) + comp.flush()
    eph = gen_key()
    shared = eph.exchange(ec.ECDH(), recipient_kex_pub)
    aes_key, ack_key = _derive(shared, header.bundle_id, header.btype)
    nonce = os.urandom(12)  # 키가 묶음마다 새로 생기므로 키당 1회 사용
    head = header.pack()
    ct = AESGCM(aes_key).encrypt(nonce, plain, head)
    body = head + pub_raw(eph) + nonce + struct.pack(">I", len(ct)) + ct
    return body + sign_raw(signer, body), receipt_code(ack_key, header.bundle_id)


def open_envelope(
    data: bytes,
    signer_pub: ec.EllipticCurvePublicKey,
    recipient_kex_priv: ec.EllipticCurvePrivateKey,
    expect_device_id: bytes | None = None,
) -> tuple[Header, dict, str]:
    """서명 검증 → 복호화 → 압축 해제. 실패 시 ProtocolError, 부분 결과 없음."""
    head = Header.unpack(data)
    if expect_device_id is not None and head.device_id != expect_device_id:
        raise ProtocolError("WRONG_DEVICE")
    if len(data) < HEADER_LEN + 65 + 12 + 4 + 16 + 64:
        raise ProtocolError("BAD_FORMAT", "길이")
    body, sig = data[:-64], data[-64:]
    verify_raw(signer_pub, sig, body)
    off = HEADER_LEN
    eph_raw = body[off : off + 65]
    off += 65
    nonce = body[off : off + 12]
    off += 12
    (ct_len,) = struct.unpack(">I", body[off : off + 4])
    off += 4
    ct = body[off:]
    if len(ct) != ct_len:
        raise ProtocolError("BAD_FORMAT", "암호문 길이")
    shared = recipient_kex_priv.exchange(ec.ECDH(), pub_from_raw(eph_raw))
    aes_key, ack_key = _derive(shared, head.bundle_id, head.btype)
    try:
        plain = AESGCM(aes_key).decrypt(nonce, ct, body[:HEADER_LEN])
    except InvalidTag as exc:
        raise ProtocolError("AUTH_FAIL", "인증 태그") from exc
    payload = json.loads(zlib.decompress(plain, -15).decode())
    return head, payload, receipt_code(ack_key, head.bundle_id)


# ---------------------------------------------------------------- 조각
def split_frames(envelope: bytes, bundle_id: bytes, chunk_size: int = 700) -> list[str]:
    parts = [envelope[i : i + chunk_size] for i in range(0, len(envelope), chunk_size)]
    total = len(parts)
    frames = []
    for idx, body in enumerate(parts):
        head = MAGIC + bytes([VERSION, FRAME_CHUNK]) + bundle_id + struct.pack(">HH", idx, total)
        frames.append(b45encode(head + struct.pack(">I", zlib.crc32(body)) + body))
    return frames


def parse_frame(text: str) -> tuple[bytes, int, int, bytes]:
    raw = b45decode(text.strip())
    if len(raw) < FRAME_HEAD_LEN or raw[:2] != MAGIC or raw[3] != FRAME_CHUNK:
        raise ProtocolError("QR_NOT_OURS")
    if raw[2] != VERSION:
        raise ProtocolError("BAD_VERSION")
    bundle_id = raw[4:20]
    idx, total = struct.unpack(">HH", raw[20:24])
    (crc,) = struct.unpack(">I", raw[24:28])
    body = raw[28:]
    if zlib.crc32(body) != crc or idx >= total:
        raise ProtocolError("QR_CORRUPT")
    return bundle_id, idx, total, body


class Collector:
    """조각 수집기. 중복은 무시, 다른 묶음은 따로 보관, 전부 모일 때만 조합."""

    def __init__(self):
        self.bundles: dict[bytes, dict] = {}

    def add(self, text: str) -> tuple[bytes, int, int]:
        bid, idx, total, body = parse_frame(text)
        b = self.bundles.setdefault(bid, {"total": total, "parts": {}})
        if b["total"] != total:
            raise ProtocolError("QR_CORRUPT", "전체 개수 불일치")
        b["parts"].setdefault(idx, body)
        return bid, len(b["parts"]), total

    def missing(self, bid: bytes) -> list[int]:
        b = self.bundles[bid]
        return [i for i in range(b["total"]) if i not in b["parts"]]

    def assemble(self, bid: bytes) -> bytes:
        if self.missing(bid):
            raise ProtocolError("QR_INCOMPLETE")
        b = self.bundles[bid]
        return b"".join(b["parts"][i] for i in range(b["total"]))


# ---------------------------------------------------------------- 기기 등록 코드
def parse_device_code(code: str) -> tuple[bytes, bytes, bytes]:
    """PWA가 보여 주는 'DGD1.' 코드 → (device_id, kex_pub, sign_pub)."""
    import base64

    code = "".join(code.split())
    if not code.startswith("DGD1."):
        raise ProtocolError("BAD_FORMAT", "기기 코드 접두어")
    raw = base64.urlsafe_b64decode(code[5:] + "=" * (-len(code[5:]) % 4))
    if len(raw) != 16 + 65 + 65 + 4:
        raise ProtocolError("BAD_FORMAT", "기기 코드 길이")
    data, chk = raw[:-4], raw[-4:]
    if hashlib.sha256(data).digest()[:4] != chk:
        raise ProtocolError("BAD_FORMAT", "기기 코드 검사값")
    dev, kex, sig = data[:16], data[16:81], data[81:146]
    pub_from_raw(kex), pub_from_raw(sig)  # 곡선 위 점인지 확인
    return dev, kex, sig


# ---------------------------------------------------------------- 목록 압축 형식 v2
# 같은 수수료표 버전을 PC와 휴대폰이 함께 갖고 있으므로, 품목은 [행ID, 품목ID, 수량]만 보내고 이름·규격·단가는
# 휴대폰이 표에서 채운다. UUID는 22자 base64url, 빈 값·기본값은 생략한다. 휴대폰은 v1·v2를 모두 받는다.
# v0.10: 지점에 선택 항목 "d"(행정동 이름, 예: "충장동")를 더했다. 없으면 빈 값으로 풀며, 이전 앱은 모르는 키를 무시하므로
#        형식 번호(dg.daylist/2)는 그대로 둔다.
import base64 as _b64
import uuid as _uuid


def _sid(u: str) -> str:
    return _b64.urlsafe_b64encode(_uuid.UUID(u).bytes).decode().rstrip("=")


def _uid(s: str) -> str:
    return str(_uuid.UUID(bytes=_b64.urlsafe_b64decode(s + "==")))


def compact_daylist(p: dict, fee_rows: dict) -> dict:
    stops = []
    for s in p["stops"]:
        c = {"q": s["seq"], "i": _sid(s["request_id"]), "r": s["receipt_no"], "v": s["req_ver"], "n": s["name"], "p": s["phone"], "a": s["address"]}
        for k, short in (("spot_note", "o"), ("memo", "m"), ("eta", "e"), ("dong", "d")):
            if s.get(k):
                c[short] = s[k]
        if s.get("lat") is not None:
            c["y"] = [round(s["lat"], 6), round(s["lon"], 6)]
        if s.get("appt"):
            c["t"] = s["appt"]
        if s.get("revisit"):
            c["rv"] = 1
        # v0.11 선택 키: "rd" 감면율(%, 정수, 감면 있을 때만), "lv" 부과번호. 없으면 이전 형식 그대로.
        red = s.get("reduction") or {}
        if red and int(red.get("rate") or 0) > 0:
            c["rd"] = int(red["rate"])
        if s.get("levy_no"):
            c["lv"] = str(s["levy_no"])
        if s.get("unload_after"):
            u = s["unload_after"]
            c["u"] = [u.get("eta", ""), u.get("label", "반입지"), u.get("minutes", 0), u.get("volume_m3", 0)]
            if u.get("lat") is not None and u.get("lon") is not None:  # 반입지 좌표(선택, 5·6번째 값) — 휴대폰 v0.10.1 길안내용
                c["u"] += [round(float(u["lat"]), 6), round(float(u["lon"]), 6)]
        items = []
        for it in s["items"]:
            row = fee_rows.get(it.get("fee_rate_id") or "")
            entry = [_sid(it["line_id"]), it.get("fee_rate_id"), it["qty"]]
            extra = {}
            if (it.get("line_kind") or "RECEIVED") != "RECEIVED":
                extra["k"] = "F"
            if it.get("prior_collected"):
                extra["c"] = it["prior_collected"]
            row_status = "CONFIRMED" if row and row["status"] == "CONFIRMED" else "PENDING"
            if not row:
                extra.update(n=it["name"], s=it.get("spec", ""), u=it["unit"], ul=it["unit_label"])
            if it.get("unit_price") is not None and (not row or it["unit_price"] != row["price"]):
                extra["pp"] = it["unit_price"]  # 접수 당시 단가가 현재 표와 다르거나, 표 밖 품목에 유사 품목 단가를 적용했으면 그대로 보냄
            if (it.get("line_kind") or "RECEIVED") == "RECEIVED" and it.get("qty_received") not in (None, it["qty"]):
                extra["qr"] = it["qty_received"]  # 재방문: 확인 수량이 바뀌면 접수 수량을 따로
            if it.get("price_status", "PENDING") != row_status:
                extra["ps"] = it.get("price_status", "PENDING")[0]
            if extra:
                entry.append(extra)
            items.append(entry)
        c["it"] = items
        stops.append(c)
    out = {"schema": "dg.daylist/2", "work_date": p["work_date"], "crew": p["crew"], "assign_ver": p["assign_ver"],
           "fee": p["fee_table"]["version_id"], "s": stops}
    ft = p["fee_table"]
    if ft.get("base_version") and ft["base_version"] != ft["version_id"]:
        out["fb"] = ft["base_version"]          # 휴대폰 내장 표(원문) 버전
        out["fo"] = ft.get("overrides") or {}   # 승인된 확정 단가 (현장 추가 계산에 사용)
        if ft.get("effective_from"):
            out["fe"] = ft["effective_from"]
    return out


def expand_daylist(p: dict, fee_rows: dict) -> dict:
    if p.get("schema") != "dg.daylist/2":
        return p
    stops = []
    for c in p["s"]:
        items = []
        for entry in c["it"]:
            sid, rid, qty = entry[:3]
            x = entry[3] if len(entry) > 3 else {}
            row = fee_rows.get(rid or "")
            status = {"C": "CONFIRMED", "P": "PENDING"}.get(x.get("ps"), "CONFIRMED" if row and row["status"] == "CONFIRMED" else "PENDING")
            price = x.get("pp", row["price"] if row else None)
            kind = "FIELD" if x.get("k") == "F" else "RECEIVED"
            items.append({"line_id": _uid(sid), "line_kind": kind, "fee_rate_id": rid,
                          "name": x.get("n", row["name"] if row else ""), "spec": x.get("s", row["spec"] if row else ""),
                          "unit": x.get("u", row["unit"] if row else "EA"), "unit_label": x.get("ul", row["unit_label"] if row else "개"),
                          "qty": qty, "prior_collected": x.get("c", 0), "unit_price": price,
                          "qty_received": 0 if kind == "FIELD" else x.get("qr", qty),
                          "amount": price * x.get("qr", qty) if status == "CONFIRMED" and price is not None and kind == "RECEIVED" else (0 if kind == "FIELD" else None),
                          "price_status": status})
        y = c.get("y")
        stops.append({"seq": c["q"], "request_id": _uid(c["i"]), "receipt_no": c["r"], "req_ver": c["v"], "name": c["n"], "phone": c["p"],
                      "address": c["a"], "dong": c.get("d", ""), "spot_note": c.get("o", ""), "memo": c.get("m", ""), "lat": y[0] if y else None, "lon": y[1] if y else None,
                      "eta": c.get("e", ""), "appt": c.get("t"), "revisit": bool(c.get("rv")), "assign_ver": p["assign_ver"], "items": items,
                      "levy_no": c.get("lv", ""), **({"reduction": {"rate": int(c["rd"])}} if c.get("rd") else {}),
                      **({"unload_after": {"eta": c["u"][0], "label": c["u"][1], "minutes": c["u"][2], "volume_m3": c["u"][3],
                                            "lat": c["u"][4] if len(c["u"]) > 5 else None, "lon": c["u"][5] if len(c["u"]) > 5 else None}} if c.get("u") else {})})
    ft = {"version_id": p["fee"]}
    if p.get("fb"):
        ft.update(base_version=p["fb"], overrides=p.get("fo") or {}, effective_from=p.get("fe"))
    return {"schema": "dg.daylist/1", "work_date": p["work_date"], "crew": p["crew"], "assign_ver": p["assign_ver"],
            "fee_table": ft, "stops": stops}
