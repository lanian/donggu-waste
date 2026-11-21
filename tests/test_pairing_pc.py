"""PC 일회용 등록 세션의 소유자, 유효 시간, 시도 제한 및 암호문 연결."""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import dgproto as P
from pairing_relay import RelayHTTPServer, Rooms


def b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class PairingPC(unittest.TestCase):
    def setUp(self):
        import device_pairing as pairing
        self.p = pairing
        self.sessions = pairing.Sessions()
        self.url = "https://example.org/pair"
        self.room = self.sessions.start("admin-session", self.url)

    def test_checks_owner_expiry_rotation_and_relay_address(self):
        for owner, url in (("other", self.url), ("admin-session", "https://other.invalid")):
            with self.assertRaises(ValueError):
                self.sessions.get(owner, self.room.id, url)
        with patch.object(self.p.time, "monotonic", return_value=self.room.deadline + 1):
            with self.assertRaises(ValueError):
                self.sessions.get("admin-session", self.room.id, self.url)
        fresh = self.sessions.start("admin-session", self.url)
        self.sessions.start("admin-session", self.url)
        with self.assertRaises(ValueError):
            self.sessions.get("admin-session", fresh.id, self.url)
        self.assertEqual(self.p.relay_url("https://example.org/pair/"), self.url)
        for url in ("http://example.org", "https://a:b@example.org", "https://example.org/?key=1",
                    "https://example.org/#x", "https://example.org\\@127.0.0.1",
                    "https://example.org/../place", "https://example.org/%2e%2e",
                    "https://example.org/\npath", "http://localhost", "http://127.1.2.3", "http://[::1]",
                    "https://a.example/" + "z"*300):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.p.relay_url(url)
        self.assertEqual(self.p.relay_url(""), "")
        self.assertEqual(self.p.relay_url("http://127.0.0.1:8181"), "http://127.0.0.1:8181")

    def packet(self, room, *, other_sign=False, other_room=False, unsigned=False):
        device, key, signer = uuid.uuid4(), P.gen_key(), P.gen_key()
        record = device.bytes + P.pub_raw(key) + P.pub_raw(signer)
        code = "DGD1." + b64(record + hashlib.sha256(record).digest()[:4])
        seed = b64(b"12345678901234567890")  # RFC 6238 SHA-1 시험키
        message = ("DGPAIR1-proof:" + ("0"*64 if other_room else room.room) + ":" + code + ":" + seed).encode()
        signature = P.sign_raw(P.gen_key() if other_sign else signer, message)
        body = {"code": code, "totp_key": seed}
        if not unsigned:
            body["proof"] = b64(signature)
        nonce = os.urandom(12)
        key = hashlib.sha256(("dg-pair-data-v1:" + room.secret).encode()).digest()
        ciphertext = AESGCM(key).encrypt(nonce, json.dumps(body).encode(), ("DGPAIR1:" + room.room).encode())
        return b64(nonce + ciphertext), code

    def test_only_signed_device_and_its_time_code_are_accepted(self):
        payload, code = self.packet(self.room)
        for now, number in [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"), (2000000000, "279037")]:
            for skew in (-30, 0, 30):
                with self.subTest(now=now, skew=skew), patch.object(self.p.time, "time", return_value=now + skew):
                    self.assertEqual(self.room.device_code(payload, number), code)
        with patch.object(self.p.time, "time", return_value=59):
            for bad in (payload[:-6], self.packet(self.room, other_sign=True)[0], self.packet(self.room, unsigned=True)[0],
                        self.packet(self.room, other_room=True)[0], self.packet(self.sessions.start("other", self.url))[0]):
                with self.assertRaises(ValueError):
                    self.room.device_code(bad, "287082")

    def test_five_failures_lock_valid_number_and_stale_session_cannot_validate(self):
        payload, code = self.packet(self.room)
        with patch.object(self.p.time, "time", return_value=59):
            for _ in range(5):
                with self.assertRaises(ValueError):
                    self.room.device_code(payload, "000000")
            with self.assertRaises(ValueError):
                self.room.device_code(payload, "287082")
        ticket = self.sessions.start("admin-session", self.url)
        other, _ = self.packet(ticket)
        self.sessions.drop("admin-session", ticket.id)
        with patch.object(self.p.time, "time", return_value=59), self.assertRaises(ValueError):
            ticket.device_code(other, "287082")
        expired = self.sessions.start("admin-session", self.url)
        other, _ = self.packet(expired)
        with patch.object(self.p.time, "time", return_value=59), patch.object(self.p.time, "monotonic", return_value=expired.deadline):
            with self.assertRaises(ValueError):
                expired.device_code(other, "287082")

    def test_qr_contains_registration_capability_address_and_original_expiry(self):
        with self.assertRaises(ValueError):
            self.sessions.start("other", "")
        ticket = self.sessions.start("admin-session", self.url)
        with patch.object(self.p.time, "time", return_value=1800000000), patch.object(
                self.p.time, "monotonic", return_value=ticket.deadline - 27):
            qr = ticket.qr_text("original registration QR").split(".", 1)[1]
        fields = json.loads(base64.urlsafe_b64decode(qr + "=" * (-len(qr) % 4)))
        self.assertEqual(fields, {"registration": "original registration QR", "relay": self.url,
                                  "secret": ticket.secret, "expires_at": 1800000027})

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_real_relay_transports_sealed_device_and_revokes_room(self):
        relay = RelayHTTPServer(("127.0.0.1", 0), Rooms())
        room = self.sessions.start("relay-test", self.serve(relay))
        room.create_remote()
        self.assertIsNone(room.read_payload())
        payload, code = self.packet(room)
        conn = http.client.HTTPConnection(*relay.server_address, timeout=3)
        try:
            conn.request("POST", f"/v1/rooms/{room.room}/submit", json.dumps({"payload": payload}),
                         {"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()
        with patch.object(self.p.time, "time", return_value=59):
            self.assertEqual(room.device_code(room.read_payload(), "287082"), code)
        self.sessions.drop("relay-test", room.id)
        room.close_remote()

    def test_transport_does_not_follow_redirect_or_read_large_response(self):
        calls = []
        class TestHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                calls.append(self.path)
                if self.path.startswith("/oversize/"):
                    raw = b'{"ok":true}' + b' ' * 8192
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                else:
                    self.send_response(307)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
        base = self.serve(HTTPServer(("127.0.0.1", 0), TestHandler))
        for path in ("/redirect", "/oversize"):
            with self.subTest(path=path), self.assertRaises(self.p.PairingError):
                self.sessions.start("transport-test", base + path).create_remote()
        self.assertEqual(calls, ["/redirect/v1/rooms", "/oversize/v1/rooms"])


if __name__ == "__main__":
    unittest.main()
