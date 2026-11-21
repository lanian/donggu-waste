"""업무용 PC와 휴대폰의 기기 등록 암호문을 잠시 보관하는 중계 서버.

실행 예: python tools/pairing_relay.py --port 8787
기본 수신 주소는 127.0.0.1이다. 같은 호스트의 HTTPS 역방향 프록시에서
/v1/rooms 경로만 전달한다. 프록시의 요청 경로·본문 기록을 끄고 연결·전송량
제한을 적용한다. X-Forwarded-For는 신뢰하지 않으므로 원격 IP별 제한은
프록시에서 적용한다. 중계는 단일 프로세스로 실행하며 DB와 접근 로그를 쓰지 않는다.
기본 세션 유효기간은 5분, 동시 보관은 256개, 접속 주소당 분당 240요청이다.
휴대폰의 submit만 브라우저 출처 간 호출을 허용한다. 등록 결과의 인증·복호화와
일회용 번호 대조는 PC에서 수행한다.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import re
import threading
import time


HEX = re.compile(r'[0-9a-f]{64}')
ACTION = re.compile(r'/v1/rooms/([0-9a-f]{64})/(submit|read|close)')
PAYLOAD = re.compile(r'[A-Za-z0-9_-]+')
MAX_PAYLOAD = 4096
MAX_BODY = 6144


@dataclass
class Room:
    read_hash: str
    expires: float
    payload: str | None = None


class RelayError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def field(body, key, pattern):
    value = body.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RelayError(400, 'invalid_request')
    return value


class Rooms:
    def __init__(self, *, clock=time.monotonic, ttl=300, max_rooms=256, per_minute=240):
        self.clock = clock
        self.ttl = ttl
        self.max_rooms = max_rooms
        self.per_minute = per_minute
        self.rooms = {}
        self.lock = threading.Lock()
        self.window = self.clock()
        self.peers = {}
        self.requests = 0

    def _expire(self, now):
        for room_id in [k for k, v in self.rooms.items() if v.expires <= now]:
            del self.rooms[room_id]
        if now - self.window >= 60:
            self.window = now
            self.peers.clear()
            self.requests = 0

    def expire(self):
        with self.lock:
            self._expire(self.clock())

    def admit(self, peer):
        with self.lock:
            self._expire(self.clock())
            count = self.peers.get(peer, 0)
            if count >= self.per_minute or self.requests >= 4096:
                raise RelayError(429, 'rate_limit')
            self.peers[peer] = count + 1
            self.requests += 1

    def act(self, room_id, action, body):
        fields = {'create': {'room', 'read_hash'}, 'submit': {'payload'}, 'read': {'read_key'}, 'close': {'read_key'}}
        if not isinstance(body, dict) or set(body) != fields[action]:
            raise RelayError(400, 'invalid_request')
        with self.lock:
            now = self.clock()
            self._expire(now)
            if action == 'create':
                room_id = field(body, 'room', HEX)
                read_hash = field(body, 'read_hash', HEX)
                existing = self.rooms.get(room_id)
                if existing:
                    if not hmac.compare_digest(existing.read_hash, read_hash):
                        raise RelayError(409, 'room_exists')
                    return 200, {'ok': True, 'expires_in': math.ceil(existing.expires - now)}
                if len(self.rooms) >= self.max_rooms:
                    raise RelayError(429, 'room_limit')
                self.rooms[room_id] = Room(read_hash, now + self.ttl)
                return 201, {'ok': True, 'expires_in': self.ttl}
            room = self.rooms.get(room_id)
            if room is None:
                raise RelayError(404, 'room_unavailable')
            if action == 'submit':
                payload = field(body, 'payload', PAYLOAD)
                if len(payload) > MAX_PAYLOAD:
                    raise RelayError(413, 'too_large')
                if len(payload) % 4 == 1:
                    raise RelayError(400, 'invalid_payload')
                decoded = base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))
                if base64.urlsafe_b64encode(decoded).rstrip(b'=').decode('ascii') != payload:
                    raise RelayError(400, 'invalid_payload')
                if room.payload is not None and room.payload != payload:
                    raise RelayError(409, 'already_submitted')
                room.payload = payload
                return 200, {'ok': True}
            read_key = field(body, 'read_key', HEX)
            if not hmac.compare_digest(room.read_hash, hashlib.sha256(read_key.encode('ascii')).hexdigest()):
                raise RelayError(403, 'forbidden')
            if action == 'read':
                return 200, {'payload': room.payload} if room.payload is not None else {'pending': True}
            del self.rooms[room_id]
            return 200, {'ok': True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # 기본 접근 로그에는 방 식별자가 든 주소가 남는다.
        pass

    def _reply(self, status, data, mobile=False):
        encoded = b'' if data is None else json.dumps(data, separators=(',', ':')).encode('ascii')
        self.send_response(status)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        if mobile:
            self.send_header('Access-Control-Allow-Origin', '*')
            if self.command == 'OPTIONS':
                self.send_header('Access-Control-Allow-Methods', 'POST')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        if data is not None:
            self.send_header('Content-Type', 'application/json')
        if status != 204:
            self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        if encoded:
            self.wfile.write(encoded)

    def _handle(self, preflight=False):
        target = ACTION.fullmatch(self.path)
        mobile = bool(target and target[2] == 'submit')
        try:
            self.server.rooms.admit(self.client_address[0])
            if self.path != '/v1/rooms' and not target:
                raise RelayError(404, 'not_found')
            if preflight:
                if not mobile or self.headers.get('Access-Control-Request-Method') != 'POST':
                    raise RelayError(403, 'forbidden')
                return self._reply(204, None, mobile=True)
            if not mobile and 'Origin' in self.headers:
                raise RelayError(403, 'forbidden')
            if self.headers.get_content_type() != 'application/json' or 'Transfer-Encoding' in self.headers:
                raise RelayError(415, 'json_required')
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
                raise RelayError(400, 'invalid_length')
            size = int(lengths[0])
            if size > MAX_BODY:
                raise RelayError(413, 'too_large')
            try:
                body = json.loads(self.rfile.read(size).decode('utf-8'))
            except (ValueError, RecursionError):
                raise RelayError(400, 'invalid_json')
            status, data = self.server.rooms.act(target[1] if target else None, target[2] if target else 'create', body)
        except RelayError as error:
            status, data = error.status, {'error': error.code}
        self._reply(status, data, mobile)

    def do_POST(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle(preflight=True)


class RelayHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, rooms):
        self.rooms = rooms
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(5)
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def service_actions(self):
        self.rooms.expire()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    with RelayHTTPServer((args.host, args.port), Rooms()) as server:
        print(f'기기 등록 중계 수신 {args.host}:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
