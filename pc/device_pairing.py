"""관리자 세션에 묶인 휴대폰 등록 QR, 중계 통신과 휴대폰 서명·시간 코드 확인."""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import dgproto as P
from dgpc.external import urlopen


class PairingError(ValueError):
    """간편 등록 화면에 보여 줄 수 있는 오류."""


TTL = 300
ATTEMPTS = 5
_NET_TIMEOUT = 3
_NETWORK_SLOTS = threading.BoundedSemaphore(8)
_URL_ERROR = "중계 주소에 HTTPS 주소를 입력해 주세요. 계정·쿼리 및 상대 경로는 사용할 수 없습니다."
_STALE = "등록 QR이 만료되거나 변경되었습니다. 새 QR을 열어 주세요."
_LOCKED = "인증번호 확인 횟수를 모두 사용했습니다. 새 QR을 열어 주세요."


def relay_url(value: str) -> str:
    """외부 중계는 HTTPS, 같은 PC의 시험 서버는 127.0.0.1의 HTTP만 허용한다."""
    if value == "":
        return ""
    if not isinstance(value, str) or len(value) > 300 or not re.fullmatch(r"[!-~]+", value) or any(c in value for c in "\\?#%@"):
        raise PairingError(_URL_ERROR)
    try:
        url = urllib.parse.urlsplit(value)
        host = url.hostname
        port = url.port
        if not host or url.scheme not in ("https", "http") or url.netloc.endswith(":"):
            raise ValueError
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
            if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError
        if url.scheme == "http" and host != "127.0.0.1":
            raise ValueError
        if port == 0 or not re.fullmatch(r"[/A-Za-z0-9._~-]*", url.path) or "//" in url.path:
            raise ValueError
        if any(part in (".", "..") for part in url.path.split("/")):
            raise ValueError
        address = f"[{host}]" if ip and ip.version == 6 else host
        if port is not None:
            address += f":{port}"
        if address.lower() != url.netloc.lower():
            raise ValueError
        return f"{url.scheme}://{address}" + url.path.rstrip("/")
    except ValueError:
        raise PairingError(_URL_ERROR) from None


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value, limit):
    if not isinstance(value, str) or not 0 < len(value) <= limit or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _b64(raw) != value:
        raise ValueError
    return raw


def _sha(text):
    return hashlib.sha256(text.encode("ascii"))


def _exchange(url, data):
    req = urllib.request.Request(url, json.dumps(data).encode(), {"Content-Type": "application/json", "Accept": "application/json"})
    deadline = time.monotonic() + _NET_TIMEOUT
    try:
        with urlopen(req, timeout=_NET_TIMEOUT) as response:
            raw = bytearray()
            while len(raw) <= 8192 and time.monotonic() < deadline:
                piece = response.read1(8193 - len(raw))
                if not piece:
                    break
                raw.extend(piece)
            if len(raw) > 8192 or time.monotonic() >= deadline:
                raise PairingError("중계 응답이 너무 크거나 지연되었습니다.")
            reply = json.loads(raw.decode("utf-8"))
            if not isinstance(reply, dict):
                raise ValueError
            return reply
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (404, 410):
            raise PairingError(_STALE) from None
        raise PairingError(f"등록 중계가 요청을 거절했습니다 (HTTP {error.code}).") from None
    except (OSError, http.client.HTTPException):
        raise PairingError("등록 중계에 연결할 수 없습니다. 중계 주소와 연결 상태를 확인해 주세요.") from None
    except PairingError:
        raise
    except (ValueError, RecursionError):
        raise PairingError("등록 중계에서 올바른 응답을 받지 못했습니다.") from None


def _post(url, body):
    # DNS·프록시 연결도 포함해 PC의 대기 시간을 제한한다.
    if not _NETWORK_SLOTS.acquire(blocking=False):
        raise PairingError("등록 중계 요청이 많습니다. 잠시 후 다시 시도해 주세요.")
    result = queue.Queue(maxsize=1)

    def exchange():
        try:
            result.put(_exchange(url, body))
        except PairingError as error:
            result.put(error)
        finally:
            _NETWORK_SLOTS.release()

    threading.Thread(target=exchange, daemon=True).start()
    try:
        reply = result.get(timeout=_NET_TIMEOUT)
    except queue.Empty:
        raise PairingError("등록 중계가 3초 안에 응답하지 않았습니다. 다시 시도해 주세요.") from None
    if isinstance(reply, PairingError):
        raise reply
    return reply


def totp_number(key, step):
    mac = hmac.digest(key, step.to_bytes(8, "big"), "sha1")
    offset = mac[-1] & 15
    value = int.from_bytes(mac[offset:offset+4], "big") & 0x7fffffff
    return f"{value % 1000000:06d}"


