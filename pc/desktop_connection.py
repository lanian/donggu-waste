"""Immutable launcher connection settings and bounded, unauthenticated probes.

Importing this module never opens a database, key store or business server.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import datetime as dt
import http.client
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from dgpc import __version__


@dataclass(frozen=True)
class ConnectionProfile:
    mode: str = "local"
    port: int = 8765
    host: str = "127.0.0.1"
    hostname: str = "127.0.0.1"
    cert: str = ""
    key: str = ""
    allow: str = ""

    @classmethod
    def from_dict(cls, values):
        if not isinstance(values, dict):
            raise ValueError("연결 설정 형식이 올바르지 않습니다.")
        known = {f.name: values[f.name] for f in fields(cls) if f.name in values}
        port = known.get("port", 8765)
        if isinstance(port, str) and re.fullmatch(r"[0-9]{1,5}", port.strip()):
            known["port"] = int(port.strip())
        for name in ("mode", "host", "hostname", "cert", "key", "allow"):
            if name in known and not isinstance(known[name], str):
                raise ValueError("연결 설정 형식이 올바르지 않습니다.")
            if name in known:
                known[name] = known[name].strip()
        if known.get("mode", "local") == "local":
            known.update(host="127.0.0.1", hostname="127.0.0.1", cert="", key="", allow="")
        return cls(**known)

    def to_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @property
    def url(self):
        if self.mode == "local":
            return f"http://127.0.0.1:{self.port}/"
        return f"https://{self.hostname}:{self.port}/"

    @property
    def advertised_url(self):
        return normalize_url(self.url)


def _configuration_directory():
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DongguWaste"


def _read_configuration(directory=None, *, strict=False):
    try:
        value = json.loads(((directory or _configuration_directory()) / "desktop.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid configuration shape")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        if strict:
            raise ValueError("저장된 실행 앱 설정을 읽지 못했습니다. 연결 설정을 확인한 뒤 다시 저장하세요.") from None
        return {}


def update_configuration(changes, *, directory=None):
    """Atomically retain state, connection settings and unknown future fields."""
    directory = directory or _configuration_directory()
    directory.mkdir(parents=True, exist_ok=True)
    current = _read_configuration(directory)
    for name, value in changes.items():
        if name == "connection" and isinstance(current.get(name), dict):
            current[name] = {**current[name], **value}
        else:
            current[name] = value
    temporary = directory / ("desktop-" + secrets.token_hex(6) + ".tmp")
    try:
        temporary.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        temporary.replace(directory / "desktop.json")
    finally:
        temporary.unlink(missing_ok=True)


def load_profile():
    values = _read_configuration(strict=True).get("connection", {})
    return ConnectionProfile.from_dict(values)


def resolve_profile(options):
    """Resolve only; TLS preflight belongs to start/save, before key access."""
    values = load_profile().to_dict()
    supplied = options if isinstance(options, dict) else vars(options)
    if supplied.get("mode") == "local":
        values = ConnectionProfile(port=values["port"]).to_dict()
    for field in fields(ConnectionProfile):
        if supplied.get(field.name) is not None:
            values[field.name] = supplied[field.name]
    return ConnectionProfile.from_dict(values)


def save_profile(profile):
    validate_profile(profile)
    update_configuration({"connection": profile.to_dict()})


def _hostname(value):
    if not isinstance(value, str) or not value or len(value) > 253 or any(c.isspace() for c in value):
        raise ValueError("접속 이름은 인증서에 등록된 DNS 이름 또는 IPv4 주소로 적어 주세요.")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value) or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
            raise ValueError("접속 이름은 인증서에 등록된 DNS 이름 또는 IPv4 주소로 적어 주세요.")
        return value.lower()
    if address.is_unspecified or address.version != 4:
        raise ValueError("접속 이름에는 0.0.0.0 대신 실제 DNS 이름 또는 IPv4 주소를 적어 주세요.")
    return str(address)


def validate_profile(profile):
    if not isinstance(profile, ConnectionProfile) or profile.mode not in {"local", "lan"}:
        raise ValueError("연결 방식은 이 PC 전용 또는 같은 망 HTTPS를 선택하세요.")
    if type(profile.port) is not int or not 1 <= profile.port <= 65535:
        raise ValueError("포트는 1~65535 사이의 정수로 적어 주세요.")
    if profile.mode == "local":
        if profile.host != "127.0.0.1" or profile.hostname != "127.0.0.1" or any((profile.cert, profile.key, profile.allow)):
            raise ValueError("이 PC 전용 연결은 127.0.0.1 주소를 사용합니다.")
        return
    try:
        ipaddress.IPv4Address(profile.host)
    except (ValueError, TypeError):
        raise ValueError("서버 바인드 주소는 IPv4 주소로 적어 주세요.") from None
    hostname = _hostname(profile.hostname)
    if not profile.cert or not profile.key or not profile.allow:
        raise ValueError("같은 망 연결에는 HTTPS 인증서·개인키 파일과 허용 IP 대역이 모두 필요합니다.")
    # Reuse the existing CIDR/file policy without starting its server or database.
    from dgpc.server import lan_config
    try:
        lan = lan_config(profile.cert, profile.key, profile.allow, hostname)
        for version in (4, 6):
            networks = (network for network in lan["nets"] if network.version == version)
            if any(network.prefixlen == 0 for network in ipaddress.collapse_addresses(networks)):
                raise ValueError("combined networks allow every address")
    except (ValueError, OSError):
        raise ValueError("HTTPS 파일 또는 허용 IP 대역을 확인하세요. 모든 주소 허용은 사용할 수 없습니다.") from None
    try:
        from cryptography import x509
        certificate_bytes = Path(profile.cert).read_bytes()
        if len(certificate_bytes) > 1024 * 1024:
            raise ValueError("certificate too large")
        certificate = x509.load_pem_x509_certificate(certificate_bytes)
        moment = dt.datetime.now(dt.timezone.utc)
        if not certificate.not_valid_before_utc <= moment <= certificate.not_valid_after_utc:
            raise ValueError("certificate time")
        sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            def matches(pattern):
                pattern = pattern.lower()
                return pattern == hostname or (pattern.startswith("*.") and hostname.count(".") == pattern.count(".") and hostname.endswith(pattern[1:]))
            matched = any(matches(name) for name in sans.get_values_for_type(x509.DNSName))
        else:
            matched = address in sans.get_values_for_type(x509.IPAddress)
        if not matched:
            raise ValueError("certificate name")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(profile.cert, profile.key, password=lambda: "")
    except (OSError, ValueError, ssl.SSLError, x509.ExtensionNotFound):
        raise ValueError("HTTPS 인증서의 유효기간·접속 이름·개인키 쌍을 확인하세요. 암호화된 개인키 파일은 지원하지 않습니다.") from None


def profile_arguments(profile):
    arguments = ["--mode", profile.mode, "--port", str(profile.port)]
    if profile.mode == "lan":
        for name in ("host", "hostname", "cert", "key", "allow"):
            arguments += ["--" + name, getattr(profile, name)]
    return arguments


def normalize_url(value):
    invalid = ValueError("접속 주소는 정보가 붙지 않은 HTTPS 기본 주소로 적어 주세요. HTTP는 이 PC 주소만 허용합니다.")
    if not isinstance(value, str) or not value or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise invalid
    try:
        parsed = urllib.parse.urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
            raise invalid
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or "?" in value or "#" in value:
            raise invalid
        if port is not None and not 1 <= port <= 65535:
            raise invalid
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            _hostname(host)
            loopback = host.lower() == "localhost"
        else:
            if address.is_unspecified:
                raise invalid
            loopback = address.is_loopback
        if parsed.scheme == "http" and not loopback:
            raise invalid
        if ":" in host:
            host = "[" + host + "]"
        return f"{parsed.scheme}://{host.lower()}:{port or (443 if parsed.scheme == 'https' else 80)}/"
    except (ValueError, TypeError):
        raise invalid from None


_MESSAGES = {
    "ready": "이 기기에서 서버 정체·버전·업무 화면 파일을 확인했습니다. 다른 기기의 접속 가능 여부는 별도 확인하세요.",
    "invalid_url": "접속 주소 형식을 확인하세요. HTTPS 기본 주소 또는 이 PC의 HTTP 주소만 허용합니다.",
    "tls_error": "인증서 신뢰·유효기간·접속 이름을 확인하세요. 인증서 검증을 건너뛰지 않았습니다.",
    "access_denied": "접속이 허용되지 않았습니다. 허용 IP 대역과 접속 이름 설정을 확인하세요.",
    "unreachable": "서버에 연결하지 못했거나 확인 시간이 초과되었습니다. 주소·서버 실행·망 연결을 확인하세요.",
    "wrong_service": "해당 주소에서 동구 PC 업무 서버를 확인하지 못했습니다.",
    "version_mismatch": "서버 버전이 이 실행 앱과 다릅니다. 서버와 접속 점검 앱의 버전을 맞춰 주세요.",
    "assets_unavailable": "서버는 응답하지만 업무 화면 파일을 확인하지 못했습니다.",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


class _Budget:
    """One deadline closes sockets even during slow headers/body/TLS handshakes."""
    def __init__(self, seconds):
        self.until = time.monotonic() + seconds
        self.sockets = []
        self.lock = threading.Lock()
        self.expired = False
        self.timer = threading.Timer(seconds, self.close)
        self.timer.daemon = True
        self.timer.start()

    def remaining(self):
        remaining = self.until - time.monotonic()
        if remaining <= 0 or self.expired:
            raise TimeoutError("probe deadline")
        return min(2.0, remaining)

    def track(self, sock):
        with self.lock:
            if self.expired:
                sock.close()
                raise TimeoutError("probe deadline")
            self.sockets.append(sock)

    def connect(self, address, timeout=None, source_address=None, **_):
        host, port = address
        remaining = self.remaining()
        results = queue.Queue(maxsize=1)
        def resolve():
            try:
                results.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
            except OSError as error:
                results.put(error)
        threading.Thread(target=resolve, daemon=True, name="connection-check-dns").start()
        try:
            addresses = results.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("DNS timeout") from None
        if isinstance(addresses, OSError):
            raise addresses
        last_error = OSError("unreachable")
        for family, kind, protocol, _, endpoint in addresses:
            sock = socket.socket(family, kind, protocol)
            self.track(sock)
            try:
                sock.settimeout(self.remaining())
                if source_address:
                    sock.bind(source_address)
                sock.connect(endpoint)
                self.remaining()
                return sock
            except OSError as error:
                sock.close()
                last_error = error
        raise last_error

    def close(self):
        with self.lock:
            self.expired = True
            sockets, self.sockets = self.sockets, []
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def finish(self):
        self.timer.cancel()
        self.close()


def _opener(budget, context):
    class HTTPConnection(http.client.HTTPConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = budget.connect

    class HTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            self.sock = budget.connect((self.host, self.port))
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host, do_handshake_on_connect=False)
            budget.track(self.sock)
            self.sock.settimeout(budget.remaining())
            self.sock.do_handshake()
            budget.remaining()

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(HTTPConnection, request)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(HTTPSConnection, request, context=context)

    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(), HTTPHandler(), HTTPSHandler())


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            source = dict(attrs).get("src")
            if source is not None:
                self.sources.append(source.strip())
                if len(self.sources) > 16:
                    raise ValueError("too many entry scripts")


def probe_url(url, *, ssl_context=None):
    """GET-only root probe, <=12s total, with normal verified TLS and fixed output."""
    if ssl_context is not None and (ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname):
        raise ValueError("인증서 신뢰와 접속 이름 검증을 모두 사용해야 합니다.")
    def result(code, safe=""):
        return {"code": code, "message": _MESSAGES[code], "ok": code == "ready", "version": __version__ if code == "ready" else "",
                "url": safe, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "scope": "this_device"}
    try:
        safe = normalize_url(url)
    except ValueError:
        return result("invalid_url")
    context = ssl_context or ssl.create_default_context()
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    budget = _Budget(12)
    opener = _opener(budget, context)
    stage = "identity"
    try:
        def fetch(target, limit):
            request = urllib.request.Request(target, headers={"X-DG-CSRF": "1"})
            with opener.open(request, timeout=budget.remaining()) as response:
                data = bytearray()
                while True:
                    budget.remaining()
                    part = response.read1(min(8192, limit + 1 - len(data)))
                    budget.remaining()
                    data.extend(part)
                    if len(data) > limit:
                        raise ValueError("response too large")
                    if not part:
                        return response, bytes(data)
        response, body = fetch(safe + "api/state", 64 * 1024)
        if response.status != 200 or response.headers.get("Server", "").strip() != "dgpc":
            return result("wrong_service", safe)
        state = json.loads(body)
        if not isinstance(state, dict) or not all(isinstance(state.get(key), bool) for key in ("setup_needed", "logged_in")):
            return result("wrong_service", safe)
        if response.headers.get("X-DG-Version") != __version__:
            return result("version_mismatch", safe)
        stage = "assets"
        _, body = fetch(safe, 1024 * 1024)
        html = body.decode("utf-8")
        if "<title>동구 대형폐기물 PC 업무</title>" not in html:
            return result("assets_unavailable", safe)
        scripts = _Scripts()
        scripts.feed(html)
        if not scripts.sources or any(not source for source in scripts.sources):
            return result("assets_unavailable", safe)
        base = urllib.parse.urlsplit(safe)
        resources = [urllib.parse.urljoin(safe, source) for source in scripts.sources + ["pc.js", "core.js"]]
        for target in dict.fromkeys(resources):
            parsed = urllib.parse.urlsplit(target)
            if (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)) != (base.scheme, base.hostname, base.port) or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                return result("assets_unavailable", safe)
            response, content = fetch(target, 1024 * 1024)
            if response.status != 200 or response.headers.get_content_type() not in {"text/javascript", "application/javascript", "text/ecmascript", "application/ecmascript"} or not content.strip():
                return result("assets_unavailable", safe)
        return result("ready", safe)
    except urllib.error.HTTPError as error:
        return result("access_denied" if error.code in (401, 403) else "assets_unavailable" if stage == "assets" else "wrong_service", safe)
    except (ssl.SSLError, ssl.CertificateError):
        return result("tls_error", safe)
    except urllib.error.URLError as error:
        return result("tls_error" if isinstance(error.reason, ssl.SSLError) else "unreachable", safe)
    except (OSError, TimeoutError, http.client.HTTPException):
        return result("unreachable", safe)
    except (ValueError, UnicodeError, AttributeError):
        return result("assets_unavailable" if stage == "assets" else "wrong_service", safe)
    finally:
        budget.finish()


def probe_connection(profile):
    return probe_url(profile.url)
