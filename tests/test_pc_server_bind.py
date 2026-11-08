"""공통 PC 서버의 포트 소유 회귀. 사용자 DB 없이 임시 loopback 소켓만 쓴다."""
from __future__ import annotations

import http.server
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from dgpc.server import DesktopServer, Server  # noqa: E402


class ServerBindingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
