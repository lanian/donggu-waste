"""새 개인폰에서 받은 팀 배차로 일한 뒤 담당자가 결과 파일을 읽어 반영한다."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright

from test_dong_work_e2e import api
from test_guest_list_workflow import assign_visit, open_scanner, send_camera_image
from test_review_ui import mobile_server, pc_server


def main():
    with pc_server() as office, mobile_server() as site, sync_playwright() as pw, TemporaryDirectory() as tmp:
        browser = pw.chromium.launch(executable_path=os.getenv("CHROME") or pw.chromium.executable_path)
        try:
            pc = browser.new_context()
            mobile = browser.new_context(viewport={"width": 360, "height": 640})
            phone = mobile.new_page()
            open_scanner(phone, site)
            day, _, request, fee, _ = assign_visit(pc, office)
            desk = pc.new_page()
            desk.goto(office + "#/send?date=" + day)
            desk.get_by_role("button", name="현장 업무 QR", exact=True).click()
            dlg = desk.get_by_role("dialog", name="현장 업무 QR")
            send_camera_image(phone, dlg.get_by_role("img", name="열쇠 QR").screenshot())
            expect(phone.get_by_text("열쇠 읽음", exact=True)).to_be_visible()
            dlg.get_by_role("button", name="목록 QR 표시").click()
            phone.wait_for_function("!!navigator.serviceWorker.controller")
            mobile.set_offline(True)
            qr = dlg.get_by_role("img", name="목록 QR", exact=True)
            send_camera_image(phone, qr.screenshot())
            # 업무 QR은 발급 당시 접수 버전도 포함하여 여러 장이 될 수 있다.
            next_frame = dlg.get_by_role("button", name="다음 QR")
            if next_frame.count():
                total = int(dlg.get_by_label("현재 QR 번호").inner_text().split("/")[1])
                for index in range(1, total):
                    expect(phone.get_by_text(f"목록 QR {index} / {total}", exact=True)).to_be_visible()
                    next_frame.click()
                    send_camera_image(phone, qr.screenshot())
            phone.get_by_role("button", name="1번 수거 결과 기록").click()
            item_name = " · ".join(filter(None, [fee["name"], fee.get("spec")]))
            phone.get_by_label(item_name + " 수거 수량", exact=True).fill("2")
            phone.get_by_role("button", name="현장 품목 추가").click()
            phone.get_by_label("품목명", exact=True).fill("가상 추가 탁자")
            phone.get_by_label("변동·미수거 사유").fill("수거지에 두 개 배출")
            phone.get_by_role("button", name="수거 결과 저장").click()
            phone.get_by_role("button", name="현장 접수 추가").click()
            for label, value in [("신청인", "가상 현장"), ("연락처", "01000001234"), ("배출 주소", "가상로 77"),
                                 ("행정동", "지산1동"), ("품목명", "가상 추가 의자")]:
                phone.get_by_label(label, exact=True).fill(value)
            phone.get_by_role("button", name="현장 접수 저장").click()
            for weight in ("20", "25"):
                phone.get_by_role("button", name="하차 기록 추가").click()
                phone.get_by_label("하차지").fill("가상 반입지")
                phone.get_by_label("하차 중량(kg)").fill(weight)
                phone.get_by_role("button", name="하차 기록 저장").click()
            with phone.expect_download() as event:
                phone.get_by_role("button", name="결과 파일 저장").click()
            file = Path(tmp) / "현장결과.dgwork"
            event.value.save_as(file)
            assert "가상로" not in file.read_text("utf8")
            assert phone.evaluate("async () => { const s=await import('./store.js'); return !await s.getRaw('device') && !await s.getRaw('pc'); }")
            desk.goto(office + "#/results")
            review = desk.get_by_role("region", name="현장 업무 결과 확인")
            picker = review.get_by_label("현장 업무 결과 파일")
            picker.set_input_files(file)
            confirm = review.get_by_role("button", name="확인한 결과 반영")
            expect(confirm).to_be_visible()
            expect(review).to_contain_text("반영 예정 4건")
            expect(review).to_contain_text("가상 추가 탁자")
            detail = "requests/" + request["id"]
            assert api(pc, office, detail)["status"] != "PARTIAL"
            review.get_by_role("button", name="취소").click()
            picker.set_input_files(file)
            confirm.click()
            expect(review).to_contain_text("반영 4건 · 이미 반영 0건")
            assert api(pc, office, detail)["status"] == "PARTIAL"
            picker.set_input_files(file)
            expect(review).to_contain_text("반영 예정 0건")
            expect(confirm).to_have_count(0)
            print("기기 등록 없는 실제 QR 수신·오프라인 부분 수거·현장 접수·2회 하차·PC 확인/취소/반영·재전송 중복 방지 통과")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
