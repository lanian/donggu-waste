"""보고서·숫자 배출표 화면 회귀: 실제 Chromium + 가상 API, 운영 DB/서버 미사용."""
from contextlib import contextmanager
from functools import partial
import http.server
import os
from pathlib import Path
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
passed = 0


def ok(condition, message):
    global passed
    assert condition, message
    passed += 1
    print("  OK", message, flush=True)


@contextmanager
def fixture_server():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript"}

        def log_message(self, *args):
            pass

        def translate_path(self, path):
            if path.startswith("/fonts/"):
                return str(ROOT / "pc" / "ui" / "fonts" / Path(path).name)
            return super().translate_path(path)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


FIXTURE = r"""async ({screen, role}) => {
  const C = await import('/pc/ui/core.js');
  C.setBoot({today:'2026-11-07',me:{username:'fixture',role,prefs:{}},settings:{},crews:[],dongs:[]});
  window.__calls=[]; window.__gates=[]; window.__hold=null; window.__holdExact=false; window.__fail=null; window.__lose=null; window.__opened=[]; window.__history={};
  window.open=(url) => {window.__opened.push(url); return {};};
  window.__labels=[
    {number:'0001',status:'AVAILABLE',destination:'',request_id:null,line_id:null,received_at:'2026-11-07T01:00:00Z',updated_at:'2026-11-07T01:00:00Z'},
    {number:'0002',status:'DISTRIBUTED',destination:'가상1동',request_id:null,line_id:null,received_at:'2026-11-07T01:00:00Z',updated_at:'2026-11-07T01:00:00Z'},
    {number:'0003',status:'VOID',destination:'',request_id:null,line_id:null,received_at:'2026-11-07T01:00:00Z',updated_at:'2026-11-07T01:00:00Z'}];
  const realFetch=window.fetch;
  window.fetch=async (path,options={}) => {
    if (!String(path).startsWith('/api/')) return realFetch(path,options);
    const u=new URL(path,location.origin), body=options.body ? JSON.parse(options.body) : null;
    window.__calls.push({path:u.pathname,search:u.search,body,method:options.method || 'GET'});
    const endpoint=u.pathname.slice(5);
    const hold=window.__hold && (window.__holdExact ? endpoint===window.__hold : endpoint.startsWith(window.__hold));
    if (hold) {window.__hold=null;window.__holdExact=false;}
    if (window.__fail && endpoint.startsWith(window.__fail)) {
      window.__fail=null; return new Response(JSON.stringify({error:'가상 입력 오류'}),{status:422});
    }
    if (window.__lose && endpoint.startsWith(window.__lose)) {window.__lose=null; throw new TypeError('가상 응답 유실');}
    let out={};
    if (endpoint === 'reports/daily' || endpoint === 'reports/monthly') {
      const n=Number((u.searchParams.get('date') || '01').slice(-2));
      out={kind:endpoint.endsWith('daily')?'daily':'monthly',work_date:u.searchParams.get('date'),month:u.searchParams.get('month'),
        basis:u.searchParams.get('basis'),basis_label:'수거 예정일',generated_at:'2026-11-07 10:00',source_note:'저장된 업무 기록을 집계합니다.',
        totals:{requests:n,received:n,by_status:{DONE:1},calculated:3000,reduction:0,levied:3000,paid:3000,amount_pending:0,unpaid:0,unload_m3:0,unload_kg:0},
        days:[{date:u.searchParams.get('date') || '2026-11-01',received:n,planned:n,done:1,partial:0,not_collected:0,cancelled:0,calculated:3000,levied:3000,paid:3000}],
        dongs:[{dong:'가상1동',planned:n,done:1,revisit:0,cancelled:0,levied:3000}],
        items:[{name:'가상 의자',spec:'일반',requested:2,collected:1,unit_label:'개'}],crews:[],categories:[],channels:[],reductions:[],
        unload_summary:{count:0,volume_records:0,weight_records:0},private_name:'화면에 나오면 안 되는 이름'};
    } else if (endpoint === 'reports/print' || endpoint.endsWith('/print')) out={token:'fixture-token'};
    else if (endpoint === 'label-usage') out={rows:[],totals:{used:0,requests:0},page:1,pages:1,total:0,source_note:'전체 기간 누적'};
    else if (endpoint === 'numbered-labels') {
      let rows=window.__labels.filter(x=>(!u.searchParams.get('q') || x.number.includes(u.searchParams.get('q'))) && (!u.searchParams.get('status') || x.status === u.searchParams.get('status')));
      const page=Number(u.searchParams.get('page') || 1);
      out={rows:rows.slice((page-1)*50,page*50),total:rows.length,page,pages:Math.max(1,Math.ceil(rows.length/50)),size:50,counts:{ALL:3,AVAILABLE:1,DISTRIBUTED:1,USED:0,VOID:1}};
    } else if (endpoint.endsWith('/history')) {
      const number=decodeURIComponent(endpoint.split('/')[1]);
      out={label:window.__labels.find(x=>x.number===number),rows:window.__history[number] || [{id:1,action:'RECEIVE',from_status:null,to_status:'AVAILABLE',at:'2026-11-07T01:00:00Z',actor:'가상 담당',reason:null}]};
    } else if (endpoint === 'numbered-labels/receive') out={count:3,numbers:['0001','0002','0003']};
    else if (endpoint === 'numbered-labels/distribute') out={count:2};
    else if (endpoint.endsWith('/use')) {
      const label=window.__labels.find(x=>x.number===endpoint.split('/')[1]);
      Object.assign(label,{status:'USED',request_id:body.request_id,line_id:body.line_id || null}); out={label,unchanged:false};
    } else if (endpoint.endsWith('/void')) {
      const label=window.__labels.find(x=>x.number===endpoint.split('/')[1]); label.status='VOID'; out={label};
      window.__history[label.number]=[{id:2,action:'VOID',from_status:'DISTRIBUTED',to_status:'VOID',at:'2026-11-07T01:00:00Z',actor:'가상 담당',reason:body.reason}];
    } else if (endpoint === 'requests') out={rows:[{id:'fixture-request-uuid',receipt_no:'DG-20261107-0001',status:'RECEIVED',name:'불필요한 이름',address:'불필요한 상세주소',items:[{line_id:'line-1',name:'가상 의자',spec:'일반',qty:1,unit_label:'개'}]}],total:1,page:1,pages:1,size:10};
    else if (endpoint === 'requests/fixture-request-uuid') out={id:'fixture-request-uuid',receipt_no:'DG-20261107-0001',items:[{line_id:'line-1',name:'가상 의자',spec:'일반',qty:1,unit_label:'개'}]};
    const json=JSON.stringify(out);
    if (hold) await new Promise(release=>window.__gates.push(release));
    return new Response(json,{status:200,headers:{'Content-Type':'application/json'}});
  };
  try {
    const S=await import('/pc/ui/screens/'+screen+'.js'); window.__screen=S[screen==='reports'?'screenReports':'screenNumberedLabels'];
    window.__mount=async()=>document.getElementById('app').replaceChildren(await window.__screen()); await window.__mount(); return true;
  } catch(error) {window.__loadError=error.message;return false;}
}"""


