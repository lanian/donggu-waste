"""Launcher profiles and HTTPS probes use only temporary state and local fixtures."""
from __future__ import annotations

import datetime as dt
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
PASSED = 0


def check(condition, message):
    global PASSED
    assert condition, message
    PASSED += 1
    print("PASS:", message, flush=True)


def rejects(call, message):
    try:
        call()
    except ValueError:
        check(True, message)
    else:
        raise AssertionError(message)


def certificates(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fictitious launcher test CA")])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
          .not_valid_after(now + dt.timedelta(days=5)).add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                      data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                      encipher_only=None, decipher_only=None), True)
          .sign(ca_key, hashes.SHA256()))
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths = {}
    for kind, dns, start, end in (
        ("valid", "localhost", now - dt.timedelta(hours=1), now + dt.timedelta(days=1)),
        ("expired", "localhost", now - dt.timedelta(days=2), now - dt.timedelta(days=1)),
        ("future", "localhost", now + dt.timedelta(days=1), now + dt.timedelta(days=2)),
        ("wrongname", "wrong.invalid", now - dt.timedelta(hours=1), now + dt.timedelta(days=1)),
    ):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns)])
        sans = [x509.DNSName(dns)]
        if kind != "wrongname":
            sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False)
                .add_extension(x509.SubjectAlternativeName(sans), False).sign(ca_key, hashes.SHA256()))
        cert_path, key_path = directory / (kind + ".pem"), directory / (kind + ".key")
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        paths[kind] = (str(cert_path), str(key_path))
    return paths


class Handler(BaseHTTPRequestHandler):
    server_version = "dgpc"
    sys_version = ""

    def do_GET(self):
        self.server.requests.append((self.command, self.path))
        status, mime, body, headers = self.server.assets.get(self.path, (404, "text/plain", b"missing", {}))
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        from socketserver import TCPServer
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


@contextmanager
def fixture(cert=None, key=None, *, handler=Handler):
    from dgpc import __version__
    with FixtureServer(("127.0.0.1", 0), handler) as server:
        server.assets = {
            "/api/state": (200, "application/json", b'{"setup_needed":true,"logged_in":false}', {"X-DG-Version": __version__}),
            "/": (200, "text/html", '<title>동구 대형폐기물 PC 업무</title><script src="start.js"></script>'.encode(), {}),
            "/start.js": (200, "text/javascript", b"import('./pc.js')", {}),
            "/pc.js": (200, "text/javascript", b"import './core.js'", {}),
            "/core.js": (200, "text/javascript", b"export const ready=true", {}),
        }
        server.requests = []
        if cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, f"{'https' if cert else 'http'}://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()
            thread.join(timeout=5)


class SlowHeaders(Handler):
    def do_GET(self):
        try:
            self.request.sendall(b"HTTP/1.0 200 OK\r\nX-Slow: ")
            for _ in range(60):
                self.request.sendall(b"x")
                time.sleep(.025)
        except OSError:
            pass


class SlowBody(Handler):
    def do_GET(self):
        try:
            self.request.sendall(b"HTTP/1.0 200 OK\r\nServer: dgpc\r\nContent-Type: application/json\r\nContent-Length: 256\r\n\r\n")
            for _ in range(60):
                self.request.sendall(b" ")
                time.sleep(.025)
        except OSError:
            pass


