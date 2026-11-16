"""본문을 읽기 전에 거절하는 응답(403 등)이 클라이언트에 온전히 닿는지.

권한·주소 검사는 요청 본문을 읽기 전에 답한다. 읽지 않은 본문을 남긴 채 연결을 닫으면 Windows는 RST를 보내
클라이언트가 이미 도착한 응답을 읽기 전에 연결이 끊긴다(ConnectionAbortedError 10053). 서버는 답한 뒤 남은 본문을 비우고 닫아야 한다.
"""
import http.client
import json
import unittest

from test_pc_session_revocation import SessionHTTPTestCase


class UnreadBodyTests(SessionHTTPTestCase):
    ROUNDS = 40

    def raw_post(self, path, data, cookie):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/api/" + path, body=data,
                               headers={"X-DG-CSRF": "1", "Content-Type": "application/json", "Cookie": cookie})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_forbidden_reply_survives_unread_body(self):
        data = json.dumps({"pad": "가" * 5000}).encode("utf-8")  # 약 15KB: 거절 전에 읽지 않는 본문
        lost = []
        for i in range(self.ROUNDS):
            try:
                status, body = self.raw_post("dongs/centers", data, self.worker_cookie)
                self.assertEqual(status, 403, body)
            except (ConnectionError, OSError) as error:
                lost.append(f"{i}: {type(error).__name__}")
        self.assertEqual(lost, [], f"{self.ROUNDS}번 중 {len(lost)}번 403 응답을 읽기 전에 연결이 끊김")


if __name__ == "__main__":
    unittest.main(verbosity=2)