class Pairing:
    def __init__(self, relay):
        self.relay = relay
        self.id = secrets.token_hex(16)
        self.secret = secrets.token_urlsafe(32)
        self.room = _sha("dg-pair-room-v1:" + self.secret).hexdigest()
        self._read = _sha("dg-pair-read-v1:" + self.secret).hexdigest()
        self.deadline = time.monotonic() + TTL
        self._failed = 0
        self._closed = False

    def _check(self):
        if self._closed or time.monotonic() >= self.deadline:
            raise PairingError(_STALE)
        if self._failed >= ATTEMPTS:
            raise PairingError(_LOCKED)

    def qr_text(self, registration):
        self._check()
        expires_at = int(time.time() + self.deadline - time.monotonic())
        data = {"registration": registration, "relay": self.relay, "secret": self.secret, "expires_at": expires_at}
        return "DGPAIR1." + _b64(json.dumps(data, separators=(",", ":")).encode())

    def create_remote(self):
        self._check()
        reply = _post(self.relay + "/v1/rooms", {"room": self.room, "read_hash": _sha(self._read).hexdigest()})
        if reply.get("ok") is not True:
            raise PairingError("중계에 등록 세션을 열지 못했습니다.")

    def read_payload(self):
        self._check()
        reply = _post(f"{self.relay}/v1/rooms/{self.room}/read", {"read_key": self._read})
        if reply == {"pending": True}:
            return None
        payload = reply.get("payload")
        if not isinstance(payload, str) or len(payload) > 4096 or set(reply) != {"payload"}:
            raise PairingError("등록 중계의 응답 형식을 확인할 수 없습니다.")
        return payload

    def close_remote(self):
        _post(f"{self.relay}/v1/rooms/{self.room}/close", {"read_key": self._read})

    def device_code(self, payload, otp):
        """서명과 시간 코드를 확인한다. 호출자는 저장 직전 세션을 다시 조회하고 쓰기 잠금을 유지한다."""
        self._check()
        try:
            raw = _unb64(payload, 4096)
            aes = AESGCM(_sha("dg-pair-data-v1:" + self.secret).digest())
            packet = json.loads(aes.decrypt(raw[:12], raw[12:], ("DGPAIR1:" + self.room).encode()).decode())
            if not isinstance(packet, dict) or set(packet) != {"code", "totp_key", "proof"}:
                raise ValueError
            code, seed = packet["code"], packet["totp_key"]
            if not isinstance(code, str) or not re.fullmatch(r"DGD1\.[A-Za-z0-9_-]{200}", code):
                raise ValueError
            _, _, sign_key = P.parse_device_code(code)
            key = _unb64(seed, 27)
            if len(key) != 20:
                raise ValueError
            message = f"DGPAIR1-proof:{self.room}:{code}:{seed}".encode("ascii")
            P.verify_raw(P.pub_from_raw(sign_key), _unb64(packet["proof"], 86), message)
        except (InvalidTag, P.ProtocolError, ValueError, RecursionError):
            self._failed += 1
            raise PairingError("휴대폰 등록 자료를 확인할 수 없습니다. 새 QR을 열고 다시 읽어 주세요.") from None
        step = int(time.time()) // 30
        if not isinstance(otp, str) or not re.fullmatch(r"[0-9]{6}", otp) or not any(
                hmac.compare_digest(otp, totp_number(key, slot)) for slot in range(max(0, step - 1), step + 2)):
            self._failed += 1
            if self._failed >= ATTEMPTS:
                raise PairingError(_LOCKED)
            raise PairingError(f"휴대폰에 표시된 6자리 인증번호를 확인해 주세요. 남은 시도 {ATTEMPTS - self._failed}회.")
        return code


class Sessions:
    """관리자 로그인 세션당 등록 QR 하나. 서버가 DB 쓰기 잠금 안에서 접근한다."""
    def __init__(self):
        self._sessions = {}

    def start(self, owner, relay):
        address = relay_url(relay)
        if not address or not owner:
            raise PairingError("관리자로 로그인하고 간편 등록 중계 주소를 설정해 주세요.")
        for key, session in list(self._sessions.items()):
            if session.deadline <= time.monotonic() or key == owner:
                self.drop(key, session.id)
        if len(self._sessions) >= 64:
            raise PairingError("사용 중인 등록 세션이 많습니다. 잠시 후 다시 시도해 주세요.")
        self._sessions[owner] = Pairing(address)
        return self._sessions[owner]

    def get(self, owner, session_id, relay):
        session = self._sessions.get(owner)
        if not session or session.id != session_id or session.relay != relay_url(relay):
            raise PairingError(_STALE)
        session._check()
        return session

    def drop(self, owner, session_id):
        session = self._sessions.get(owner)
        if session and session.id == session_id:
            session._closed = True
            del self._sessions[owner]
