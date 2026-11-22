"""Windows의 휴대폰 USB 인터페이스에만 잠시 열리는 기기 등록 암호문 수신기."""
from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer


_NETWORKS = tuple(ipaddress.IPv4Network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
_NO_PHONE = '휴대폰을 데이터용 USB 케이블로 연결하고 USB 테더링 또는 개인용 핫스팟을 켠 뒤 다시 시도하세요.'
_PHONE_VENDORS = {'04e8', '05ac', '0bb4', '0fce', '1004', '12d1', '18d1', '22b8', '22d9', '2717', '2a70'}
_MAX_PAYLOAD = 4096
_MAX_BODY = 4352
_DETECT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred | Where-Object { -not $_.SkipAsSource })
$adapters = @(Get-CimInstance Win32_NetworkAdapter -Filter 'NetConnectionStatus = 2')
$rows = @(foreach ($a in $adapters) {
    $index = $a.InterfaceIndex
    [pscustomobject]@{
        index = $index
        pnp = [string]$a.PNPDeviceID
        description = [string]$a.Description
        service = [string]$a.ServiceName
        addresses = @($addresses | Where-Object InterfaceIndex -eq $index | ForEach-Object IPAddress)
    }
})
ConvertTo-Json -InputObject $rows -Depth 3 -Compress
"""


def _usb_candidates():
    if os.name != 'nt':
        raise ValueError('USB 직접 등록은 Windows PC에서 시작하세요.')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', _DETECT],
                                capture_output=True, timeout=8, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        records = json.loads(result.stdout.decode('utf-8-sig'))
        if not isinstance(records, list):
            raise ValueError
        return records
    except (OSError, subprocess.SubprocessError, ValueError):
        raise ValueError('Windows에서 USB 연결 상태를 확인할 수 없습니다. ' + _NO_PHONE) from None


def _private_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return any(address in network for network in _NETWORKS)


def _phone_adapter(record):
    vendor = re.match(r'USB\\VID_([0-9a-f]{4})(?:&|\\)', record['pnp'], re.I)
    if not vendor:
        return False
    description = record['description'].casefold()
    service = record['service'].casefold()
    if vendor[1].casefold() == '05ac':
        return 'apple mobile device ethernet' in description or service in ('netaapl', 'netaapl64')
    phone = vendor[1].casefold() in _PHONE_VENDORS or 'android' in description or 'tether' in description
    tether = (re.search(r'rndis|remote\s+ndis|\bncm\b|tethering', description) or
              service in ('usbrndis', 'usbrndis6', 'rndismp', 'usbncm'))
    return bool(phone and tether)


def discover_usb_ipv4() -> str:
    """일반 유·무선 LAN으로 대체하지 않고, 모호하면 사용자에게 연결을 정리하도록 안내한다."""
    candidates = set()
    other_addresses = set()
    for record in _usb_candidates():
        if _phone_adapter(record):
            candidates.update((record.get('index'), ip) for ip in record['addresses'] if _private_ipv4(ip))
        else:
            other_addresses.update(record['addresses'])
    if not candidates:
        raise ValueError(_NO_PHONE)
    if len(candidates) != 1 or next(iter(candidates))[1] in other_addresses:
        raise ValueError('USB 연결 주소를 하나로 정할 수 없습니다. 등록할 휴대폰 하나만 직접 연결하고 다시 시도하세요.')
    return candidates.pop()[1]


_SCRIPT = """
const message = document.getElementById('message');
const retry = document.getElementById('retry');
let packet = location.hash.slice(1);
history.replaceState(null, '', location.pathname);
async function deliver() {
  retry.hidden = true;
  if (!/^[A-Za-z0-9_-]{1,4096}$/.test(packet)) {
    message.textContent = '수거폰 앱으로 돌아가 PC로 보내기를 다시 눌러 주세요.';
    return;
  }
  try {
    const response = await fetch(location.pathname, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({payload: packet}), credentials: 'omit', redirect: 'error',
    });
    if (response.ok) {
      packet = '';
      message.textContent = 'PC가 등록 자료를 받았습니다. 수거폰 앱으로 돌아가 6자리 번호를 확인하세요.';
    } else if ([404, 409, 410].includes(response.status)) {
      packet = '';
      message.textContent = '등록 요청이 끝났습니다. PC에서 새 등록 QR을 열고 다시 읽어 주세요.';
    } else {
      throw Error();
    }
  } catch {
    message.textContent = 'PC에 전달하지 못했습니다. USB 연결을 확인한 뒤 다시 보내세요.';
    retry.hidden = false;
  }
}
retry.addEventListener('click', deliver);
deliver();
"""
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
_PAGE = ("""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>수거폰 직접 등록</title>
<style>body{max-width:30rem;margin:3rem auto;padding:0 1rem;font:1.1rem/1.6 system-ui;color:#142331}
button{font:inherit;padding:.4rem 1rem}</style><h1>수거폰 직접 등록</h1>
<p role="status" id="message">PC로 등록 자료를 보내고 있습니다.</p>
<button id="retry" hidden>다시 보내기</button><script>""" + _SCRIPT + '</script></html>').encode()


class _RequestReader:
    """먼저 읽는 요청 줄과 모든 헤더의 합을 제한한다. 본문은 확인한 길이만 별도로 읽는다."""
    def __init__(self, source):
        self.source = source
        self.remaining = 8192

    def readline(self, limit):
        line = self.source.readline(min(limit, self.remaining + 1))
        self.remaining -= len(line)
        if self.remaining < 0:
            raise http.client.LineTooLong('request header')
        return line

    def read(self, length):
        return self.source.read(length)

    def close(self):
        self.source.close()


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.rfile = _RequestReader(self.rfile)

    def log_message(self, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._reply(code)

    def _reply(self, status, data=b''):
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Type', 'text/html; charset=utf-8' if data else 'text/plain')
        self.send_header('Content-Security-Policy',
                         f"default-src 'none'; script-src 'sha256-{_SCRIPT_HASH}'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; frame-ancestors 'none'; form-action 'none'; base-uri 'none'")
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _allowed(self):
        receiver = self.server.receiver
        if self.headers.get_all('Host') != [receiver.host]:
            self._reply(403)
        elif self.path != receiver.path:
            self._reply(404)
        elif self.headers.get_all('Origin', []) not in ([], [receiver.origin]):
            self._reply(403)
        elif not receiver.active():
            self._reply(410)
        else:
            return True
        return False

    def do_GET(self):
        if self._allowed():
            self._reply(200, _PAGE)

    def do_POST(self):
        if not self._allowed():
            return
        if self.headers.get_all('Origin') != [self.server.receiver.origin]:
            return self._reply(403)
        if self.headers.get_content_type() != 'application/json' or 'Transfer-Encoding' in self.headers:
            return self._reply(415)
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,8}', lengths[0]):
            return self._reply(400)
        size = int(lengths[0])
        if size > _MAX_BODY:
            return self._reply(413)
        try:
            body = json.loads(self.rfile.read(size).decode('utf-8'))
            if not isinstance(body, dict) or set(body) != {'payload'}:
                raise ValueError
            value = body['payload']
            if not isinstance(value, str) or not 1 <= len(value) <= _MAX_PAYLOAD or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                raise ValueError
            raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
            if value != base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii'):
                raise ValueError
        except (ValueError, RecursionError):
            return self._reply(400)
        self._reply(self.server.receiver.submit(value))


class _Server(ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, receiver):
        self.receiver = receiver
        self.slots = threading.BoundedSemaphore(6)
        super().__init__((receiver.address, 0), _Handler)

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, *args):
        # 요청 경로와 단말 주소를 기록하지 않는다.
        pass

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(3)
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        # 헤더를 조금씩 보내도 하나의 연결은 정해진 시간만 슬롯을 사용한다.
        def interrupt():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        deadline = threading.Timer(min(5, max(0, self.receiver.deadline - time.monotonic())), interrupt)
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            deadline.cancel()
            self.slots.release()


class UsbReceiver:
    """단일 등록 세션 전용 수신기. 닫을 때 포트를 해제하고 보관한 암호문을 지운다."""
    def __init__(self, address, room_id, expires_at_monotonic):
        if not isinstance(address, str) or not (_private_ipv4(address) or address == '127.0.0.1'):
            raise ValueError('휴대폰 USB 연결의 IPv4 주소를 확인해 주세요.')
        if not re.fullmatch(r'[a-f0-9]{64}', room_id):
            raise ValueError('USB 등록 세션을 다시 시작해 주세요.')
        self.address = address
        self.path = '/register/' + room_id
        self.deadline = expires_at_monotonic
        self._lock = threading.Lock()
        self._data = None
        self._server = self._timer = None
        self._closed = False
        self._stopped = threading.Event()

    def start(self):
        with self._lock:
            if self._closed or self.deadline <= time.monotonic():
                raise ValueError('USB 등록 시간이 끝났습니다. 새 QR을 열어 주세요.')
            if self._server:
                return self.origin
            try:
                self._server = _Server(self)
            except OSError:
                raise ValueError('USB 주소에서 등록을 시작할 수 없습니다. ' + _NO_PHONE) from None
            self.host = f'{self.address}:{self._server.server_address[1]}'
            self.origin = 'http://' + self.host
            thread = threading.Thread(target=self._server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
            thread.start()
            self._timer = threading.Timer(max(0, self.deadline - time.monotonic()), self.close)
            self._timer.daemon = True
            self._timer.start()
            return self.origin

    def active(self):
        return not self._closed and time.monotonic() < self.deadline

    def payload(self):
        with self._lock:
            return self._data if self.active() else None

    def submit(self, value):
        with self._lock:
            if not self.active():
                return 410
            if self._data is not None and self._data != value:
                return 409
            self._data = value
            return 204

    def close(self):
        with self._lock:
            already_closing = self._closed
            if not already_closing:
                self._closed = True
                self._data = None
                server, self._server = self._server, None
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
        if already_closing:
            self._stopped.wait()
            return
        try:
            if server:
                server.shutdown()
                server.server_close()
        finally:
            self._stopped.set()