def mount(browser, url, screen, role="ADMIN"):
    context = browser.new_context(service_workers="block", viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.route("**/ui-fixture.html", lambda route: route.fulfill(content_type="text/html", body="<link rel='stylesheet' href='/pc/ui/pc.css'><main id='app'></main>"))
    page.goto(url + "/ui-fixture.html#/" + screen)
    loaded = page.evaluate(FIXTURE, {"screen": screen, "role": role})
    ok(loaded, f"{screen} 화면 모듈을 실제 브라우저에서 읽고 mount한다")
    return context, page


def reports_regressions(browser, url):
    context, page = mount(browser, url, "reports")
    try:
        expect(page.get_by_text("가상 의자", exact=True)).to_be_visible()
        expect(page.get_by_role("columnheader", name="선택 기준 건수", exact=True)).to_be_visible()
        ok("화면에 나오면 안 되는 이름" not in page.locator("#app").inner_text(), "보고서 미리보기는 집계 필드만 표시한다")
        page.get_by_label("집계 기준", exact=True).select_option("received")
        expect(page.get_by_role("button", name="인쇄 화면 열기", exact=True)).to_be_disabled()
        page.get_by_role("button", name="조회", exact=True).click()
        expect(page.get_by_role("button", name="인쇄 화면 열기", exact=True)).to_be_enabled()
        page.get_by_role("button", name="인쇄 화면 열기", exact=True).click()
        page.wait_for_function("window.__opened.length === 1")
        printed = page.evaluate("window.__calls.find(x=>x.path==='/api/reports/print').body")
        ok(printed == {"kind": "daily", "date": "2026-11-07", "basis": "received"}, "보고서 인쇄는 확인한 조회조건을 그대로 보낸다")
        page.evaluate("() => {window.__hold='reports/daily';}")
        page.get_by_label("작업일", exact=True).fill("2026-11-08")
        page.get_by_role("button", name="조회", exact=True).click()
        page.wait_for_function("window.__gates.length === 1")
        page.get_by_label("작업일", exact=True).fill("2026-11-09")
        page.get_by_role("button", name="조회", exact=True).click()
        page.wait_for_function("document.querySelector('[data-report-summary]')?.textContent.includes('9건')")
        page.evaluate("() => window.__gates.shift()()")
        expect(page.locator("[data-report-summary]")).to_contain_text("9건")
        ok(True, "이전 조회의 늦은 응답은 최신 보고서를 덮지 않는다")
        page.get_by_label("보고서 종류", exact=True).select_option("monthly")
        page.get_by_label("보고 월", exact=True).fill("2026-10")
        page.get_by_role("button", name="조회", exact=True).click()
        page.wait_for_function("window.__calls.some(x=>x.path==='/api/reports/monthly' && x.search.includes('month=2026-10'))")
        ok(True, "월보는 월 단위 전용 API로 조회한다")
        with page.expect_download() as download:
            page.get_by_role("button", name="CSV 내려받기", exact=True).click()
        ok(download.value.suggested_filename == "동구-월보-2026-10.csv" and page.evaluate("window.__calls.some(x=>x.path==='/api/reports/monthly.csv' && x.search.includes('month=2026-10'))"), "월보 CSV도 확인한 월·기준으로 내려받는다")
        page.evaluate("() => {window.__fail='reports/monthly';}")
        page.get_by_role("button", name="조회", exact=True).click()
        expect(page.get_by_role("alert")).to_contain_text("가상 입력 오류")
        expect(page.get_by_label("보고 월", exact=True)).to_have_value("2026-10")
        expect(page.get_by_role("button", name="인쇄 화면 열기", exact=True)).to_be_disabled()
        page.get_by_role("button", name="다시 시도", exact=True).click()
        expect(page.get_by_role("button", name="인쇄 화면 열기", exact=True)).to_be_enabled()
        ok(True, "보고서 조회 실패는 기간을 유지하고 재조회 전 출력하지 않는다")
        downloads = []
        page.on("download", lambda download: downloads.append(download))
        page.evaluate("() => {window.__hold='reports/monthly.csv';}")
        page.get_by_role("button", name="CSV 내려받기", exact=True).click()
        page.wait_for_function("window.__gates.length === 1")
        page.get_by_label("집계 기준", exact=True).select_option("completed")
        page.evaluate("() => window.__gates.shift()()")
        page.wait_for_timeout(150)
        ok(not downloads, "CSV 응답을 기다리는 중 조건을 바꾸면 이전 파일 저장을 억제한다")
        page.get_by_role("button", name="조회", exact=True).click()
        expect(page.get_by_role("button", name="CSV 내려받기", exact=True)).to_be_enabled()
        page.evaluate("() => {window.__hold='reports/monthly.csv';}")
        page.get_by_role("button", name="CSV 내려받기", exact=True).click()
        page.wait_for_function("window.__gates.length === 1")
        page.evaluate("() => {location.hash='#/other'; document.getElementById('app').textContent='다른 화면';window.__gates.shift()();}")
        page.wait_for_timeout(150)
        ok(not downloads, "CSV 응답을 기다리는 중 화면을 떠나면 파일 저장을 억제한다")
        page.evaluate("async()=>{location.hash='#/reports'; await new Promise(resolve=>setTimeout(resolve,0));await window.__mount();}")
        expect(page.get_by_role("button", name="조회", exact=True)).to_be_enabled()
        page.evaluate("() => {window.__hold='reports/monthly';}")
        page.get_by_role("button", name="조회", exact=True).click()
        page.wait_for_function("window.__gates.length === 1")
        page.evaluate("() => {location.hash='#/other'; document.getElementById('app').textContent='다른 화면';window.__gates.shift()();}")
        expect(page.locator("#app")).to_have_text("다른 화면")
        ok(True, "보고서에서 떠난 뒤 도착한 응답은 새 화면을 변경하지 않는다")
    finally:
        context.close()


def labels_regressions(browser, url):
    context, page = mount(browser, url, "numbered_labels")
    try:
        expect(page.get_by_role("button", name="0001", exact=True)).to_be_visible()
        page.get_by_role("button", name="번호 구간 입고", exact=True).click()
        dialog = page.get_by_role("dialog")
        start, end = dialog.get_by_label("시작 번호", exact=True), dialog.get_by_label("끝 번호", exact=True)
        ok(start.get_attribute("type") == "text" and start.get_attribute("inputmode") == "numeric" and start.get_attribute("maxlength") == "30", "번호 입력은 최대 30자리 문자열로 앞 0을 보존한다")
        start.fill("0001")
        end.fill("0003")
        page.evaluate("() => {window.__fail='numbered-labels/receive';}")
        dialog.get_by_role("button", name="입고 기록", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("가상 입력 오류")
        expect(start).to_have_value("0001")
        expect(end).to_have_value("0003")
        ok(True, "입고 실패는 오류와 입력을 유지한다")
        dialog.get_by_role("button", name="취소", exact=True).click()
        page.get_by_role("button", name="번호 구간 입고", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_label("시작 번호", exact=True)).to_have_value("0001")
        page.evaluate("() => {window.__hold='numbered-labels/receive';}")
        dialog.get_by_role("button", name="입고 기록", exact=True).evaluate("button=>{button.click();button.click();}")
        page.wait_for_function("window.__gates.length === 1")
        calls = page.evaluate("window.__calls.filter(x=>x.path==='/api/numbered-labels/receive')")
        ok(len(calls) == 2 and calls[-1]["body"]["start"] == "0001" and calls[-1]["body"]["end"] == "0003" and bool(calls[-1]["body"]["client_req_id"]), "실패 뒤 재시도는 문자열 번호·멱등키를 보내고 더블클릭은 한 번만 처리한다")
        page.evaluate("() => window.__gates.shift()()")
        expect(page.get_by_role("dialog")).to_have_count(0)
        page.get_by_role("button", name="번호 구간 입고", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("시작 번호", exact=True).fill("00000")
        dialog.get_by_label("끝 번호", exact=True).fill("05000")
        before = page.evaluate("window.__calls.filter(x=>x.path==='/api/numbered-labels/receive').length")
        dialog.get_by_role("button", name="입고 기록", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("5,000")
        ok(page.evaluate("window.__calls.filter(x=>x.path==='/api/numbered-labels/receive').length") == before, "5,001개 구간은 서버 요청 없이 차단한다")
        dialog.get_by_role("button", name="입력 초기화", exact=True).click()
        expect(dialog.get_by_label("시작 번호", exact=True)).to_have_value("")
        dialog.get_by_role("button", name="취소", exact=True).click()
        page.get_by_role("button", name="번호 구간 배부", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("시작 번호", exact=True).fill("0004")
        dialog.get_by_label("끝 번호", exact=True).fill("0005")
        dialog.get_by_label("배부처", exact=True).fill("가상 기관")
        page.evaluate("() => {window.__lose='numbered-labels/distribute';}")
        dialog.get_by_role("button", name="배부 기록", exact=True).click()
        expect(dialog.get_by_role("alert")).to_contain_text("가상 응답 유실")
        dialog.get_by_role("button", name="취소", exact=True).click()
        page.get_by_role("button", name="번호 구간 배부", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_label("배부처", exact=True)).to_have_value("가상 기관")
        dialog.get_by_role("button", name="배부 기록", exact=True).click()
        expect(page.get_by_role("dialog")).to_have_count(0)
        calls = page.evaluate("window.__calls.filter(x=>x.path==='/api/numbered-labels/distribute')")
        ok(len(calls) == 2 and calls[0]["body"] == calls[1]["body"], "배부 응답 유실 뒤 다시 연 창도 같은 입력·요청번호로 재시도한다")
        page.get_by_role("button", name="0001", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("가상 담당", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="인쇄 화면 열기", exact=True).click()
        page.wait_for_function("window.__opened.length === 1")
        ok(not page.evaluate("window.__calls.some(x=>x.path.endsWith('/use'))"), "배출표 인쇄만으로 사용 처리하지 않는다")
        dialog.get_by_label("접수 찾기", exact=True).fill("0001")
        dialog.get_by_label("접수 찾기", exact=True).press("Enter")
        expect(dialog).to_be_visible()
        ok(True, "상세 접수찾기 Enter는 창을 닫지 않고 검색한다")
        dialog.get_by_role("button", name="DG-20261107-0001 선택", exact=True).click()
        ok("불필요한 이름" not in dialog.inner_text() and "불필요한 상세주소" not in dialog.inner_text(), "접수 연결 후보는 접수번호·상태 위주로 표시한다")
        dialog.get_by_label("연결 품목", exact=True).select_option("line-1")
        page.evaluate("() => {window.__hold='numbered-labels/0001/use';}")
        dialog.get_by_role("button", name="접수에 연결하고 사용 기록", exact=True).click()
        page.wait_for_function("window.__gates.length === 1")
        page.wait_for_function("window.__calls.some(x=>x.path==='/api/numbered-labels/0001/use')")
        use = page.evaluate("window.__calls.find(x=>x.path==='/api/numbered-labels/0001/use').body")
        ok(use["request_id"] == "fixture-request-uuid" and use["line_id"] == "line-1" and bool(use["client_req_id"]), "번호 사용은 선택한 접수·품목을 명시해 멱등 요청한다")
        dialog.get_by_role("button", name="닫기", exact=True).click()
        page.evaluate("() => window.__gates.shift()()")
        expect(page.get_by_role("row").filter(has=page.get_by_role("button", name="0001", exact=True))).to_contain_text("사용")
        ok(True, "저장 중 상세창을 닫아도 성공 응답은 현재 원장의 상태를 갱신한다")
        page.get_by_role("button", name="0002", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("무효 사유", exact=True).fill("가상 훼손")
        page.evaluate("() => {window.__fail='numbered-labels/0002/void';}")
        dialog.get_by_role("button", name="무효 기록", exact=True).click()
        expect(dialog.get_by_role("alert").filter(has_text="가상 입력 오류")).to_be_visible()
        expect(dialog.get_by_label("무효 사유", exact=True)).to_have_value("가상 훼손")
        dialog.get_by_role("button", name="무효 기록", exact=True).click()
        expect(dialog.get_by_role("cell", name="가상 훼손", exact=True)).to_be_visible()
        ok(dialog.get_by_label("무효 사유", exact=True).count() == 0, "무효 성공은 사유 이력을 표시하고 재사용·무효 입력을 닫는다")
        dialog.get_by_role("button", name="닫기", exact=True).click()
        page.evaluate("() => {window.__hold='numbered-labels';}")
        page.get_by_label("번호 찾기", exact=True).fill("0001")
        page.get_by_role("button", name="조회", exact=True).click()
        page.wait_for_function("window.__gates.length === 1")
        page.get_by_label("번호 찾기", exact=True).fill("0002")
        page.get_by_role("button", name="조회", exact=True).click()
        expect(page.get_by_role("button", name="0002", exact=True)).to_be_visible()
        page.evaluate("() => window.__gates.shift()()")
        expect(page.get_by_role("button", name="0001", exact=True)).to_have_count(0)
        ok(True, "원장도 늦게 도착한 이전 검색이 최신 번호를 덮지 않는다")
    finally:
        context.close()
    context, page = mount(browser, url, "numbered_labels", "LEAD")
    try:
        expect(page.get_by_role("button", name="0002", exact=True)).to_be_visible()
        ok(page.get_by_role("button", name="번호 구간 입고", exact=True).count() == 0 and page.get_by_role("button", name="번호 구간 배부", exact=True).count() == 0, "팀장은 원장 조회와 출력만 제공한다")
        page.get_by_role("button", name="0002", exact=True).click()
        expect(page.get_by_role("dialog").get_by_role("button", name="인쇄 화면 열기", exact=True)).to_be_visible()
        ok(page.get_by_label("접수 찾기", exact=True).count() == 0 and page.get_by_label("무효 사유", exact=True).count() == 0, "팀장 상세에는 사용·무효 입력을 노출하지 않는다")
    finally:
        context.close()
    context, page = mount(browser, url, "numbered_labels", "FIELD")
    try:
        ok(not page.evaluate("window.__calls.length"), "현장 역할은 원장 API를 호출하지 않는다")
    finally:
        context.close()


def labels_mutation_refresh_regression(browser, url):
    context, page = mount(browser, url, "numbered_labels")
    try:
        page.get_by_role("button", name="0001", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("접수 찾기", exact=True).fill("0001")
        dialog.get_by_role("button", name="접수 검색", exact=True).click()
        dialog.get_by_role("button", name="DG-20261107-0001 선택", exact=True).click()
        page.evaluate("() => {window.__hold='numbered-labels';window.__holdExact=true;}")
        dialog.get_by_role("button", name="접수에 연결하고 사용 기록", exact=True).click()
        page.wait_for_function("window.__gates.length === 1")
        expect(dialog.get_by_label("접수 찾기", exact=True)).to_have_count(0)
        dialog.get_by_label("무효 사유", exact=True).fill("가상 분실")
        dialog.get_by_role("button", name="무효 기록", exact=True).click()
        expect(dialog.get_by_role("cell", name="가상 분실", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="닫기", exact=True).click()
        page.evaluate("() => window.__gates.shift()()")
        expect(page.get_by_role("row").filter(has=page.get_by_role("button", name="0001", exact=True))).to_contain_text("무효")
        ok(True, "연속 사용·무효 저장은 진행 중인 같은 조건 조회를 새로 시작하고 과거 사용 응답을 버린다")
    finally:
        context.close()


def main():
    with fixture_server() as url, sync_playwright() as playwright:
        options = {"headless": True}
        if os.environ.get("CHROME"):
            options["executable_path"] = os.environ["CHROME"]
        browser = playwright.chromium.launch(**options)
        try:
            reports_regressions(browser, url)
            labels_regressions(browser, url)
            labels_mutation_refresh_regression(browser, url)
        finally:
            browser.close()
    print(f"\n통과 {passed}개", flush=True)


if __name__ == "__main__":
    main()
