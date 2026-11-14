"""실제 암호화 결과 가져오기 → PC 현장 확인 목록 회귀(임시 PC 서버만 사용)."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

from playwright.sync_api import expect, sync_playwright
import qrcode
from test_review_ui import pc_server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
import dgproto as P


class Phone:
    def __init__(self, registration):
        raw = P.b45decode(registration)
        self.pc_kex = P.pub_from_raw(raw[69:134])
        self.kex, self.sign, self.id, self.seq = P.gen_key(), P.gen_key(), uuid.uuid4().bytes, 0

    def code(self):
        raw = self.id + P.pub_raw(self.kex) + P.pub_raw(self.sign)
        return "DGD1." + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip("=")

    def event(self, request_id, kind, body):
        self.seq += 1
        return {"event_id": str(uuid.uuid4()), "seq": self.seq, "request_id": request_id,
                "assign_ver": 0, "base_req_ver": 1, "kind": kind, "body": body,
                "device_ts": f"2026-11-14T02:{self.seq:02d}:00Z"}

    def seal(self, events, day):
        head = P.Header(P.TYPE_RESULT, uuid.uuid4().bytes, self.id, 1, 1, P.key_id(P.pub_raw(self.sign)))
        return P.seal({"schema": "dg.result/1", "work_date": day, "events": events}, head, self.pc_kex, self.sign)[0]


def main():
    explicit_evidence, test_dir = os.environ.get("DG_IMPORT_UI_EVIDENCE"), os.environ.get("DG_TEST_DIR")
    if explicit_evidence:
        evidence = Path(explicit_evidence)
    elif test_dir:
        evidence = Path(test_dir) / "shots" / "pc-field-import"
    else:
        evidence = Path(tempfile.mkdtemp(prefix="dg-field-import-ui-"))
    evidence.mkdir(parents=True, exist_ok=True)
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers="block", viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def api(path, body=None, raw=None):
            headers = {"X-DG-CSRF": "1"}
            if raw is not None:
                headers["Content-Type"] = "application/octet-stream"
                response = context.request.post(url + "api/" + path, data=raw, headers=headers)
            elif body is not None:
                response = context.request.post(url + "api/" + path, data=body, headers=headers)
            else:
                response = context.request.get(url + "api/" + path, headers=headers)
            assert response.ok, (path, response.status, response.text())
            return response.json()

        try:
            api("setup", {"username": "field_review", "display_name": "가상 현장 확인자", "password": "field-review-pass-123"})
            day = api("bootstrap")["today"]
            held_conflicts = []
            page.route("**/api/conflicts", lambda route: held_conflicts.append(route), times=1)
            page.goto(url + "#/results")
            page.add_script_tag(url=url + "web/vendor/jsQR.js")
            registration = page.evaluate("""async (src) => {
                const image = new Image(); image.src = src; await image.decode();
                const canvas = document.createElement('canvas'); canvas.width = image.width; canvas.height = image.height;
                const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0);
                const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
                return window.jsQR(data.data, data.width, data.height).data;
            }""", api("registration-qr")["image"])
            phone = Phone(registration)
            device = api("devices/register", {"code": phone.code(), "label": "가상 현장 폰"})
            api(f"devices/{device['device_id']}/confirm", {})
            crew = api("crews", {"name": "가상 현장 팀", "device_id": device["device_id"], "capacity_m3": 8})
            fee = next(row["id"] for row in json.loads((ROOT / "data/fee_table.json").read_text(encoding="utf-8"))["rows"]
                       if row["status"] == "CONFIRMED" and row.get("price", 0) > 0)

            def intake(request_id):
                return phone.event(request_id, "FIELD_INTAKE", {"temp_no": "현장-시험", "name": "가상 현장 주민",
                    "phone": "01000000000", "address": "가상구 시험길 1", "planned_date": day,
                    "items": [{"line_id": str(uuid.uuid4()), "fee_rate_id": fee, "qty": 1}]})

            existing, new_id, missing = (str(uuid.uuid4()) for _ in range(3))
            seed = api("import/result", raw=phone.seal([intake(existing)], day))
            assert seed["field_intakes"] == 1
            existing_before = api(f"requests/{existing}")
            added_line = str(uuid.uuid4())
            events = [intake(new_id), phone.event(existing, "FIELD_ITEM_ADD", {
                "line_id": added_line, "name": "가상 추가 품목", "spec": "현장 확인 규격", "qty": 2, "unit": "EA", "unit_label": "개"}),
                phone.event(missing, "FIELD_ITEM_ADD", {"line_id": str(uuid.uuid4()), "name": "가상 충돌 품목", "qty": 1})]
            result_file = phone.seal(events, day)
            upload = {"name": "field-review.dgr", "mimeType": "application/octet-stream", "buffer": result_file}
            def upload_result(file):
                with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("api/import/result")) as response:
                    page.get_by_label("결과·사진 파일 (여러 개)").set_input_files(file)
                return response.value.json()
            imported = upload_result(upload)
            expect(page.locator("#import-run .imp-row")).to_contain_text("현장 접수 만듦 1")
            expect(page.locator("#import-run .code-kind")).to_have_text("D-")
            try:
                expect(page.locator("#import-run .field-review")).to_be_visible(timeout=3000)
            except BaseException:
                page.screenshot(path=str(evidence / "red-missing-field-review.png"), full_page=True)
                raise
            import_id = imported["import_id"]
            review = page.locator(f'#import-run .field-review[data-import="{import_id}"]')
            expect(review.get_by_role("heading", name="가져온 현장 내용 확인")).to_be_visible()
            new_row = review.locator(f'[data-request="{new_id}"]')
            add_row = review.locator(f'[data-request="{existing}"]')
            conflict_row = review.locator(f'[data-request="{missing}"]')
            expect(new_row).to_contain_text("새 현장 접수")
            expect(new_row).to_contain_text("사무실 확인 필요")
            expect(new_row.locator(f'a[href="#/request/{new_id}"]')).to_be_visible()
            expect(add_row).to_contain_text("추가 품목")
            expect(add_row).to_contain_text("가상 추가 품목")
            expect(add_row).to_contain_text("2개")
            expect(add_row).to_contain_text("단가 확인 대기")
            expect(add_row.locator(f'a[href="#/request/{existing}"]')).to_be_visible()
            expect(conflict_row).to_contain_text("충돌 확인 필요")
            expect(conflict_row.locator('a[href="#conflicts"]')).to_be_visible()
            expect(conflict_row.locator(f'a[href="#/request/{missing}"]')).to_have_count(0)
            expect(conflict_row).not_to_contain_text("반영 완료")
            expect(conflict_row).not_to_contain_text("단가 확인 대기")
            reason = page.locator("#conflicts").get_by_label("판단 사유")
            reason.fill("가상 현장 내용을 확인 중")
            assert len(held_conflicts) == 1
            with page.expect_response(lambda response: response.url.endswith("api/conflicts")) as old_conflicts:
                held_conflicts[0].fulfill(content_type="application/json", body="[]")
            old_conflicts.value.body()
            page.evaluate("() => new Promise(requestAnimationFrame)")
            try:
                expect(reason).to_have_value("가상 현장 내용을 확인 중")
            except BaseException:
                page.screenshot(path=str(evidence / "red-stale-conflicts.png"), full_page=True)
                raise
            conflict_row.get_by_role("link", name="충돌함에서 확인", exact=True).click()
            expect(reason).to_have_value("가상 현장 내용을 확인 중")
            expect(page.get_by_label("가져오기 고르기")).to_have_value(import_id)
            # 숨겨진 충돌 탭에서 이동해 표를 다시 그릴 때도 입력 사유를 보존한다.
            page.locator(".conf-tabs .qchip", has_text="현장 요청").click()
            expect(page.locator("#conflicts .ev")).to_have_count(0)
            conflict_row.get_by_role("link", name="충돌함에서 확인", exact=True).click()
            expect(reason).to_have_value("가상 현장 내용을 확인 중")
            assert page.url == url + "#/results"
            actual = api(f"requests/{existing}")
            assert len(actual["state"]["lines"]) == len(existing_before["state"]["lines"]) + 1
            assert sum(item["line_id"] == added_line for item in actual["state"]["lines"]) == 1

            # 같은 파일 재선택은 같은 가져오기/접수/행을 유지한다.
            duplicate = upload_result(upload)
            expect(page.locator("#import-run .imp-row").first).to_contain_text("이미 가져온 파일 (변경 없음)")
            assert duplicate["import_id"] == import_id
            assert api(f"requests/{new_id}")["id"] == new_id
            assert sum(item["line_id"] == added_line for item in api(f"requests/{existing}")["state"]["lines"]) == 1
            page.screenshot(path=str(evidence / "green-import-review.png"), full_page=True)

            # 최근 이력 조회 실패는 가져오기·코드를 지우지 않고 다시 읽을 수 있다.
            page.reload()
            path = f"**/api/imports/{import_id}/field-review"
            failures = []
            def fail_once(route):
                failures.append(route.request.url)
                route.fulfill(status=503, content_type="application/json", body='{"error":"가상 일시 조회 실패"}')
            page.route(path, fail_once, times=1)
            recent_row = page.locator(f'#recent-imports tr[data-import="{import_id}"]')
            recent_row.get_by_role("button", name="현장 내용 확인", exact=True).click()
            history_review = recent_row.locator(".field-review")
            expect(history_review).to_contain_text("가상 일시 조회 실패")
            expect(recent_row.locator(".code-kind")).to_have_text("D-")
            history_review.get_by_role("button", name="다시 불러오기").click()
            expect(history_review.locator(f'a[href="#/request/{new_id}"]')).to_be_visible()
            assert len(failures) == 1
            page.screenshot(path=str(evidence / "green-history-review.png"), full_page=True)

            # 새 잘못된 파일은 성공한 요약/확인 목록을 지우지 않는다.
            upload_result(upload)
            expect(page.locator("#import-run .field-review")).to_be_visible()
            page.get_by_label("결과·사진 파일 (여러 개)").set_input_files({"name": "invalid.dgr", "mimeType": "application/octet-stream", "buffer": b"invalid"})
            expect(page.locator("#import-run .imp-row.bad")).to_have_count(1)
            expect(page.locator(f'#import-run a[href="#/request/{new_id}"]')).to_be_visible()

            # 합성 카메라 영상의 실제 QR 판독/복호/HTTP 가져오기도 같은 확인 목록으로 이어진다.
            qr_id = str(uuid.uuid4())
            qr_raw = phone.seal([intake(qr_id)], day)
            images = []
            for frame in P.split_frames(qr_raw, qr_raw[4:20]):
                qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=4)
                qr.add_data(frame)
                qr.make(fit=True)
                png = io.BytesIO()
                qr.make_image().save(png, format="PNG")
                images.append("data:image/png;base64," + base64.b64encode(png.getvalue()).decode())
            page.evaluate("""async (sources) => {
                const images = await Promise.all(sources.map(async (src) => { const image = new Image(); image.src = src; await image.decode(); return image; }));
                const canvas = document.createElement('canvas'); canvas.width = 1280; canvas.height = 720;
                const ctx = canvas.getContext('2d'); ctx.imageSmoothingEnabled = false; let index = 0;
                const draw = () => { const image = images[index++ % images.length]; ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 1280, 720); ctx.drawImage(image, (1280 - image.width) / 2, (720 - image.height) / 2); };
                draw(); window.__fieldQrTimer = setInterval(draw, 500);
                const stream = canvas.captureStream(10);
                navigator.mediaDevices.getUserMedia = async () => stream;
            }""", images)
            with page.expect_response(lambda response: response.url.endswith("api/import/result?channel=QR")) as qr_response:
                page.get_by_role("button", name="웹캠으로 읽기", exact=True).click()
            assert qr_response.value.ok, qr_response.value.text()
            qr_import = qr_response.value.json()["import_id"]
            expect(page.locator("#import-run .imp-row").first).to_contain_text("휴대폰 화면 QR (웹캠)")
            expect(page.locator(f'#import-run .field-review[data-import="{qr_import}"] a[href="#/request/{qr_id}"]')).to_be_visible()
            expect(page.locator(f'#recent-imports tr[data-import="{qr_import}"]')).to_contain_text("웹캠 QR")
            page.evaluate("clearInterval(window.__fieldQrTimer)")
            page.screenshot(path=str(evidence / "green-qr-import-review.png"), full_page=True)

            plain_result = phone.seal([phone.event(existing, "FIELD_MEMO", {"text": "가상 작업 메모"})], day)
            ordinary = upload_result({"name": "ordinary-result.dgr", "mimeType": "application/octet-stream", "buffer": plain_result})
            empty_review = page.locator(f'#import-run .field-review[data-import="{ordinary["import_id"]}"]')
            expect(empty_review).to_contain_text("새 현장 접수나 추가 품목이 없습니다")
            expect(empty_review.locator(".field-review-item")).to_have_count(0)
            expect(empty_review).not_to_contain_text("null")

            # 닫힌 이전 조회가 늦게 도착해도 새 목록을 덮지 않고, 화면 이동 뒤에도 끼어들지 않는다.
            payload = api(f"imports/{import_id}/field-review")
            page.reload()
            recent_row = page.locator(f'#recent-imports tr[data-import="{import_id}"]')
            held = []
            page.route(path, lambda route: held.append(route), times=1)
            toggle = recent_row.get_by_role("button", name="현장 내용 확인", exact=True)
            toggle.click()
            expect(recent_row.locator(".field-review")).to_contain_text("불러오는 중")
            toggle.click()
            toggle.click()
            expect(recent_row.locator(f'a[href="#/request/{new_id}"]')).to_be_visible()
            assert len(held) == 1
            held[0].fulfill(content_type="application/json", body=json.dumps({"import_id": import_id, "items": [], "counts": {}}))
            expect(recent_row.locator(f'a[href="#/request/{new_id}"]')).to_be_visible()
            toggle.click()
            held.clear()
            page.route(path, lambda route: held.append(route), times=1)
            toggle.click()
            expect(recent_row.locator(".field-review")).to_contain_text("불러오는 중")
            page.evaluate("location.hash = '/home'")
            expect(page.get_by_role("heading", name="오늘 현황", exact=True)).to_be_visible()
            held[0].fulfill(content_type="application/json", body=json.dumps(payload))
            expect(page.locator(".field-review")).to_have_count(0)

            # 화면에서 빠진 충돌함과 같은 계정의 이전 인증 응답도 입력·목록을 바꾸지 않는다.
            for changed_session in (False, True):
                page.goto(url + "#/results")
                reason = page.locator("#conflicts").get_by_label("판단 사유")
                reason.fill("가상 이전 응답 보호 사유")
                page.evaluate("window.__heldConflictCard = document.querySelector('#conflicts')")
                before_conflicts = page.locator("#conflicts").text_content()
                held_conflicts.clear()
                page.route("**/api/conflicts", lambda route: held_conflicts.append(route), times=1)
                page.locator(".conf-tabs .qchip", has_text="충돌").click()
                assert len(held_conflicts) == 1
                if changed_session:
                    page.evaluate("async () => { const C = await import('/core.js'); C.setBoot(C.boot, {sessionChanged: true}); }")
                else:
                    page.evaluate("location.hash = '/home'")
                    expect(page.get_by_role("heading", name="오늘 현황", exact=True)).to_be_visible()
                with page.expect_response(lambda response: response.url.endswith("api/conflicts")) as old_conflicts:
                    held_conflicts[0].fulfill(content_type="application/json", body="[]")
                old_conflicts.value.body()
                page.evaluate("() => new Promise(requestAnimationFrame)")
                assert page.evaluate("window.__heldConflictCard.textContent") == before_conflicts
                assert page.evaluate("window.__heldConflictCard.querySelector('input').value") == "가상 이전 응답 보호 사유"

            # FIELD 계정은 기존 코드만 보며 사무실 전용 현장 상세 API를 호출하지 않는다.
            api("users", {"username": "field_only", "display_name": "가상 수거팀", "password": "field-only-pass-123",
                          "role": "FIELD", "crew_id": crew["id"]})
            field_context = browser.new_context(service_workers="block")
            try:
                response = field_context.request.post(url + "api/login", headers={"X-DG-CSRF": "1"}, data={
                    "username": "field_only", "password": "field-only-pass-123"})
                assert response.ok, response.text()
                response = field_context.request.post(url + "api/password", headers={"X-DG-CSRF": "1"}, data={
                    "old": "field-only-pass-123", "password": "field-only-new-pass-123"})
                assert response.ok, response.text()
                field_page = field_context.new_page()
                reads = []
                field_page.on("request", lambda request: reads.append(request.url) if "/field-review" in request.url else None)
                field_page.goto(url + "#/results")
                expect(field_page.locator("#recent-imports tbody tr")).not_to_have_count(0)
                expect(field_page.get_by_role("button", name="현장 내용 확인", exact=True)).to_have_count(0)
                expect(field_page.locator(".field-review")).to_have_count(0)
                field_page.get_by_label("결과·사진 파일 (여러 개)").set_input_files(upload)
                expect(field_page.locator("#import-run .imp-row")).to_contain_text("이미 가져온 파일 (변경 없음)")
                expect(field_page.locator("#import-run .code-kind")).to_have_text("D-")
                expect(field_page.locator(".field-review")).to_have_count(0)
                assert not reads
            finally:
                field_context.close()
            assert not errors, errors
            print("PASS: real encrypted file/QR FIELD_INTAKE/FIELD_ITEM_ADD, conflict, duplicate, history retry, failed file, stale reads/session, FIELD role")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
