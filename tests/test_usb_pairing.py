"""휴대폰 USB 인터페이스 선택과 1회용 등록 자료 수신을 가상 자료로 확인한다."""
import base64
import http.client
import json
from pathlib import Path
import socket
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pc'))
import usb_pairing as usb


def adapter(pnp=r'USB\VID_04E8&PID_6863', description='Remote NDIS Compatible Device',
            service='usbrndis6', addresses=('192.168.42.11',)):
    return dict(pnp=pnp, description=description, service=service, addresses=list(addresses))


class DiscoveryTests(unittest.TestCase):
    def discover(self, *adapters):
        with patch.object(usb, '_usb_candidates', return_value=list(adapters)):
            return usb.discover_usb_ipv4()

    def test_unique_usb_phone_address(self):
        for record in [adapter(), adapter(description='NCM Device', service='UsbNcm'),
                       adapter(pnp=r'USB\VID_05AC&PID_12A8', description='Apple Mobile Device Ethernet', service='netaapl')]:
            with self.subTest(record=record):
                self.assertEqual(self.discover(record, record), '192.168.42.11')

    def test_other_adapters_are_never_selected(self):
        for record in [adapter(pnp=r'PCI\VID_04E8'), adapter(pnp=r'ROOT\NET\0001'),
                       adapter(pnp=r'USB\VID_0BDA&PID_8153', description='Realtek USB GbE', service='rtux64w10'),
                       adapter(pnp=r'USB\VID_0525&PID_A4A2', description='RNDIS/Ethernet Gadget'),
                       adapter(addresses=('169.254.10.10', '127.0.0.1', '203.0.113.1', '::1', '100.64.0.1'))]:
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, 'USB'):
                self.discover(record)
        with self.assertRaisesRegex(ValueError, '하나'):
            self.discover(adapter(), adapter(addresses=('172.20.10.2',)))
        with self.assertRaisesRegex(ValueError, '하나'):
            self.discover(adapter(), adapter(pnp=r'PCI\VID_04E8'))


class ReceiverTests(unittest.TestCase):
    room = 'ab' * 32
    path = '/register/' + room
    encoded = base64.urlsafe_b64encode(b'synthetic opaque encrypted packet' * 6).rstrip(b'=').decode()

    def open(self, ttl=5):
        self.receiver = usb.UsbReceiver('127.0.0.1', self.room, time.monotonic() + ttl)
        self.origin = self.receiver.start()
        self.port = urlsplit(self.origin).port
        self.assertEqual(self.origin, f'http://127.0.0.1:{self.port}')
        self.addCleanup(self.receiver.close)

    def request(self, method='POST', path=None, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=2)
        try:
            conn.request(method, path or self.path,
                         json.dumps(data).encode() if data is not None else None,
                         headers or {})
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def post(self, value, **changes):
        return self.request(data={'payload': value},
                            headers={'Origin': self.origin, 'Content-Type': 'application/json', **changes})[0]

    def test_only_landing_page_and_one_immutable_submission(self):
        self.open()
        self.assertIsNone(self.receiver.payload())
        status, headers, page = self.request('GET')
        self.assertEqual(status, 200)
        self.assertIn('no-store', headers['Cache-Control'])
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertIn(b'history.replaceState', page)
        self.assertIn(self.post(self.encoded), (200, 204))
        self.assertIn(self.post(self.encoded), (200, 204))
        self.assertEqual(self.post('YWJj' * 30), 409)
        self.assertEqual(self.receiver.payload(), self.encoded)
        self.assertNotIn(self.encoded.encode(), self.request('GET')[2])
        self.assertEqual(self.request('GET', '/api/bootstrap')[0], 404)
        self.assertEqual(self.request('GET', self.path + '?a=b')[0], 404)
        self.receiver.close()
        self.receiver.close()
        self.assertIsNone(self.receiver.payload())
        with self.assertRaises(OSError):
            socket.create_connection(('127.0.0.1', self.port), timeout=.3)

    def test_host_origin_body_and_fragment_format(self):
        self.open()
        self.assertEqual(self.request('GET', headers={'Host': 'outside.test'})[0], 403)
        self.assertEqual(self.request('GET', headers={'X-Large': 'a' * 12000})[0], 431)
        for origin in ['https://outside.test', 'null', '']:
            self.assertEqual(self.post(self.encoded, Origin=origin), 403)
        self.assertEqual(self.post(self.encoded, **{'Content-Type': 'text/plain'}), 415)
        self.assertEqual(self.post('YW Jj'), 400)
        self.assertEqual(self.post('YX'), 400)  # unused base64 bits
        self.assertEqual(self.post('A' * 7000), 413)
        self.assertEqual(self.request(data={'payload': self.encoded, 'device': 1},
                                     headers={'Origin': self.origin, 'Content-Type': 'application/json'})[0], 400)
        self.assertNotIn('Access-Control-Allow-Origin', self.request('OPTIONS')[1])
        self.assertIsNone(self.receiver.payload())

    def test_idle_expiry_releases_port_and_data(self):
        self.open(ttl=.35)
        self.assertIn(self.post(self.encoded), (200, 204))
        until = time.monotonic() + 2
        while time.monotonic() < until:
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.2):
                    time.sleep(.04)
            except OSError:
                break
        else:
            self.fail('expired listener still accepting connections')
        self.assertIsNone(self.receiver.payload())

    def test_concurrent_phones_cannot_overwrite_first_submission(self):
        self.open()
        packets = [self.encoded, 'YWJj' * 30]
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(self.post, packets))
        self.assertCountEqual(statuses, [204, 409])
        self.assertEqual(self.receiver.payload(), packets[statuses.index(204)])

    def test_bind_must_be_an_explicit_private_address(self):
        for address in ['0.0.0.0', '::', 'example.test', '203.0.113.7', '255.255.255.255', '169.254.2.3']:
            with self.subTest(address=address), self.assertRaises(ValueError):
                usb.UsbReceiver(address, self.room, time.monotonic() + 60).start()


if __name__ == '__main__':
    unittest.main()
