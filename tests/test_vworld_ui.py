"""실제 PC HTTP/Chromium의 VWorld 설정·참고 조회·초안 보존 회귀."""
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server
from test_dong_order_ui import api
from vworld_fixture import FAKE_KEY, fake_vworld

passed = 0


def ok(value, label):
    global passed
    assert value, label
    passed += 1
    print("  OK", label, flush=True)


def main():
    shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-vworld-ui-")) / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    with fake_vworld() as (fake, provider), patch.dict(os.environ, {"DG_VWORLD_BASE": provider}), pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        page = context.new_page()
        page.set_default_timeout(10000)
        try:
            api(context, url, "setup", {"username": "vworld_admin", "display_name": "가상 위치 관리자", "password": "vworld-ui-test-123"})
            page.goto(url + "#/request/new")
            expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
            launcher = page.get_by_role("button", name="VWorld 위치 참고", exact=True)
            launcher.click()
            dialog = page.get_by_role("dialog", name="VWorld 위치 참고", exact=True)
            expect(dialog.get_by_role("button", name="위치 조회", exact=True)).to_be_disabled()
            expect(dialog.get_by_text("관리자가 VWorld API 키를 등록하고 참고 조회를 켜면 사용할 수 있습니다.", exact=True)).to_be_visible()
            dialog.get_by_role("button", name="취소", exact=True).click()
            expect(launcher).to_be_focused()
            ok(not fake["calls"], "미등록은 외부 호출 없이 설정 안내, 닫은 뒤 원래 버튼으로 초점 복귀")

            page.goto(url + "#/settings/addr")
            card = page.locator('.card').filter(has=page.get_by_role("heading", name="외부 서비스 (주소 검색·도로 이동시간)", exact=True))
            card.get_by_label("VWorld API 키", exact=True).fill(FAKE_KEY)
            card.get_by_label("VWorld 참고 조회 사용", exact=True).check()
            card.get_by_label("VWorld 등록 서비스 URL", exact=True).fill("http://127.0.0.1:8765")
            card.get_by_role("button", name="저장", exact=True).click()
            expect(card.get_by_label("VWorld API 키", exact=True)).to_have_value("")
            expect(card.get_by_label("VWorld 참고 조회 사용", exact=True)).to_be_checked()
            ok(FAKE_KEY not in str(api(context, url, "bootstrap")), "키 저장 뒤 원문은 화면/bootstrap에 반환하지 않음")
            card.get_by_role("button", name="VWorld 연결 시험", exact=True).click()
            expect(card.get_by_text("✓ VWorld 위치 참고 조회 정상 (결과 저장 안 함)", exact=True)).to_be_visible()
            ok(fake["calls"][-1]["params"]["address"] == "광주광역시 동구 서남로 1", "연결 시험은 신청인 정보 없는 고정 공공주소만 사용")

            page.goto(url + "#/request/new")
            expect(page.get_by_role("heading", name="전화 접수 등록", exact=True)).to_be_visible()
            fields = {"이름": "가상신청인", "연락처": "010-2345-6789", "배출 주소": "광주광역시 동구 가상시험로 1, 101동 1203호",
                      "상세 배출 위치": "분리수거장 옆", "요청 메모": "가상 보존 메모"}
            for name, value in fields.items():
                page.get_by_label(name, exact=True).fill(value)
            launcher.click()
            expect(dialog.get_by_label("VWorld 조회 주소", exact=True)).to_have_value(fields["배출 주소"])
            lookup = dialog.get_by_role("button", name="위치 조회", exact=True)
            fake["status"] = 503
            lookup.click()
            expect(dialog.get_by_text("외부 서버 오류(503)", exact=True)).to_be_visible()
            expect(lookup).to_be_enabled()
            ok(all(page.get_by_label(k, exact=True).input_value() == v for k, v in fields.items()), "조회 실패 뒤에도 접수 필드 유지, 다시 조회 가능")
            fake["status"] = 200
            fake["response"] = {"response": {"status": "OK", "result": {"crs": "EPSG:4326", "point": {"x": "126.932167", "y": "35.157891"}}}}
            lookup.click()
            expect(dialog.get_by_text("브이월드(국토교통부)", exact=True)).to_be_visible()
            expect(dialog.get_by_text("위도 35.157891 · 경도 126.932167", exact=True)).to_be_visible()
            ok(fake["calls"][-1]["params"]["address"] == "광주광역시 동구 가상시험로 1", "상세 동호수·신청인 이름·전화·메모는 공급자에 보내지 않음")
            dialog.screenshot(path=str(shots / "vworld_reference.png"))
            dialog.get_by_role("button", name="닫기", exact=True).click()
            expect(launcher).to_be_focused()
            ok(all(page.get_by_label(k, exact=True).input_value() == v for k, v in fields.items())
               and not page.locator('input').evaluate_all("xs => xs.some(x => ['35.157891', '126.932167'].includes(x.value))"),
               "참고 결과를 접수 주소·좌표·행정동 입력에 적용하지 않음")
            ok(not page.evaluate("() => JSON.stringify({...localStorage, ...sessionStorage}).includes('126.932167')"),
               "조회 좌표가 브라우저 저장소/접수 초안에 남지 않음")

            launcher.click()
            fake["gate"] = threading.Event()
            fake["entered"].clear()
            before = len(fake["calls"])
            lookup.click()
            assert fake["entered"].wait(4), "공급자 응답 대기 지점에 도달하지 못함"
            expect(lookup).to_be_disabled()
            expect(dialog.get_by_label("VWorld 조회 주소", exact=True)).to_be_disabled()
            dialog.get_by_role("button", name="취소", exact=True).click()
            expect(launcher).to_be_focused()
            fake["gate"].set()
            launcher.click()
            expect(dialog.get_by_text("브이월드(국토교통부)", exact=True)).to_have_count(0)
            ok(len(fake["calls"]) <= before + 1, "조회 중 중복 실행을 막고 취소 뒤 늦은 결과를 새 상자에 표시하지 않음")
            page.goto(url + "#/settings/addr")
            expect(page.get_by_role("dialog")).to_have_count(0)
            ok(True, "화면 이동 시 참고 상자를 닫음")
            print(f"VWorld 실제 HTTP/UI {passed}개 통과 · 캡처 {shots}", flush=True)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
