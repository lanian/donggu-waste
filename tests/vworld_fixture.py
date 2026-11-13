"""개인정보와 실제 키 없이 VWorld HTTP 응답을 재현하는 루프백 서버."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import urllib.parse

FAKE_KEY = "VWORLD-TEST-ONLY-NOT-PRODUCTION-1234"


@contextmanager
def fake_vworld():
    state = {"calls": [], "status": 200, "response": {"response": {"status": "OK", "result": {
        "crs": "EPSG:4326", "point": {"x": "126.9233", "y": "35.1462"}}}}, "gate": None,
             "entered": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            state["calls"].append({"path": url.path, "params": dict(urllib.parse.parse_qsl(url.query)),
                                   "referer": self.headers.get("Referer")})
            state["entered"].set()
            if state["gate"]:
                state["gate"].wait(8)
            body = json.dumps(state["response"]).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        if state["gate"]:
            state["gate"].set()
        server.shutdown()
        server.server_close()
        worker.join(5)
