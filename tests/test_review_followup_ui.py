"""배정 응답·접수 초안 수명 회귀: 원본 모듈, Chromium, 가상 자료와 실제 PIN/IndexedDB.

배정 API만 가상 응답으로 제어한다. 실제 PC DB 변경이나 실제 휴대폰 시험은 아니다.
"""
from contextlib import contextmanager
from functools import partial
import http.server
import os
from pathlib import Path
import threading
import unittest

from playwright.sync_api import expect, sync_playwright

from test_review_ui import PIN, mobile_server
from test_workflow_mobile import open_mobile, seed

ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def source_server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                          ".js": "text/javascript", ".mjs": "text/javascript"}

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        request_queue_size = 128

    server = Server(("127.0.0.1", 0), partial(Quiet, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


DISPATCH_FIXTURE = r"""async () => {
  const C = await import('/pc/ui/core.js'); window.__C = C;
  C.setBoot({today:'2026-11-09',me:{username:'fixture',role:'ADMIN',prefs:{}},
    settings:{depot:{lat:35.1,lon:126.9}}, dongs:[],
    crews:['c1','c2'].map((id,i)=>({id,name:`가상${i+1}팀`,active:1,capacity_m3:8,
      shift_start:'09:00',shift_end:'17:00',device_id:`fixture-${id}`}))});
  window.__calls = []; window.__gates = []; window.__hold = null; window.__fail = null;
  window.__stops = ['current-1','current-2'];
  const realFetch = window.fetch;
  window.fetch = async (path, opts={}) => {
    if (!String(path).startsWith('/api/')) return realFetch(path, opts);
    const u = new URL(path, location.origin), endpoint = u.pathname.slice(5);
    const body = opts.body ? JSON.parse(opts.body) : null;
    __calls.push({endpoint,search:u.search,body});
    const date = body?.date || u.searchParams.get('date') || '2026-11-09';
    let out = [];
    if (endpoint === 'route') {
      const id = `route-${date}`;
      out = {routes:[{crew_id:body.crew_ids[0],crew_name:'가상1팀',capacity_m3:8,
        stops:[{request_id:id,eta:'09:10'}]}],unassigned:[],
        requests:{[id]:{name:`${date} 계산`,address:'가상길',m3:1,appt:[],items:[],lat:null}}};
    } else if (endpoint === 'assign/current') {
      out = [{crew_id:'c1',work_date:date,assign_ver:1,
        stops:__stops.map((id,i)=>({request_id:id,receipt_no:`DG-${id}`,seq:i+1}))}];
    } else if (endpoint === 'batches') {
      out = [{status:'ACTIVE',crew_id:'c1',bundles:[],stops:__stops.map(id=>({request_id:id,
        name:`가상 ${id}`,address:'가상길',receipt_no:`DG-${id}`}))}];
    } else if (endpoint === 'requests') out = {rows:[]};
    else if (endpoint === 'assign') out = [{count:1,assign_ver:1}];
    else if (endpoint === 'assign/edit') {
      if (body.op === 'reorder') __stops = body.order;
      out = {resend_needed:['c1']};
    }
    const json = JSON.stringify(out);
    const fail = __fail === endpoint; if (fail) __fail = null;
    if (__hold === endpoint) {
      __hold = null;
      await new Promise(resolve=>__gates.push(resolve));
    }
    if (fail) throw new TypeError('이전 요청의 가상 오류');
    return new Response(json,{headers:{'content-type':'application/json'}});
  };
  window.__D = await import('/pc/ui/screens/dispatch.js');
  window.__mount = async () => document.querySelector('#app').replaceChildren(await __D.screenDispatch());
  await __mount();
}"""


class DispatchResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = source_server()
        cls.url = cls.server.__enter__()
        cls.playwright = sync_playwright().start()
        options = {"headless": True}
        if os.environ.get("CHROME"):
            options["executable_path"] = os.environ["CHROME"]
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.__exit__(None, None, None)

    def setUp(self):
        self.context = self.browser.new_context(service_workers="block")
        self.addCleanup(self.context.close)
        self.context.route("https://**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.route("**/fixture.html", lambda route: route.fulfill(
            content_type="text/html", body="<link rel='stylesheet' href='/pc/ui/pc.css'><main id='app'></main>"))
        self.page.goto(self.url + "fixture.html#/dispatch")
        self.page.evaluate(DISPATCH_FIXTURE)

    def hold(self, endpoint, button):
        self.page.evaluate("endpoint => { __hold = endpoint; }", endpoint)
        self.page.get_by_role("button", name=button, exact=True).click()
        self.page.wait_for_function("__gates.length === 1")

    def release(self):
        self.page.evaluate("() => { __gates.splice(0).forEach(release=>release()); }")
        self.page.wait_for_function("__C.pendingActions.size === 0")

    def change_date(self, date="2026-11-10"):
        self.page.get_by_label("수거일", exact=True).fill(date)

    def state(self):
        return self.page.evaluate("({plan:__D.screenDispatch.s.plan?.date || null, edit:__D.screenDispatch.s.edit?.date || null})")

    def test_pending_route_cannot_be_relabelled_as_the_changed_date(self):
        self.hold("route", "노선 계산")
        self.change_date()
        self.release()
        self.assertEqual(self.state(), {"plan": None, "edit": None})
        expect(self.page.get_by_role("button", name="배정 확정", exact=True)).to_have_count(0)

    def test_pending_current_assignment_cannot_be_relabelled_as_changed_date(self):
        self.hold("assign/current", "현재 배정 불러오기")
        self.change_date()
        self.release()
        self.assertEqual(self.state(), {"plan": None, "edit": None})
        expect(self.page.get_by_text("DG-current-1", exact=False)).to_have_count(0)

    def test_pending_route_is_discarded_after_crew_selection_changes(self):
        self.hold("route", "노선 계산")
        self.page.locator(".crewpick input[type=checkbox]").nth(1).uncheck()
        self.release()
        self.assertIsNone(self.state()["plan"])

    def test_pending_current_assignment_is_discarded_after_crew_selection_changes(self):
        self.hold("assign/current", "현재 배정 불러오기")
        self.page.locator(".crewpick input[type=checkbox]").nth(1).uncheck()
        self.release()
        self.assertIsNone(self.state()["edit"])

    def test_old_view_route_does_not_change_state_after_dispatch_reentry(self):
        self.hold("route", "노선 계산")
        self.page.evaluate("() => { location.hash='#/home'; document.querySelector('#app').replaceChildren(); }")
        self.page.evaluate("() => new Promise(requestAnimationFrame)")
        self.page.evaluate("() => { location.hash='#/dispatch'; }")
        self.page.evaluate("() => new Promise(requestAnimationFrame)")
        self.page.evaluate("__mount()")
        self.release()
        self.assertIsNone(self.state()["plan"])
        self.page.get_by_role("button", name="노선 계산", exact=True).click()
        self.page.wait_for_function("__C.pendingActions.size === 0")
        self.assertEqual(self.state()["plan"], "2026-11-09")

    def test_later_current_load_wins_over_pending_route(self):
        self.hold("route", "노선 계산")
        self.page.get_by_role("button", name="현재 배정 불러오기", exact=True).click()
        self.page.wait_for_function("__D.screenDispatch.s.edit !== null && __D.screenDispatch.s.edit !== undefined")
        self.release()
        self.assertEqual(self.state(), {"plan": None, "edit": "2026-11-09"})

    def test_later_route_wins_over_pending_current_load(self):
        self.hold("assign/current", "현재 배정 불러오기")
        self.page.get_by_role("button", name="노선 계산", exact=True).click()
        self.page.wait_for_function("__D.screenDispatch.s.plan !== null")
        self.release()
        self.assertEqual(self.state(), {"plan": "2026-11-09", "edit": None})

    def test_obsolete_route_error_does_not_replace_current_message(self):
        self.page.evaluate("__fail='route'")
        self.hold("route", "노선 계산")
        self.change_date()
        self.release()
        expect(self.page.get_by_text("이전 요청의 가상 오류", exact=True)).to_have_count(0)
        self.page.get_by_role("button", name="노선 계산", exact=True).click()
        self.page.wait_for_function("__C.pendingActions.size === 0")
        self.assertEqual(self.state()["plan"], "2026-11-10")

    def test_normal_route_can_be_confirmed_for_its_original_date(self):
        self.page.get_by_role("button", name="노선 계산", exact=True).click()
        self.page.wait_for_function("__C.pendingActions.size === 0")
        self.page.get_by_role("button", name="배정 확정", exact=True).click()
        self.page.wait_for_function("__calls.some(call=>call.endpoint==='assign')")
        body = self.page.evaluate("__calls.find(call=>call.endpoint==='assign').body")
        self.assertEqual(body["date"], "2026-11-09")
        self.assertEqual(body["plan"]["c1"][0]["request_id"], "route-2026-11-09")

    def test_date_change_during_confirmation_lookup_prevents_assignment(self):
        self.page.get_by_role("button", name="노선 계산", exact=True).click()
        self.page.wait_for_function("__C.pendingActions.size === 0")
        self.hold("batches", "배정 확정")
        self.change_date()
        self.release()
        self.assertFalse(self.page.evaluate("__calls.some(call=>call.endpoint==='assign')"))

    def test_completed_route_crew_change_requires_recalculation(self):
        self.page.get_by_role("button", name="노선 계산", exact=True).click()
        self.page.wait_for_function("__C.pendingActions.size === 0")
        self.page.locator(".crewpick input[type=checkbox]").nth(1).uncheck()
        expect(self.page.get_by_role("button", name="배정 확정", exact=True)).to_be_disabled()

    def test_normal_current_assignment_reordering_keeps_date_and_version(self):
        self.page.get_by_role("button", name="현재 배정 불러오기", exact=True).click()
        self.page.wait_for_function("__C.pendingActions.size === 0")
        self.page.get_by_role("button", name="가상1팀 1번 아래로", exact=True).click()
        self.page.wait_for_function("__C.pendingActions.size === 0")
        body = self.page.evaluate("__calls.find(call=>call.endpoint==='assign/edit').body")
        self.assertEqual(body["date"], "2026-11-09")
        self.assertEqual(body["assign_ver"], {"c1": 1})
        self.assertEqual(body["order"], ["current-2", "current-1"])


class MobileDraftLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mobile_server()
        cls.url = cls.server.__enter__()
        cls.playwright = sync_playwright().start()
        options = {"headless": True}
        if os.environ.get("CHROME"):
            options["executable_path"] = os.environ["CHROME"]
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.__exit__(None, None, None)

    def setUp(self):
        self.context, self.page = open_mobile(self.browser, self.url)
        self.addCleanup(self.context.close)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        seed(self.page, {"mode": "SOLO", "requests": [], "events": [], "intakeDraft": {"name": "가상 기존 초안"}}, registered=False)
        self.page.evaluate("async () => { const C=await import('./core.js'); location.hash='#/intake'; await C.render(); }")
        expect(self.page.get_by_label("이름", exact=True)).to_have_value("가상 기존 초안")
        self.page.clock.install()

    def advance(self):
        try:
            self.page.clock.fast_forward(601)
        except Exception as error:
            self.fail(str(error))
        self.page.evaluate("async () => { await (await import('./core.js')).saving; }")
        self.assertEqual(self.errors, [])

    def draft(self):
        return self.page.evaluate("async () => (await import('./core.js')).st?.intakeDraft || null")

    def test_valid_pending_draft_is_saved_and_restored(self):
        self.page.get_by_label("이름", exact=True).fill("가상 보존 초안")
        self.advance()
        self.assertEqual(self.draft()["name"], "가상 보존 초안")
        self.page.evaluate("async () => { const C=await import('./core.js'); C.ui.form=null; await C.render(); }")
        expect(self.page.get_by_label("이름", exact=True)).to_have_value("가상 보존 초안")
        stored = self.page.evaluate("async () => (await (await import('./store.js')).getSecure('state')).intakeDraft.name")
        self.assertEqual(stored, "가상 보존 초안")

    def test_clear_cancels_pending_write_and_draft_stays_empty(self):
        self.page.get_by_label("이름", exact=True).fill("가상 지운 초안")
        self.page.get_by_role("button", name="지우고 새로 쓰기", exact=True).click()
        expect(self.page.get_by_label("이름", exact=True)).to_have_value("")
        self.advance()
        self.assertIsNone(self.draft())
        self.page.evaluate("async () => { const C=await import('./core.js'); C.ui.form=null; await C.render(); }")
        expect(self.page.get_by_label("이름", exact=True)).to_have_value("")

    def test_pending_draft_is_settled_before_lock_and_never_writes_locked_state(self):
        self.page.get_by_label("이름", exact=True).fill("가상 잠금 전 초안")
        self.page.evaluate("async () => { await (await import('./core.js')).lockNow(); }")
        self.advance()
        stored = self.page.evaluate("""async pin => {
            const S=await import('./store.js'); await S.unlock(pin); return (await S.getSecure('state')).intakeDraft;
        }""", PIN)
        self.assertEqual(stored["name"], "가상 잠금 전 초안")

    def test_old_form_timer_cannot_overwrite_replaced_state(self):
        self.page.get_by_label("이름", exact=True).fill("가상 옛 양식")
        self.page.evaluate("""async () => {
            const C=await import('./core.js');
            C.setSt({...C.emptyState(),mode:'SOLO',intakeDraft:{name:'가상 새 상태'}});
            C.ui.form=null; await C.commit(); await C.render();
        }""")
        self.advance()
        self.assertEqual(self.draft()["name"], "가상 새 상태")

    def test_cancel_preserves_the_last_pending_valid_draft(self):
        self.page.get_by_label("이름", exact=True).fill("가상 취소 전 초안")
        self.page.get_by_role("button", name="← 취소", exact=True).click()
        self.advance()
        self.assertEqual(self.draft()["name"], "가상 취소 전 초안")

    def test_successful_registration_does_not_resurrect_its_pending_draft(self):
        self.page.evaluate("""async () => {
            const C=await import('./core.js'), {BUILTIN_FEES}=await import('./fees-data.js');
            Object.assign(C.ui.form,{phone:'01000005555',address:'광주광역시 동구 서남로 1',dong:'서남동',
                addrAck:true,planned_date:C.todayKST(1),items:[C.lineFromRow(BUILTIN_FEES.rows.find(row=>row.status==='CONFIRMED'),1,BUILTIN_FEES)]});
            await C.render();
        }""")
        self.page.get_by_label("이름", exact=True).fill("가상 정상 등록")
        self.page.evaluate("async () => { (await import('./core.js')).ui.form.addrAck=true; }")
        self.page.get_by_role("button", name="접수 등록", exact=True).click()
        self.page.wait_for_function("import('./core.js').then(C=>C.st.requests.length===1 && C.ui.form===null)")
        self.advance()
        self.assertIsNone(self.draft())

    def test_invalid_registration_keeps_the_pending_draft(self):
        self.page.get_by_label("이름", exact=True).fill("가상 입력 보완")
        self.page.get_by_role("button", name="접수 등록", exact=True).click()
        expect(self.page.get_by_text("연락처를 확인해 주세요. (0으로 시작하는 9~11자리)", exact=True)).to_be_visible()
        self.advance()
        self.assertEqual(self.draft()["name"], "가상 입력 보완")


if __name__ == "__main__":
    unittest.main()
