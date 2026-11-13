"""가상 주민·실제 SQLCipher/HTTP/브라우저의 동별 등록→배정 통합 흐름."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server

ROOT = Path(__file__).resolve().parent.parent
DAY = "2099-01-05"
PASSWORD = "dong-e2e-initial-123"


def api(context, url, path, body=None, status=200):
    options = {"headers": {"X-DG-CSRF": "1"}, "timeout": 30000}
    response = (context.request.get(url + "api/" + path, **options) if body is None else
                context.request.post(url + "api/" + path, data=body, **options))
    assert response.status == status, (path, response.status, response.text())
    return response.json()


def main():
    shots = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-dong-real-e2e-")) / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        admin = browser.new_context(service_workers="block")
        scoped = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 900})
        page = scoped.new_page()
        page.set_default_timeout(10000)
        try:
            api(admin, url, "setup", {"username": "dong_e2e_admin", "display_name": "가상 동관리자", "password": PASSWORD})
            dongs = [api(admin, url, "dongs", {"name": name})["id"] for name in ("통합가상A동", "통합가상B동", "통합가상C동")]
            crew = api(admin, url, "crews", {"name": "통합가상팀", "capacity_m3": 20})["id"]
            fee = next(row for row in json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf-8"))["rows"]
                       if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)
            seed = {"phone": "01000001111", "address": "가상구 검증로 10", "planned_date": DAY,
                    "lat": 35.146, "lon": 126.923, "items": [{"fee_rate_id": fee["id"], "qty": 1}]}
            api(admin, url, "requests", {**seed, "name": "통합가상A주민", "dong": "통합가상A동"})
            outside = api(admin, url, "requests", {**seed, "name": "통합비공개C주민", "dong": "통합가상C동"})["id"]
            uid = api(admin, url, "users", {"username": "dong_e2e_ab", "display_name": "통합가상동담당", "role": "DONG",
                      "password": PASSWORD, "dong_ids": dongs[:2]})["id"]
            api(scoped, url, "login", {"username": "dong_e2e_ab", "password": PASSWORD})
            page.goto(url + "#/dongs", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="내 계정", exact=True)).to_be_visible()
            page.get_by_label("현재 비밀번호", exact=True).fill(PASSWORD)
            page.get_by_label("새 비밀번호 (10자 이상)", exact=True).fill("dong-e2e-changed-123")
            page.get_by_label("새 비밀번호 확인", exact=True).fill("dong-e2e-changed-123")
            page.get_by_role("button", name="비밀번호 바꾸기", exact=True).click()
            expect(page.get_by_role("heading", name="동별 관리", exact=True)).to_be_visible()
            scope = page.get_by_label("행정동 범위", exact=True)
            expect(scope).to_be_visible()
            assert set(scope.locator("option").evaluate_all("xs => xs.map(x => x.value)")) == {"all", *dongs[:2]}
            assert page.locator('nav.side a[href="#/requests"]').count() == 0
            page.get_by_label("업무 기준일", exact=True).fill(DAY)
            page.get_by_label("업무 기준일", exact=True).press("Tab")
            expect(page.get_by_text("통합가상A주민", exact=True)).to_be_visible()
            assert "통합비공개C주민" not in page.locator("main").inner_text()
            api(scoped, url, f"dong-work/requests/{outside}", status=403)
            api(scoped, url, "requests", status=403)
            scope.select_option(dongs[1])
            page.get_by_role("button", name="접수 등록", exact=True).click()
            dlg = page.get_by_role("region", name="동별 접수 등록", exact=True)
            dlg.get_by_label("이름", exact=True).fill("통합신규B주민")
            dlg.get_by_label("연락처", exact=True).fill("01000002222")
            dlg.get_by_label("배출 주소", exact=True).fill("가상구 시험로 20")
            expect(dlg.get_by_label("접수 행정동", exact=True)).to_have_value(dongs[1])
            dlg.get_by_text("위치 직접 입력", exact=True).click()
            dlg.get_by_label("위도", exact=True).fill("35.147")
            dlg.get_by_label("경도", exact=True).fill("126.924")
            search = dlg.get_by_label("품목명·규격 검색", exact=True)
            search.fill(fee["name"])
            dlg.locator(".results button").first.click()
            dlg.get_by_role("button", name="품목 추가", exact=True).click()
            dlg.get_by_role("button", name="접수 저장", exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            expect(page.get_by_text("통합신규B주민", exact=True)).to_be_visible()
            created = api(scoped, url, f"dong-work/requests?date={DAY}&dong={dongs[1]}")["rows"]
            assert len(created) == 1 and created[0]["name"] == "통합신규B주민"
            detail = api(scoped, url, "dong-work/requests/" + created[0]["id"])
            assert (detail["lat"], detail["lon"], detail["dong"]) == (35.147, 126.924, "통합가상B동")
            page.get_by_role("button", name="노선 계산", exact=True).click()
            expect(page.get_by_role("button", name="배정 확정", exact=True)).to_be_enabled(timeout=25000)
            assert not api(admin, url, "batches?date=" + DAY)
            page.get_by_role("button", name="배정 확정", exact=True).click()
            page.get_by_role("dialog").get_by_role("button", name="이 범위로 배정 확정", exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            batches = api(admin, url, "batches?date=" + DAY)
            assert len(batches) == 1 and batches[0]["crew_id"] == crew, batches
            current = api(scoped, url, f"dong-work/assignments?date={DAY}&dong={dongs[1]}")
            assert created[0]["id"] in json.dumps(current) and outside not in json.dumps(current)
            page.screenshot(path=str(shots / "dong-real-assigned.png"), full_page=True)
            page.get_by_role("button", name=created[0]["receipt_no"], exact=True).click()
            page.get_by_role("region", name="동별 접수 상세", exact=True).get_by_role("button", name="접수 취소", exact=True).click()
            cancellation = page.get_by_role("dialog", name="접수 취소", exact=True)
            cancellation.get_by_role("combobox").select_option("OTHER")
            cancellation.get_by_role("textbox").fill("통합 가상 취소 직접 사유")
            cancellation.get_by_role("button", name="취소 확정", exact=True).click()
            expect(page.get_by_role("dialog")).to_have_count(0)
            cancelled = api(scoped, url, "dong-work/requests/" + created[0]["id"])
            assert cancelled["status"] == "CANCELLED" and cancelled["cancel_reason"] == "통합 가상 취소 직접 사유"
            api(admin, url, f"users/{uid}/dongs", {"dong_ids": [dongs[0]]})
            api(scoped, url, "dong-work/requests/" + created[0]["id"], status=403)
            api(scoped, url, f"dong-work/requests?date={DAY}&dong={dongs[1]}", status=403)
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="동별 관리", exact=True)).to_be_visible()
            assert "통합신규B주민" not in page.locator("main").inner_text()
            expect(page.get_by_label("행정동 범위", exact=True).locator(f'option[value="{dongs[1]}"]')).to_have_count(0)
            admin_page = admin.new_page()
            admin_page.goto(url + "#/dongs", wait_until="domcontentloaded")
            expect(admin_page.get_by_label("행정동 범위", exact=True).locator(f'option[value="{dongs[2]}"]')).to_have_count(1)
            print("동별 실HTTP E2E: 복수 동·첫 암호 변경·전역 차단·위치 포함 신규 접수·명시 배정·기타 사유 취소·담당 범위 취소·관리자 전체 범위 통과")
        except BaseException:
            page.screenshot(path=str(shots / "dong-real-failure.png"), full_page=True)
            raise
        finally:
            scoped.close()
            admin.close()
            browser.close()


if __name__ == "__main__":
    main()
