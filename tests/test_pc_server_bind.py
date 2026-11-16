"""공통 PC 서버의 포트 소유 회귀. 사용자 DB 없이 임시 loopback 소켓만 쓴다."""
from __future__ import annotations

import http.server
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from dgpc.server import DesktopServer, Server  # noqa: E402


class ServerBindingTests(unittest.TestCase):
    def test_server_startup_does_not_depend_on_reverse_dns(self):
        for server_class in (Server, DesktopServer):
            with self.subTest(server=server_class.__name__):
                with patch("socket.getfqdn", side_effect=AssertionError("역방향 DNS 조회에 의존하면 안 됩니다")):
                    server = server_class(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
                try:
                    self.assertEqual(server.server_name, "127.0.0.1")
                    self.assertEqual(server.server_port, server.server_address[1])
                finally:
                    server.server_close()

    @unittest.skipUnless(os.name == "nt", "Windows의 중복 listen 바인딩 회귀")
    def test_cli_server_rejects_a_second_cli_server_on_its_port(self):
        first = Server(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        second = None
        try:
            with self.assertRaises(OSError):
                second = Server(first.server_address, http.server.BaseHTTPRequestHandler)
        finally:
            if second is not None:
                second.server_close()
            first.server_close()

    @unittest.skipUnless(os.name == "nt", "Windows의 실행 앱·CLI 포트 소유 회귀")
    def test_desktop_server_rejects_a_cli_server_on_its_port(self):
        first = DesktopServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        second = None
        try:
            with self.assertRaises(OSError):
                second = Server(first.server_address, http.server.BaseHTTPRequestHandler)
        finally:
            if second is not None:
                second.server_close()
            first.server_close()

    def test_a_closed_cli_server_releases_its_port(self):
        first = Server(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        address = first.server_address
        first.server_close()
        second = Server(address, http.server.BaseHTTPRequestHandler)
        second.server_close()


class ConnectionBurstTests(unittest.TestCase):
    """브라우저는 첫 화면에서 ES 모듈 수십 개를 동시에 연다. Windows는 listen 대기열(기본 5)이 차면
    연결을 대기시키지 않고 거부해(ERR_CONNECTION_REFUSED) 화면이 '불러오는 중'에 멈출 수 있다."""

    BURST = 30

    def test_listen_queue_holds_a_module_burst(self):
        import socket
        for server_class in (Server, DesktopServer):
            with self.subTest(server=server_class.__name__):
                self.assertGreaterEqual(server_class.request_queue_size, 64, "listen 대기열이 모듈 동시 요청을 받을 만큼 커야 함")
                server = server_class(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)  # serve_forever 없이: 아무것도 accept 하지 않는 순간
                clients, refused = [], 0
                try:
                    for _ in range(self.BURST):
                        c = socket.socket(); c.settimeout(2)
                        try:
                            c.connect(server.server_address)
                        except OSError:
                            refused += 1
                        clients.append(c)
                    self.assertEqual(refused, 0, f"서버가 바쁜 순간 동시 연결 {self.BURST}개 중 {refused}개 거부")
                finally:
                    for c in clients:
                        c.close()
                    server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
