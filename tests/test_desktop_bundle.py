"""생성된 Windows EXE를 Python 없는 PATH로 실행해 실제 업무 경로를 확인한다."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from unittest.mock import patch
from vworld_fixture import FAKE_KEY, fake_vworld

ROOT = Path(__file__).resolve().parent.parent


def check_theme_ui(url):
    """선택 실행: 실제 EXE가 제공하는 파일/HTTP로 로그인 전후 외형을 확인한다."""
    from playwright.sync_api import expect, sync_playwright
    from test_pc_theme import appearance, login, logout
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block", color_scheme="light")
        page = context.new_page()
        try:
            page.goto(url)
            login(page, "bundle_admin", "bundle-admin-pass-123")
            page.get_by_role("banner").get_by_label("화면 테마", exact=True).select_option("dark")
            page.get_by_role("banner").get_by_label("글자 크기", exact=True).select_option("17")
            page.wait_for_function("() => document.documentElement.dataset.theme === 'dark' && document.documentElement.style.getPropertyValue('--fs') === '17px'")
            before = appearance(page)
            logout(page)
            page.reload()
            expect(page.get_by_role("button", name="로그인", exact=True)).to_be_visible()
            assert appearance(page) == before, "실제 EXE의 로그인 화면 새로고침에서 외형이 바뀜"
            login(page, "bundle_admin", "bundle-admin-pass-123")
            assert appearance(page) == before, "실제 EXE의 로그인 전후 외형이 다름"
            print("실제 EXE/Chromium: 로그인·로그아웃·새로고침·재로그인 테마/글자 크기 보존 통과", flush=True)
        finally:
            context.close()
            browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--legacy-dongs", action="store_true", help="8단계 자료의 기본 동 보충과 기존 설정 보존도 확인")
    parser.add_argument("--theme-ui", action="store_true", help="Playwright가 있으면 실제 EXE의 로그인 전후 외형도 확인")
    args = parser.parse_args()
    exe = args.exe.resolve()
    assert exe.is_file(), f"배포 EXE 없음: {exe}"
    binary = exe.read_bytes()
    pe = struct.unpack_from("<I", binary, 0x3C)[0]
    assert binary[pe:pe + 4] == b"PE\0\0"
    assert struct.unpack_from("<H", binary, pe + 24 + 68)[0] == 2, "콘솔형 EXE로 빌드됨"
    with tempfile.TemporaryDirectory(prefix="dg bundle with spaces ") as directory, fake_vworld() as (vworld, provider):
        state = Path(directory) / "state with spaces"
        env = {**os.environ, "DG_STATE": str(state), "DG_OUT": str(Path(directory) / "out"),
               "DG_NO_SCHEDULER": "1", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "DG_VWORLD_BASE": provider}
        for name in ("DG_DB_KEY", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
            env.pop(name, None)
        sys.path.insert(0, str(ROOT / "pc"))
        from dgpc.dong_defaults import DONGGU_DONGS
        original_dongs = []
        if args.legacy_dongs:
            from dgpc import area, service, store  # service가 쓰기 후처리기를 등록한다.
            with patch.dict(os.environ, {**env, "DG_DB_KEY": "bundle-test-only-pass-123"}), \
                    patch.object(store, "LATEST", 8), \
                    patch.object(store, "MIGRATIONS", [m for m in store.MIGRATIONS if m[0] <= 8]):
                connection = store.connect(create=True)
                try:
                    area.save_dong(connection, {"name": DONGGU_DONGS[0], "active": False, "weekdays": [1, 4],
                                                "daily_limit": 11, "daily_limit_m3": 4.5, "keywords": ["가상 시험로"], "code": "X9", "sort": 98})
                    area.save_dong(connection, {"name": "가상 배포동", "sort": 99})
                    original_dongs = area.list_dongs(connection)
                finally:
                    connection.close()
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
                for path in ("", "start.js", "pc.js", "core.js", "screens/home.js", "screens/dongs.js", "screens/reports.js", "screens/numbered_labels.js", "screens/label_usage.js", "screens/vworld.js", "pc.css", "doc.css",
                             "fonts/PretendardGOV-Regular.subset.woff2", "web/proto.js", "web/fees-data.js",
                             "web/labels-data.js", "web/vendor/maplibre/maplibre-gl.mjs"):
                    with opener.open(url + path, timeout=10) as response:
                        assert response.status == 200 and response.read(), path
                api("setup", {"username": "bundle_admin", "display_name": "가상 배포검증", "password": "bundle-admin-pass-123"})
                assert api("vworld/locate", {"address": "가상시험로 1", "type": "ROAD"})["code"] == "NOT_CONFIGURED"
                api("settings", {"external": {"keys": {"vworld_api_key": FAKE_KEY}, "vworld": {"enabled": True}}})
                reference = api("vworld/locate", {"address": "가상시험로 1, 101동 1203호", "type": "ROAD"})
                assert reference.get("reference_only") and reference.get("source") == "VWORLD", "배포 EXE VWorld 참고 조회 누락"
                assert vworld["calls"][-1]["params"]["address"] == "가상시험로 1", "배포 EXE 상세 동호수 외부 전송"
                assert FAKE_KEY not in json.dumps(api("bootstrap")), "배포 EXE가 원문 인증키를 반환"
                api("settings", {"external": {"keys": {"vworld_api_key": ""}}})
                if args.theme_ui:
                    check_theme_ui(url)
                dongs = api("dongs")
                expected_names = set(DONGGU_DONGS) | ({"가상 배포동"} if args.legacy_dongs else set())
                assert {dong["name"] for dong in dongs} == expected_names, "배포 EXE의 기본 행정동 자동 등록 누락"
                by_id = {dong["id"]: dong for dong in dongs}
                for original in original_dongs:
                    assert by_id[original["id"]] == original, "기존 동 ID·사용 여부·수거 설정 변경"
                if args.legacy_dongs:
                    assert list((state / "pre-migration").glob(f"pc-v8-to-v{store.LATEST}-*.db")), "이전 전 사본 누락"
                dong_order = [dong["id"] for dong in dongs]
                wanted_dong_order = list(reversed(dong_order))
                ordered = api("dongs/order", {"ids": wanted_dong_order, "base_ids": dong_order})["dongs"]
                assert [dong["id"] for dong in ordered] == wanted_dong_order, "배포 EXE의 동 순서 저장 누락"
                original_by_id = {dong["id"]: dong for dong in dongs}
                for dong in ordered:
                    assert {k: v for k, v in dong.items() if k not in {"sort", "updated_at"}} == {
                        k: v for k, v in original_by_id[dong["id"]].items() if k not in {"sort", "updated_at"}}, "순서 저장 중 기존 설정 변경"
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
                process.stdin.close()
                process = subprocess.Popen([str(exe), "--desktop-child", "--state", str(state), "--port", str(port)],
                                           env=env, cwd=directory, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                           text=True, encoding="utf-8")
                process.stdin.write(json.dumps({"key": "bundle-test-only-pass-123"}) + "\n")
                process.stdin.flush()
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError("배포 EXE 재시작 중 조기 종료")
                    try:
                        restarted = api("state")
                        break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise AssertionError("배포 EXE 재시작 준비 시간 초과")
                assert restarted == {"setup_needed": False, "logged_in": False}
                api("login", {"username": "bundle_admin", "password": "bundle-admin-pass-123"})
                assert [dong["id"] for dong in api("dongs")] == wanted_dong_order, "재시작 뒤 동 순서 유실"
                process.stdin.write(json.dumps({"command": "stop"}) + "\n")
                process.stdin.flush()
                assert process.wait(timeout=20) == 0
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
                dong_result = "기존 DB 이전·기본 동 보충·기존 설정 보존" if args.legacy_dongs else "기본 동 13개 자동 등록"
                print(f"배포 EXE: {dong_result}·동 순서 저장/재시작 보존·VWorld 참고 조회(가상 공급자)·콘솔 없음·Python 없는 PATH·공백 경로·자산·암호화 DB·실제 노선 계산·정상 종료 통과")
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