def profile_cases(api, directory, certs):
    from desktop_runtime import DesktopController, load_state_choice, save_state_choice, worker_command
    profile = api.ConnectionProfile()
    api.validate_profile(profile)
    check(profile.url == "http://127.0.0.1:8765/" and profile.advertised_url == profile.url, "local defaults preserve loopback URL")
    check(DesktopController(directory / "unused", 8766).url == "http://127.0.0.1:8766/", "old constructor preserves explicit8766")
    rejects(lambda: api.validate_profile(api.ConnectionProfile(port=0)), "invalid port rejected")
    rejects(lambda: api.validate_profile(api.ConnectionProfile(mode="internet")), "unknown mode rejected")
    rejects(lambda: api.validate_profile(api.ConnectionProfile(host="0.0.0.0")), "local bind cannot silently become external")
    cert, key = certs["valid"]
    values = dict(mode="lan", port=19876, host="127.0.0.1", hostname="localhost", cert=cert, key=key, allow="127.0.0.1/32")
    lan = api.ConnectionProfile.from_dict(values)
    api.validate_profile(lan)
    check(lan.url == "https://localhost:19876/" and lan.to_dict() == values, "immutable LAN profile keeps complete configuration")
    for changes in ({"cert": ""}, {"allow": "0.0.0.0/0"}, {"allow": "::/0"},
                    {"allow": "0.0.0.0/1,128.0.0.0/1"}, {"allow": "::/1,8000::/1"}, {"allow": "bad"},
                    {"hostname": "0.0.0.0"}, {"hostname": "user@host"}, {"host": "example.com"},
                    {"key": certs["wrongname"][1]}, {"cert": certs["expired"][0], "key": certs["expired"][1]},
                    {"cert": certs["future"][0], "key": certs["future"][1]},
                    {"cert": certs["wrongname"][0], "key": certs["wrongname"][1]}):
        rejects(lambda changes=changes: api.validate_profile(api.ConnectionProfile.from_dict({**values, **changes})), "invalid LAN preflight: " + ','.join(changes))
    config = directory / "profile-home" / "DongguWaste" / "desktop.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"state": str(directory / "remembered"), "future": {"keep": [1, 2]}}), encoding="utf-8")
    with patch.dict(os.environ, {"LOCALAPPDATA": str(config.parent.parent)}):
        check(api.load_profile() == profile, "legacy state-only configuration retains local defaults")
        config.write_text("{broken", encoding="utf-8")
        rejects(api.load_profile, "damaged settings are reported instead of silently starting a fallback server")
        config.write_text("[]", encoding="utf-8")
        rejects(api.load_profile, "non-object settings are reported for repair")
        with patch.dict(os.environ, {"DG_STATE": ""}):
            check(load_state_choice() is not None, "legacy state chooser tolerates non-object settings")
        config.write_text(json.dumps({"state": str(directory / "remembered"), "future": {"keep": [1, 2]}}), encoding="utf-8")
        api.save_profile(lan)
        save_state_choice(directory / "new-state")
        saved = json.loads(config.read_text(encoding="utf-8"))
        check(saved["future"] == {"keep": [1, 2]} and saved["connection"] == values and saved["state"] == str((directory / "new-state").resolve()), "both writers preserve state/profile/unknown configuration")
        check(api.resolve_profile(SimpleNamespace(port=8766)).port == 8766, "explicit8766 overrides saved port")
        local = api.resolve_profile({"mode": "local", "port": 8766})
        check(local.mode == "local" and local.host == "127.0.0.1" and not local.cert, "explicit local profile defeats savedLAN")
        check(api.load_profile() == lan, "resolving options does not write configuration")
    args = api.profile_arguments(lan)
    check(all(flag in args for flag in ("--mode", "--port", "--host", "--hostname", "--cert", "--key", "--allow")), "profile arguments propagate every LAN setting")
    check(api.profile_arguments(local) == ["--mode", "local", "--port", "8766"], "local arguments explicitly override savedLAN")
    command = worker_command(directory / "worker-state", 8765, connection=lan)
    check(command[command.index("--port") + 1] == "19876" and command.count("--port") == 1 and "--cert" in command, "child command uses complete selected profile once")
    invalid = api.ConnectionProfile.from_dict({**values, "cert": ""})
    controller = DesktopController(directory / "never-created", connection=invalid)
    try:
        controller.start(lambda new: (_ for _ in ()).throw(AssertionError("invalid LAN requested a key")))
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("invalid LAN started")
    check(not controller.state_dir.exists() and not controller.owned, "LAN preflight fails before credentials or state initialization")
    from desktop_runtime import child_main
    with patch("desktop_runtime._stdin_pipe", side_effect=AssertionError("invalid LAN read a key pipe")):
        try:
            child_main(directory / "invalid-child", 8765, connection=invalid)
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid child reached key/DB initialization")
    check(not (directory / "invalid-child").exists(), "child independently validates TLS before reading keys or initializing state")


