"""PC 화면 검토 결함 11건 회귀: 실제 임시 PC 서버 + Chromium, 가상 자료만.

서버 응답 지연(대기 문)·웹캠(가짜 캔버스 영상)·단독 백업 응답만 화면 안 fetch/getUserMedia 대역으로
제어한다. 권한은 실제 서버 계정(관리자·접수·수거팀·팀장)으로 확인한다.
DG_REVIEW_KEEP_GOING=1 이면 실패한 절을 기록하고 다음 절을 이어 본다(재현 확인용).
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import traceback
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import pc_server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
import dgproto as P  # noqa: E402

PW = "review-fixture-password-123"
NEW_PW = PW + "-new"
KEEP_GOING = os.environ.get("DG_REVIEW_KEEP_GOING") == "1"
PASSED = 0
FAILED: list[str] = []


def ok(value, message):
    global PASSED
    if not value:
        raise AssertionError("실패: " + message)
    PASSED += 1
    print("  ✓", message, flush=True)


def api(ctx, url, path, body=None):
    options = {"headers": {"X-DG-CSRF": "1"}, "timeout": 30000}
    r = ctx.request.get(url + "api/" + path, **options) if body is None else ctx.request.post(url + "api/" + path, data=body, **options)
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


# 화면 안 fetch 대역: 실제 서버로 보내고, 필요한 경로만 응답을 늦추거나(hold) 바꾸거나(patch) 가짜로(fake) 돌려준다.
HOOK = r"""() => {
  if (window.__hooked) return; window.__hooked = true;
  window.__calls = []; window.__gates = []; window.__hold = new Set(); window.__patch = {}; window.__fake = {};
  const real = window.fetch.bind(window);
  window.fetch = async (path, opts = {}) => {
    const s = String(path);
    if (!s.startsWith('/api/')) return real(path, opts);
    const u = new URL(s, location.origin), ep = u.pathname.slice(5), method = (opts.method || 'GET').toUpperCase();
    const key = method + ' ' + ep;
    const call = { key, search: u.search, headers: { ...(opts.headers || {}) } };
    if (opts.body instanceof Uint8Array) call.bytes = Array.from(opts.body);
    __calls.push(call);
    let res;
    if (__fake[key]) res = new Response(JSON.stringify(__fake[key](call)), { headers: { 'content-type': 'application/json' } });
    else res = await real(path, opts);
    call.status = res.status;
    if (__patch[key] && res.ok) {
      const data = __patch[key](await res.json());
      res = new Response(JSON.stringify(data), { status: res.status, headers: { 'content-type': 'application/json' } });
    }
    if (__hold.has(key)) { __hold.delete(key); await new Promise((r) => __gates.push(r)); }
    return res;
  };
}"""

# 웹캠 대역: 캔버스 영상(QR 없음)을 돌려주고 만든 영상을 모두 기록한다.
CAMERA = r"""() => {
  window.__streams = [];
  const fake = async () => {
    const c = document.createElement('canvas'); c.width = 64; c.height = 48;
    const g = c.getContext('2d'); g.fillStyle = '#888'; g.fillRect(0, 0, 64, 48);
    const s = c.captureStream(10); __streams.push(s); return s;
  };
  if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = fake;
  else Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: fake } });
}"""
LIVE = "() => __streams.filter((s) => s.getTracks().some((t) => t.readyState === 'live')).length"


def new_context(browser):
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block", accept_downloads=True)
    ctx.add_init_script(f"({HOOK})()")  # 첫 화면의 첫 요청부터 기록한다
    ctx.route("https://tiles.openfreemap.org/**", lambda route: route.fulfill(json={
        "version": 8, "sources": {}, "layers": [{"id": "background", "type": "background", "paint": {"background-color": "#dde6e8"}}]}))
    return ctx


def fresh(page, url, hash_):
    """페이지를 새로 읽어 화면 모듈 상태를 비운 뒤 지정 화면을 연다."""
    page.goto("about:blank")
    page.goto(url + hash_)
    expect(page.locator(".shell main h1").first).to_be_visible()


def wait_js(page, expr, timeout=10000):
    """PC 화면 CSP(script-src 'self')는 wait_for_function의 문자열 평가를 막으므로 evaluate로 직접 기다린다."""
    for _ in range(max(1, timeout // 50)):
        if page.evaluate(expr):
            return
        page.wait_for_timeout(50)
    raise AssertionError("기다린 조건이 시간 안에 참이 되지 않음: " + expr)


def wait_gate(page):
    wait_js(page, "() => __gates.length === 1")


def release(page):
    page.evaluate("() => __gates.splice(0).forEach((r) => r())")


def section(name, fn, *args):
    print(f"[{name}]", flush=True)
    try:
        fn(*args)
    except Exception:
        if not KEEP_GOING:
            raise
        FAILED.append(name)
        print("  ✗", name, "\n", traceback.format_exc(limit=3), flush=True)


# ------------------------------------------------------------------ 12. 다른 PC의 첫 화면: 관리자 계정 만들기 양식을 보이지 않음
def remote_first_setup(browser, url):
    ctx = new_context(browser)
    try:
        # 서버는 다른 PC의 요청에 setup_here:false 를 더한다(본체 판정은 test_desktop_review_fixes 가 검사). 여기서는 그 응답을 흉내 낸다.
        ctx.route("**/api/state", lambda route: route.fulfill(json={"setup_needed": True, "logged_in": False, "setup_here": False}))
        page = ctx.new_page()
        page.goto(url)
        card = page.locator(".login")
        expect(card).to_contain_text("서버 PC 본체")
        ok(True, "다른 PC에는 첫 관리자 설정을 본체에서 하라고 안내")
        ok(card.locator("input").count() == 0 and page.get_by_role("button", name="관리자 계정 만들기").count() == 0,
           "다른 PC에는 아이디·비밀번호 입력칸과 [관리자 계정 만들기] 단추가 없음")
        expect(page.get_by_role("button", name="다시 확인", exact=True)).to_be_visible()
        ok(True, "본체에서 만든 뒤 [다시 확인]으로 로그인 화면을 불러올 수 있음")
    finally:
        ctx.close()


# ------------------------------------------------------------------ 1. 모달 진행 중 취소 → 개인키 유실
def backup_key_modal(page, url):
    fresh(page, url, "#/settings/backup")
    page.get_by_role("button", name="백업 키 만들기", exact=True).click()
    dlg = page.get_by_role("dialog")
    dlg.get_by_label("개인키 보호 비밀번호 (12자 이상)", exact=True).fill("fixture-key-password-1")
    dlg.get_by_label("한 번 더", exact=True).fill("fixture-key-password-1")
    page.evaluate("() => __hold.add('POST backup-key')")
    dlg.get_by_role("button", name="만들고 개인키 내려받기", exact=True).click()
    wait_gate(page)
    page.keyboard.press("Escape")
    page.mouse.click(5, 5)  # 대화 상자 바깥(어두운 배경)
    expect(dlg).to_be_visible()
    expect(dlg.get_by_role("button", name="취소", exact=True)).to_be_disabled()
    expect(dlg.get_by_role("status")).to_contain_text("처리 중")
    ok(True, "백업 키 요청 진행 중에는 Esc·바깥 클릭·[취소]로 닫히지 않고 진행 중을 알린다")
    with page.expect_download() as info:
        release(page)
    ok(info.value.suggested_filename.startswith("donggu-backup-key-"), "응답이 오면 한 번만 주는 개인키 파일을 내려받는다")
    expect(dlg).to_have_count(0)
    expect(page.locator(".toast").filter(has_text="백업 키를 만들었습니다")).to_be_visible()
    ok(True, "개인키를 받은 뒤 대화 상자가 닫히고 완료를 알린다")


# ------------------------------------------------------------------ 13. 응답이 오지 않는 처리 중 대화 상자: 기다리지 않고 닫을 길
def stuck_modal(admin_ctx, url):
    page = admin_ctx.new_page()
    try:
        page.clock.install()  # 시간은 그대로 흐르고, 필요할 때 앞으로 건너뛴다
        fresh(page, url, "#/settings/backup")
        page.get_by_role("button", name="백업 키 만들기", exact=True).click()
        dlg = page.get_by_role("dialog")
        dlg.get_by_label("개인키 보호 비밀번호 (12자 이상)", exact=True).fill("fixture-key-password-2")
        dlg.get_by_label("한 번 더", exact=True).fill("fixture-key-password-2")
        page.evaluate("() => __hold.add('POST backup-key')")  # 응답이 끝내 오지 않는 요청
        dlg.get_by_role("button", name="만들고 개인키 내려받기", exact=True).click()
        wait_gate(page)
        escape = dlg.get_by_role("button", name="기다리지 않고 닫기", exact=True)
        page.clock.fast_forward("00:20")
        ok(escape.count() == 0 or not escape.is_visible(), "처리 중 30초 전에는 [기다리지 않고 닫기]가 없음(응답을 기다림)")
        page.clock.fast_forward("00:11")
        expect(escape).to_be_visible()
        expect(dlg.get_by_role("status")).to_contain_text("새로 고쳐")
        ok(True, "30초가 지나도 응답이 없으면 [기다리지 않고 닫기]와 결과 확인 방법을 보임")
        escape.click()
        expect(dlg).to_have_count(0)
        ok(True, "[기다리지 않고 닫기]로 대화 상자를 닫고 화면을 계속 쓸 수 있음")
    finally:
        page.close()  # 붙잡은 요청은 보내지 않은 채 버린다


# ------------------------------------------------------------------ 2. 일괄 취소 진행 중 취소 → 결과 누락
def bulk_cancel(page, url, receipts):
    fresh(page, url, "#/requests")
    for no in receipts:
        page.get_by_label(f"{no} 고르기", exact=True).check()
    expect(page.get_by_text(f"{len(receipts)}건 선택", exact=True)).to_be_visible()
    page.locator(".selbar").get_by_role("button", name="취소", exact=True).click()
    dlg = page.get_by_role("dialog")
    code = dlg.locator("select").first
    value = code.locator("option").nth(1).get_attribute("value")
    code.select_option(value)
    dlg.get_by_label("내용", exact=True).fill("가상 일괄 취소")
    page.evaluate("() => __hold.add('POST requests/bulk')")
    dlg.get_by_role("button", name="취소 기록", exact=True).click()
    wait_gate(page)
    page.keyboard.press("Escape")
    expect(dlg).to_be_visible()
    release(page)
    expect(page.locator(".toast").filter(has_text=f"{len(receipts)}건 취소했습니다")).to_be_visible()
    expect(page.get_by_text(f"{len(receipts)}건 선택", exact=True)).to_have_count(0)
    ok(True, "일괄 취소 진행 중 Esc를 눌러도 결과 알림·선택 해제·목록 새로 고침이 이어진다")


# ------------------------------------------------------------------ 6. 설정 저장(일반 재조회)에서 동별 초안·필터 보존
def dong_drafts(page, url):
    fresh(page, url, "#/dongs")
    search = page.get_by_label("접수 검색어", exact=True)
    search.fill("가상검토")
    create = page.get_by_role("button", name="접수 등록", exact=True)
    expect(create).to_be_enabled()
    create.click()
    form = page.get_by_role("region", name="동별 접수 등록", exact=True)
    form.get_by_label("이름", exact=True).fill("가상 초안 주민")
    page.get_by_role("button", name="목록으로 · 초안 보관", exact=True).click()
    page.evaluate("location.hash = '#/settings/capacity'")
    page.get_by_role("button", name="저장", exact=True).click()
    expect(page.locator(".toast").filter(has_text="저장했습니다")).to_be_visible()
    page.evaluate("location.hash = '#/dongs'")
    expect(page.get_by_label("접수 검색어", exact=True)).to_have_value("가상검토")
    page.get_by_role("button", name="접수 등록", exact=True).click()
    expect(page.get_by_role("region", name="동별 접수 등록", exact=True).get_by_label("이름", exact=True)).to_have_value("가상 초안 주민")
    ok(True, "설정 저장 뒤 일반 재조회는 동별 검색 조건과 작성 중 초안을 보존한다")
    page.get_by_role("button", name="목록으로 · 초안 보관", exact=True).click()
    # 재인증 취소(인증 경계)는 예전처럼 주민 초안을 비운다.
    page.evaluate("async () => { const C = await import('/core.js'); C.setBoot(null, { sessionChanged: true }); await C.render(); }")
    expect(page.get_by_label("접수 검색어", exact=True)).to_have_value("")
    ok(True, "인증 경계(재인증 취소·로그아웃)에서는 동별 초안·조건을 비운다")


# ------------------------------------------------------------------ 4. 설정>기기 웹캠 [읽기 멈추기]
def device_camera_stop(page, url):
    fresh(page, url, "#/settings/devices")
    page.evaluate(CAMERA)
    page.get_by_role("button", name="휴대폰 QR 웹캠으로 읽기", exact=True).click()
    stop = page.get_by_role("button", name="읽기 멈추기", exact=True)
    expect(stop).to_be_visible()
    wait_js(page, f"() => ({LIVE})() === 1", 3000)
    expect(stop).to_be_enabled(timeout=3000)
    stop.click()
    wait_js(page, f"() => ({LIVE})() === 0", 3000)
    expect(page.locator("video.regcam")).to_be_hidden()
    expect(page.get_by_role("button", name="휴대폰 QR 웹캠으로 읽기", exact=True)).to_be_enabled()
    ok(page.evaluate("__streams.length") == 1, "기기 등록 웹캠은 [읽기 멈추기]로 카메라를 끄고 다시 시작할 수 있다")


# ------------------------------------------------------------------ 5. 결과 가져오기 웹캠 중복 시작
def results_camera(page, url):
    fresh(page, url, "#/results")
    page.evaluate(CAMERA)
    start = page.get_by_role("button", name="웹캠으로 읽기", exact=True)
    start.click()
    wait_js(page, f"() => ({LIVE})() === 1", 3000)
    page.wait_for_timeout(300)
    expect(start).to_be_disabled()
    start.click(force=True)
    page.wait_for_timeout(300)
    ok(page.evaluate("__streams.length") == 1, "웹캠을 읽는 동안에는 [웹캠으로 읽기]가 다시 카메라를 열지 않는다")
    page.get_by_role("button", name="멈추기", exact=True).click()
    wait_js(page, f"() => ({LIVE})() === 0", 3000)
    expect(start).to_be_enabled()
    start.click()
    wait_js(page, f"() => ({LIVE})() === 1", 3000)
    page.get_by_role("button", name="멈추기", exact=True).click()
    wait_js(page, f"() => ({LIVE})() === 0", 3000)
    ok(page.evaluate("__streams.length") == 2, "[멈추기]가 열린 카메라를 모두 끄고 다시 시작은 새 영상 하나만 연다")


# ------------------------------------------------------------------ 7. 단독 백업 미리보기와 실제 옮기기 파일 일치
SOLO_FAKE = r"""() => {
  __fake['POST import/solo'] = (call) => call.search.includes('preview=1')
    ? { created_at: '2026-11-01T00:00:00Z', requests: 1, new: 1, already: 0, events: 0, photos: 0, unloads: 0, invalid: 0, errors: [] }
    : { requests: 1, events: 0, photos_recovered: 0, unloads_applied: 0, renamed: 0, invalid: 0 };
}"""


def solo_preview(page, url):
    fresh(page, url, "#/results")
    page.evaluate(SOLO_FAKE)
    card = page.locator(".card").filter(has=page.get_by_role("heading", name="휴대폰 단독 방식 자료 옮기기 (.dgb)", exact=True))
    file_in, pw = card.locator('input[type="file"]'), card.get_by_placeholder("휴대폰 백업 비밀번호", exact=True)
    move = card.get_by_role("button", name="PC로 옮기기", exact=True)
    preview = card.get_by_role("button", name="미리보기", exact=True)

    def pick(name, data):
        file_in.set_input_files({"name": name, "mimeType": "application/octet-stream", "buffer": data})

    pick("a.dgb", b"AAAA-solo")
    pw.fill("pass-a")
    preview.click()
    expect(move).to_be_visible()
    pick("b.dgb", b"BBBB-solo")
    expect(move).to_have_count(0)
    ok(True, "파일을 바꾸면 이전 미리보기와 [PC로 옮기기]를 지운다")
    preview.click()
    expect(move).to_be_visible()
    pw.fill("pass-b")
    expect(move).to_have_count(0)
    ok(True, "비밀번호를 바꾸면 이전 미리보기와 [PC로 옮기기]를 지운다")
    # 미리보기 응답을 기다리는 동안 입력이 바뀌면 늦은 미리보기를 보이지 않는다.
    page.evaluate("() => __hold.add('POST import/solo')")
    preview.click()
    wait_gate(page)
    pick("c.dgb", b"CCCC-solo")
    release(page)
    page.wait_for_timeout(300)
    expect(move).to_have_count(0)
    ok(True, "미리보기 중 입력이 바뀌면 늦게 온 미리보기를 버린다")
    pick("d.dgb", b"DDDD-solo")
    pw.fill("pass-d")
    preview.click()
    expect(move).to_be_visible()
    page.evaluate("() => { __calls.length = 0; }")
    move.click()
    expect(page.locator(".toast").filter(has_text="접수 1건")).to_be_visible()
    sent = page.evaluate("() => __calls.filter((c) => c.key === 'POST import/solo')")
    ok(len(sent) == 1 and "preview=0" in sent[0]["search"] and bytes(sent[0]["bytes"]) == b"DDDD-solo"
       and base64.b64decode(sent[0]["headers"]["X-DG-Pass"]) == b"pass-d", "[PC로 옮기기]는 미리보기한 그 파일·비밀번호만 보낸다")


# ------------------------------------------------------------------ 3·10. 수거팀: 달력 조회 없음, 단독 옮기기 카드 없음, 하차 입력 있음
def field_detail(page, url, rid):
    fresh(page, url, f"#/request/{rid}")
    expect(page.get_by_role("heading", name="일정·우선순위", exact=True)).to_be_visible()
    page.wait_for_timeout(600)
    calls = page.evaluate("() => __calls.map((c) => c.key + ' ' + c.status)")
    ok(not any(c.startswith("GET calendar") for c in calls), "수거팀 접수 상세는 권한 없는 달력(calendar)을 부르지 않는다")
    expect(page.locator(".toast.bad")).to_have_count(0)
    expect(page.locator(".calwrap")).to_have_count(0)
    ok(True, "수거팀 접수 상세는 달력 영역을 숨기고 권한 오류 알림을 띄우지 않는다")


def field_results(page, url):
    fresh(page, url, "#/results")
    expect(page.get_by_role("heading", name="휴대폰 화면 QR로 받기 (웹캠)", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="휴대폰 단독 방식 자료 옮기기 (.dgb)", exact=True)).to_have_count(0)
    ok(True, "수거팀 결과 가져오기에는 단독 방식 옮기기 카드가 없다")
    fresh(page, url, "#/close")
    expect(page.get_by_role("button", name="하차 기록", exact=True)).to_be_visible()
    ok(True, "수거팀은 하차 기록을 입력할 수 있다")


# ------------------------------------------------------------------ 8·9·11. 팀장: 하차 입력·수수료 승인·결과 가져오기 링크 없음
LEAD_PATCH = r"""() => {
  __patch['GET pending'] = (d) => ({ ...d, items: [...(d.items || []).filter((x) => x.key !== 'no_result_crews'),
    { key: 'no_result_crews', count: 1, crews: [{ crew: '가상 미회수팀', crew_id: 'fixture-crew', work_date: '2026-01-05', sent_at: '2026-01-05T00:00:00Z' }] }] });
  __patch['GET close'] = (d) => ({ ...d, crews_no_result: [{ crew: '가상 미회수팀', crew_id: 'fixture-crew', sent_at: '2026-01-05T00:00:00Z' }] });
}"""


def lead_unload(page, url):
    fresh(page, url, "#/close")
    expect(page.get_by_role("heading", name="하차 기록 · 합계", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="하차 기록", exact=True)).to_have_count(0)
    ok(True, "팀장 일일 마감의 하차 기록은 읽기 전용이다(POST unloads 권한 없음)")


def lead_links(page, url):
    fresh(page, url, "#/close")
    page.evaluate(LEAD_PATCH)
    page.evaluate("location.hash = '#/close?date=2026-01-05'")
    expect(page.locator("main .notice strong").get_by_text("결과 미회수 팀", exact=True)).to_be_visible()
    expect(page.locator('main a[href^="#/results"]')).to_have_count(0)
    ok(True, "팀장 마감 화면의 결과 미회수 안내에 갈 수 없는 결과 가져오기 링크가 없다")
    page.evaluate("location.hash = '#/home'")
    todo = page.locator('.dash-backlog a[data-key="no_result_crews"]')
    expect(todo).to_be_visible()
    href = todo.get_attribute("href")
    ok(not href.startswith("#/results"), "팀장 홈의 결과 미회수 팀 항목이 결과 가져오기로 연결되지 않는다")
    todo.click()
    expect(page.get_by_role("heading", name="일일 마감", exact=True)).to_be_visible()
    expect(page.get_by_label("수거일", exact=True)).to_have_value("2026-01-05")
    ok(True, "팀장 홈의 결과 미회수 팀 항목은 그 날짜의 일일 마감 화면으로 이어진다")


def lead_fees(page, url):
    fresh(page, url, "#/fees")
    expect(page.get_by_role("heading", name="승인 대기", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="원문과 대조했고 승인", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="반려", exact=True)).to_have_count(0)
    ok(True, "팀장 수수료표에는 승인·반려 단추가 없다")


def intake_checks(page, url):
    fresh(page, url, "#/fees")
    expect(page.get_by_role("button", name="원문과 대조했고 승인", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="반려", exact=True)).to_be_visible()
    ok(True, "접수 담당자는 다른 사람이 쓴 개정안을 승인·반려할 수 있다")
    fresh(page, url, "#/results")
    expect(page.get_by_role("heading", name="휴대폰 단독 방식 자료 옮기기 (.dgb)", exact=True)).to_be_visible()
    ok(True, "접수 담당자에게는 단독 방식 옮기기 카드가 보인다")
    fresh(page, url, "#/close")
    expect(page.get_by_role("button", name="하차 기록", exact=True)).to_be_visible()
    ok(True, "접수 담당자는 하차 기록을 입력할 수 있다")


def role_page(browser, url, admin_ctx, role, crew=None):
    username = "review" + role.lower()
    api(admin_ctx, url, "users", {"username": username, "display_name": "가상 " + role, "role": role, "password": PW, "crew_id": crew})
    ctx = new_context(browser)
    api(ctx, url, "login", {"username": username, "password": PW})
    api(ctx, url, "password", {"old": PW, "password": NEW_PW})
    page = ctx.new_page()
    page.on("pageerror", lambda error: ERRORS.append(f"{role}: {error}"))
    return ctx, page


ERRORS: list[str] = []


def main():
    output = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-pc-review-fixes-"))
    output.mkdir(parents=True, exist_ok=True)
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        section("12 다른 PC 첫 화면", remote_first_setup, browser, url)  # 관리자를 만들기 전(첫 설정 필요 상태)에 본다
        admin = new_context(browser)
        api(admin, url, "setup", {"username": "review_admin", "display_name": "가상 검토관리자", "password": PW})
        boot = api(admin, url, "bootstrap")
        today = boot["today"]
        raw = os.urandom(16) + P.pub_raw(P.gen_key()) + P.pub_raw(P.gen_key())
        code = "DGD1." + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip("=")
        dev = api(admin, url, "devices/register", {"code": code, "label": "가상 검토폰"})["device_id"]
        api(admin, url, "devices/" + dev + "/confirm", {})
        crew = api(admin, url, "crews", {"name": "가상 검토팀", "device_id": dev})["id"]
        api(admin, url, "dongs", {"name": "가상검토동"})
        page = admin.new_page()
        page.on("pageerror", lambda error: ERRORS.append(f"ADMIN: {error}"))
        page.goto(url + "#/home")
        fee = page.evaluate("async () => (await import('/web/fees-data.js')).BUILTIN_FEES.rows.find(r => r.status === 'CONFIRMED' && r.price > 0).id")
        made = [api(admin, url, "requests", {"name": f"가상 검토{i}", "phone": f"0100000{i:04d}", "address": f"가상구 검토로 {i}",
                                              "planned_date": today, "lat": 35.1, "lon": 126.9, "items": [{"fee_rate_id": fee, "qty": 1}]})
                for i in range(1, 4)]
        api(admin, url, "assign", {"date": today, "plan": {crew: [{"request_id": made[0]["id"], "eta": "09:30"}]}})
        api(admin, url, "fee-revisions", {"effective_from": "2099-01-01", "confirmed_by": "가상 확인자", "overrides": {}, "remove": []})
        receipts = [api(admin, url, "requests/" + m["id"])["receipt_no"] for m in made[1:]]
        try:
            section("1 백업 키 대화 상자", backup_key_modal, page, url)
            section("2 일괄 취소", bulk_cancel, page, url, receipts)
            section("6 동별 초안 보존", dong_drafts, page, url)
            section("4 기기 웹캠 멈추기", device_camera_stop, page, url)
            section("5 결과 웹캠 중복", results_camera, page, url)
            section("7 단독 백업 미리보기", solo_preview, page, url)
            field_ctx, field = role_page(browser, url, admin, "FIELD", crew)
            section("3 수거팀 접수 상세", field_detail, field, url, made[0]["id"])
            section("10 수거팀 결과·하차", field_results, field, url)
            lead_ctx, lead = role_page(browser, url, admin, "LEAD")
            section("8 팀장 하차", lead_unload, lead, url)
            section("11 팀장 결과 링크", lead_links, lead, url)
            section("9 팀장 수수료 승인", lead_fees, lead, url)
            intake_ctx, intake = role_page(browser, url, admin, "INTAKE")
            section("9·10 접수", intake_checks, intake, url)
            section("13 응답 없는 대화 상자", stuck_modal, admin, url)
            ok(not ERRORS, "화면 스크립트 오류 없음: " + "; ".join(ERRORS))
        except BaseException:
            for name, p in [("admin", page), *[(n, v) for n, v in (("field", locals().get("field")), ("lead", locals().get("lead")), ("intake", locals().get("intake"))) if v]]:
                try:
                    p.screenshot(path=str(output / f"review-fixes-{name}.png"), full_page=True)
                except Exception:
                    pass
            raise
        finally:
            browser.close()
    if FAILED:
        raise SystemExit(f"실패한 절: {', '.join(FAILED)}")
    print(f"PC 화면 검토 결함 회귀 {PASSED}개 통과")


if __name__ == "__main__":
    main()
