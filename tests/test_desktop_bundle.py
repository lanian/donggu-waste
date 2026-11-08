"""생성된 Windows EXE를 Python 없는 PATH로 실행해 실제 업무 경로를 확인한다."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    exe = parser.parse_args().exe.resolve()
    assert exe.is_file(), f"배포 EXE 없음: {exe}"
    binary = exe.read_bytes()
    pe = struct.unpack_from("<I", binary, 0x3C)[0]
    assert binary[pe:pe + 4] == b"PE\0\0"
    assert struct.unpack_from("<H", binary, pe + 24 + 68)[0] == 2, "콘솔형 EXE로 빌드됨"
    with tempfile.TemporaryDirectory(prefix="dg bundle with spaces ") as directory:
        state = Path(directory) / "state with spaces"
        env = {**os.environ, "DG_STATE": str(state), "DG_OUT": str(Path(directory) / "out"),
               "DG_NO_SCHEDULER": "1", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}
        for name in ("DG_DB_KEY", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(name, None)
        env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") + ";" + os.environ["SystemRoot"]
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"http://127.0.0.1:{port}/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def api(path, body=None):
            data = None if body is None else json.dumps(body).encode("utf-8")
            request = urllib.request.Request(url + "api/" + path, data=data,
                                             headers={"X-DG-CSRF": "1", "Content-Type": "application/json"})
            try:
                with opener.open(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                raise AssertionError(f"{path}: HTTP {error.code} {error.read().decode('utf-8')}") from error
        with (Path(directory) / "process.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([str(exe), "--desktop-child", "--state", str(state), "--port", str(port)],
                                       env=env, cwd=directory, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                       text=True, encoding="utf-8")
            try:
                process.stdin.write(json.dumps({"key": "bundle-test-only-pass-123"}) + "\n")
                process.stdin.flush()
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError("배포 서버 조기 종료: " + (Path(directory) / "process.log").read_text(encoding="utf-8"))
                    try:
                        initial = api("state")
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise AssertionError("배포 서버 준비 시간 초과")
                assert initial == {"setup_needed": True, "logged_in": False}
                for path in ("", "start.js", "pc.js", "core.js", "screens/home.js", "screens/dongs.js", "screens/reports.js", "screens/numbered_labels.js", "pc.css", "doc.css",
                             "fonts/PretendardGOV-Regular.subset.woff2", "web/proto.js", "web/fees-data.js",
                             "web/labels-data.js", "web/vendor/maplibre/maplibre-gl.mjs"):
                    with opener.open(url + path, timeout=10) as response:
                        assert response.status == 200 and response.read(), path
                api("setup", {"username": "bundle_admin", "display_name": "가상 배포검증", "password": "bundle-admin-pass-123"})
                crew = api("crews", {"name": "가상 배포팀"})["id"]
                fee = next(row["id"] for row in json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf-8"))["rows"]
                           if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)
                day = "2099-01-02"
                ids = []
                for number in range(2):
                    request = api("requests", {"name": f"가상 배포 {number}", "phone": "01000001234", "address": f"가상구 시험로 {number}",
                                              "planned_date": day, "lat": 35.146 + number * .001, "lon": 126.923,
                                              "items": [{"fee_rate_id": fee, "qty": 1}]})
                    ids.append(request["id"])
                result = api("route", {"date": day, "crew_ids": [crew]})
                planned = [stop["request_id"] for route in result["routes"] for stop in route["stops"]]
                assert sorted(planned) == sorted(ids), result
                assert (state / "pc.db").read_bytes()[:16] != b"SQLite format 3\0"
                process.stdin.write(json.dumps({"command": "stop"}) + "\n")
                process.stdin.flush()
                assert process.wait(timeout=20) == 0
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
                assert (state / "pc.db").is_file()
                print("배포 EXE: 콘솔 없음·Python 없는 PATH·공백 경로·자산·암호화 DB·실제 노선 계산·정상 종료 통과")
            except Exception:
                evidence = Path(tempfile.mkdtemp(prefix="dg-bundle-failure-"))
                for source in (state / "logs").glob("*.log"):
                    (evidence / source.name).write_bytes(source.read_bytes())
                print("가상 배포 시험 오류 기록: " + str(evidence), flush=True)
                raise
            finally:
                if process.stdin:
                    process.stdin.close()
                if process.poll() is None:
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == "__main__":
    main()