def probe_cases(api, directory, certs):
    expected = {"code", "message", "ok", "version", "url", "checked_at", "scope"}
    for url in ("http://example.com/", "https://user:secret@example.com/", "https://example.com/private", "https://example.com/?key=secret",
                "https://example.com/#secret", "https://0.0.0.0/", "file:///x", "https://example.com/\n", "https://example.com:0/", "https://*.example.com/"):
        result = api.probe_url(url)
        check(result["code"] == "invalid_url" and result["url"] == "" and set(result) == expected and "secret" not in json.dumps(result), "unsafe URL rejected without echo or network")
    check(api.normalize_url("http://127.0.0.1:8766") == "http://127.0.0.1:8766/", "safe root URL normalized")
    with fixture() as (server, url):
        result = api.probe_url(url)
        check(result["code"] == "ready" and result["ok"] and result["scope"] == "this_device" and set(result) == expected, "local probe recognizes server and limits observing-device scope")
        original = dict(server.assets)
        for response, code in (((403, "text/plain", b"denied", {}), "access_denied"),
                               ((200, "application/json", b"{}", {}), "wrong_service"),
                               ((200, "application/json", b'{"setup_needed":true,"logged_in":false}', {"X-DG-Version": "0.14.20"}), "version_mismatch")):
            server.assets = {**original, "/api/state": response}
            check(api.probe_url(url)["code"] == code, "probe distinguishes " + code)
        server.assets = {**original, "/start.js": (302, "text/plain", b"", {"Location": "https://outside.invalid/secret"})}
        check(api.probe_url(url)["code"] == "assets_unavailable", "asset redirects are never followed")
        server.assets = {**original, "/": (200, "text/html", '<title>동구 대형폐기물 PC 업무</title><script src="https://outside.invalid/a.js"></script>'.encode(), {})}
        check(api.probe_url(url)["code"] == "assets_unavailable" and all(method == "GET" for method, _ in server.requests), "cross-origin assets rejected; checker uses GET only")
        server.assets = {**original, "/api/state": (200, "application/json", b" " * (64 * 1024 + 1), {})}
        check(api.probe_url(url)["code"] == "wrong_service", "API response is bounded to64KB")
        server.assets = {**original, "/": (200, "text/html", ('<title>동구 대형폐기물 PC 업무</title>' + '<script src="start.js"></script>' * 17).encode(), {})}
        check(api.probe_url(url)["code"] == "assets_unavailable", "entry script count is bounded to16")
    context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
    with fixture(*certs["valid"]) as (_, url):
        check(api.probe_url(url)["code"] == "tls_error", "untrusted HTTPS certificate rejected")
        check(api.probe_url(url, ssl_context=context)["code"] == "ready", "test-only trusted CA retains hostname verification")
        insecure = ssl._create_unverified_context()
        rejects(lambda: api.probe_url(url, ssl_context=insecure), "TLS verification bypass cannot be injected")
    for kind in ("expired", "wrongname"):
        with fixture(*certs[kind]) as (_, url):
            check(api.probe_url(url, ssl_context=context)["code"] == "tls_error", kind + " HTTPS certificate rejected")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    check(api.probe_url(f"http://127.0.0.1:{port}/")["code"] == "unreachable", "closed temporary port has a distinct result")
    marker = directory / "import-side-effects"
    code = "import sys;sys.path.insert(0,sys.argv[1]);from desktop_connection import probe_url;assert probe_url('file:///never')['code']=='invalid_url';assert not any(x in sys.modules for x in ('dgpc.store','dgpc.server','keyring'));from pathlib import Path;assert not Path(sys.argv[2]).exists()"
    result = subprocess.run([sys.executable, "-c", code, str(ROOT / "pc"), str(marker)],
                            env={**os.environ, "LOCALAPPDATA": str(marker), "DG_STATE": str(marker)}, capture_output=True, timeout=10)
    check(result.returncode == 0, "checker import and URL validation open no keyring/server/DB/configuration")
    budget_class = api._Budget
    for slow in (SlowHeaders, SlowBody):
        with fixture(handler=slow) as (_, url), patch.object(api, "_Budget", side_effect=lambda seconds: budget_class(.25)):
            before = time.monotonic()
            result = api.probe_url(url)
            check(result["code"] == "unreachable" and time.monotonic() - before < 1.5,
                  slow.__name__ + " is interrupted by whole-probe deadline, not refreshed by trickle traffic")


