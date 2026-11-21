"""PC·휴대폰 등록 중계의 HTTP 계약과 메모리 만료를 로컬에서 검증한다."""
import base64
import hashlib
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import pairing_relay as relay


def digest(text):
    return hashlib.sha256(text.encode('ascii')).hexdigest()


class PairingRelayTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.store = relay.Rooms(clock=lambda: self.now, max_rooms=2)
        self.server = relay.RelayHTTPServer(('127.0.0.1', 0), self.store)
        worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        secret = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b'=').decode('ascii')
        self.room = digest('dg-pair-room-v1:' + secret)
        self.key = digest('dg-pair-read-v1:' + secret)
        self.ticket = {'room': self.room, 'read_hash': digest(self.key)}
        self.path = '/v1/rooms/' + self.room + '/'
        self.payload = base64.urlsafe_b64encode(b'opaque-ciphertext' * 4).rstrip(b'=').decode()

    def request(self, path, body=None, *, method='POST', headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request(method, path, json.dumps(body).encode() if body is not None else None,
                         {'Content-Type': 'application/json', **(headers or {})})
            res = conn.getresponse()
            data = res.read()
            return res.status, json.loads(data) if data else None, res.headers
        finally:
            conn.close()

    def test_ticket_submit_read_close_and_retries(self):
        self.assertEqual(self.request('/v1/rooms', self.ticket)[:2], (201, {'ok': True, 'expires_in': 300}))
        self.now += 12
        self.assertEqual(self.request('/v1/rooms', self.ticket)[:2], (200, {'ok': True, 'expires_in': 288}))
        self.assertEqual(self.request('/v1/rooms', {**self.ticket, 'read_hash': '0' * 64})[0], 409)
        self.assertEqual(self.request(self.path+'read', {'read_key': self.key})[:2], (200, {'pending': True}))
        for verb in ('read', 'close'):
            self.assertEqual(self.request(self.path+verb, {'read_key': '1'*64})[0], 403)
        for _ in range(2):
            self.assertEqual(self.request(self.path+'submit', {'payload': self.payload})[:2], (200, {'ok': True}))
        for _ in range(2):
            self.assertEqual(self.request(self.path+'read', {'read_key': self.key})[:2], (200, {'payload': self.payload}))
        self.assertEqual(self.request(self.path+'close', {'read_key': self.key})[:2], (200, {'ok': True}))
        self.assertEqual(self.request(self.path+'read', {'read_key': self.key})[0], 404)

    def test_first_simultaneous_submission_wins(self):
        self.assertEqual(self.request('/v1/rooms', self.ticket)[0], 201)
        values = [self.payload, self.payload+'AAAA']
        with ThreadPoolExecutor(2) as pool:
            statuses = list(pool.map(lambda v: self.request(self.path+'submit', {'payload': v})[0], values))
        self.assertCountEqual(statuses, [200, 409])
        self.assertEqual(self.request(self.path+'read', {'read_key': self.key})[1], {'payload': values[statuses.index(200)]})

    def test_only_mobile_submit_supports_browser_cors(self):
        origin = {'Origin': 'https://example.test', 'Access-Control-Request-Method': 'POST',
                  'Access-Control-Request-Headers': 'content-type'}
        self.assertEqual(self.request('/v1/rooms', self.ticket)[0], 201)
        for path in ('/v1/rooms', self.path+'read', self.path+'close'):
            self.assertNotIn('Access-Control-Allow-Origin', self.request(path, method='OPTIONS', headers=origin)[2])
        status, _, headers = self.request(self.path+'submit', method='OPTIONS', headers=origin)
        self.assertEqual(status, 204)
        self.assertEqual(headers['Access-Control-Allow-Origin'], '*')
        self.assertIn('content-type', headers['Access-Control-Allow-Headers'].lower())
        self.assertNotIn('Access-Control-Allow-Credentials', headers)
        self.assertEqual(self.request(self.path+'submit', {'payload': self.payload}, headers=origin)[2]['Access-Control-Allow-Origin'], '*')
        self.assertEqual(self.request(self.path+'read', {'read_key': self.key}, headers=origin)[0], 403)

    def test_capacity_expiry_size_and_throttling(self):
        self.assertEqual(self.request('/v1/rooms', self.ticket)[0], 201)
        self.assertEqual(self.request('/v1/rooms', {**self.ticket, 'room': '1'*64})[0], 201)
        self.assertEqual(self.request('/v1/rooms', {**self.ticket, 'room': '2'*64})[0], 429)
        self.assertEqual(self.request(self.path+'submit', {'payload': 'A'*8193})[0], 413)
        self.assertEqual(self.request(self.path+'submit', {'payload': 'A'*4097})[0], 413)
        self.assertEqual(self.request(self.path+'submit', {'payload': 'bad data'})[0], 400)
        self.assertEqual(self.request(self.path+'submit', {'payload': self.payload, 'name': '가상 이름'})[0], 400)
        self.now += 301
        self.assertEqual(self.request(self.path+'submit', {'payload': self.payload})[0], 404)
        self.assertEqual(self.request('/v1/rooms', {**self.ticket, 'room': '2'*64})[0], 201)
        self.store.per_minute = 2
        self.assertEqual(self.request('/v1/rooms', self.ticket)[0], 429)


if __name__ == '__main__':
    unittest.main()
