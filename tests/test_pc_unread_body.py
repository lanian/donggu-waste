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


    def test_client_disconnect_is_not_logged_as_server_error(self):
        """응답을 쓰는 중에 브라우저가 연결을 끊으면(다른 화면으로 이동 등) 서버 오류(500·추적 기록)로 남기지 않는다."""
        import contextlib
        import io
        from unittest.mock import patch
        from dgpc import server as SV

        def gone(*_a, **_k):
            raise ConnectionAbortedError(10053, "클라이언트가 연결을 끊음(시험)")

        err = io.StringIO()
        with patch.object(SV.Handler, "api", gone), contextlib.redirect_stderr(err), self.assertNoLogs(SV.LOG, level="ERROR"):
            try:
                self.raw_post("dongs/centers", b"{}", self.admin_cookie)
            except (ConnectionError, OSError, http.client.HTTPException, ValueError):
                pass  # 응답 없이 닫힘(정상)
        self.assertNotIn("Traceback", err.getvalue(), "연결 끊김을 서버 오류 추적 기록으로 출력함")

if __name__ == "__main__":
    unittest.main(verbosity=2)