def actual_tls_child(api, directory, certs):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    cert, key = certs["valid"]
    profile = api.ConnectionProfile(mode="lan", port=port, host="127.0.0.1", hostname="127.0.0.1", cert=cert, key=key, allow="127.0.0.1/32")
    state = directory / "actual-tls-state"
    code = "import sys,json;sys.path.insert(0,sys.argv[1]);from desktop_runtime import child_main;from desktop_connection import ConnectionProfile;raise SystemExit(child_main(sys.argv[2],int(sys.argv[3]),connection=ConnectionProfile.from_dict(json.loads(sys.argv[4]))))"
    env = {**os.environ, "DG_STATE": str(state), "DG_OUT": str(directory / "out"), "DG_NO_SCHEDULER": "1", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "PYTHONUTF8": "1"}
    env.pop("DG_DB_KEY", None)
    process = subprocess.Popen([sys.executable, "-c", code, str(ROOT / "pc"), str(state), str(port), json.dumps(profile.to_dict())],
                               cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
    try:
        process.stdin.write((json.dumps({"key": "fictitious-tls-test-db-key-123", "launch_id": "a" * 32}) + "\n").encode()); process.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, process.stdout.read().decode(errors="replace")
            result = api.probe_url(profile.url, ssl_context=context)
            if result["ok"]:
                break
            time.sleep(.1)
        else:
            raise AssertionError("temporary TLS child did not become ready: " + result["code"])
        check((state / "logs" / ("desktop-ready-" + "a" * 32 + ".json")).exists(), "actual HTTPS child writes its own readiness nonce")
        check((state / "pc.db").read_bytes()[:16] != b"SQLite format 3\x00", "actual HTTPS child creates only encrypted fixture DB")
        from desktop_runtime import DesktopController
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(directory / "ca.pem")}):
            other = DesktopController(directory / "must-not-create", connection=profile)
            check(other.start(lambda _: (_ for _ in ()).throw(AssertionError("existing TLS server requested key"))) == "existing" and not other.owned,
                  "existing HTTPS server is reused without ownership/key/state")
            other.stop()
            check(process.poll() is None and not other.state_dir.exists(), "stopping non-owned HTTPS controller leaves fixture server/state intact")
    finally:
        if process.poll() is None:
            process.stdin.write(b'{"command":"stop"}\n'); process.stdin.flush()
        process.stdin.close()
        check(process.wait(timeout=20) == 0, "actual HTTPS child stops normally through control pipe")
    from desktop_runtime import DesktopController
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    controlled = DesktopController(directory / "controller-tls-state", connection=replace(profile, port=port))
    with patch.dict(os.environ, {"LOCALAPPDATA": str(directory / "controller-home"), "DG_NO_SCHEDULER": "1",
                                "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "SSL_CERT_FILE": str(directory / "ca.pem")}):
        try:
            phases = []
            check(controlled.start(lambda _: "fictitious-controller-tls-key-123", on_progress=phases.append) == "ready",
                  "controller propagates complete profile through actual desktop child parser and TLS server")
            check(phases == ["checking", "unlocking", "starting", "waiting", "ready"], "TLS startup retains existing progress/control-pipe contract")
            child = controlled.child
        finally:
            controlled.stop()
        check(child.returncode == 0 and not controlled.owned, "owned HTTPS controller stops only its own child normally")


def local_startup_deadlines(api, directory):
    from desktop_runtime import DesktopController, DesktopError
    budget_class = api._Budget
    for slow in (SlowHeaders, SlowBody):
        with fixture(handler=slow) as (server, _), patch.object(api, "_Budget", side_effect=lambda seconds: budget_class(.25)):
            selected = directory / ("must-not-create-" + slow.__name__)
            controller = DesktopController(selected, server.server_port)
            before = time.monotonic()
            try:
                controller.start(lambda _: (_ for _ in ()).throw(AssertionError("occupied slow server requested key")))
            except DesktopError:
                pass
            else:
                raise AssertionError("slow wrong service was accepted")
            check(time.monotonic() - before < 1.3 and controller.status == "error" and not controller.owned
                  and not selected.exists(), slow.__name__ + " cannot stall local startup or request keys/create state")


def main():
    check(importlib.util.find_spec("desktop_connection") is not None, "connection profiles and safe probe module exist")
    import desktop_connection as api
    with tempfile.TemporaryDirectory(prefix="dg-desktop-connection-") as temporary:
        directory = Path(temporary)
        certs = certificates(directory)
        profile_cases(api, directory, certs)
        probe_cases(api, directory, certs)
        local_startup_deadlines(api, directory)
        actual_tls_child(api, directory, certs)
    print(f"Desktop connection: {PASSED} checks passed; isolated fixtures only, other-device reachability unconfirmed")


if __name__ == "__main__":
    main()
