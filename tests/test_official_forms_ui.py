"""원본 서식 UI: 지연 응답·다운로드·오류 재시도·권한의 Chromium 회귀."""
import os
from playwright.sync_api import expect, sync_playwright
from test_reports_labels_ui import fixture_server

FIXTURE = r"""async (role) => {
  const C = await import('/pc/ui/core.js');
  C.setBoot({today:'2026-11-12',me:{username:'forms-fixture',role,prefs:{}},settings:{},crews:[],dongs:[{id:'d1',name:'가상동'}]});
  window.__downloads=[]; window.__calls=[]; window.__gates=[]; window.__hold=''; window.__fail=false; window.__pending=false;
  HTMLAnchorElement.prototype.click=function(){if(this.download)window.__downloads.push(this.download);};
  const original=window.fetch;
  window.fetch=async (path,options={}) => {
    if(!String(path).startsWith('/api/'))return original(path,options);
    const u=new URL(path,location.origin), endpoint=u.pathname.slice(5), body=options.body?JSON.parse(options.body):null;
    window.__calls.push({endpoint,body,search:u.search});
    const hold=window.__hold===endpoint; if(hold)window.__hold='';
    if(window.__fail){window.__fail=false;return new Response(JSON.stringify({error:'가상 서식 조회 오류'}),{status:400});}
    let response;
    if(endpoint==='reports/forms'){
      const day=u.searchParams.get('date'), n=Number(day.slice(-2));
      const totals={qty:n,fee:1000,opening:2,received:10,issued:n,closing:12-n,ready:true,issues:[]};
      const monthly={...totals,qty:30};
      if(window.__pending){monthly.ready=false;monthly.issues=['배출표 3장이 미배정입니다.'];}
      response=new Response(JSON.stringify({dong_id:'d1',dong:'가상동',date:day,month:day.slice(0,7),daily:totals,monthly,source_note:'실제 수거 기준'}));
    }else response=new Response('PK-fixture',{headers:{'Content-Disposition':"attachment; filename*=UTF-8''fixture.xlsx"}});
    if(hold)await new Promise(resolve=>window.__gates.push(resolve));
    return response;
  };
  const {officialForms}=await import('/pc/ui/screens/official_forms.js');
  const panel=officialForms(()=>true); document.getElementById('app').replaceChildren(panel);
}"""


def main():
    with fixture_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers='block')
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            def mount(role='ADMIN'):
                page.goto(url + '/pc/ui/index.html')
                page.evaluate(FIXTURE, role)
            mount()
            panel = page.get_by_role('region', name='원본 서식 자동 작성')
            query = panel.get_by_role('button', name='서식 조회', exact=True)
            date = panel.get_by_label('서식 처리일', exact=True)
            xlsx = panel.get_by_role('button', name='월보 XLSX', exact=True)
            daily = panel.get_by_label('일일 대장 수거 수량', exact=True)
            expect(daily).to_have_text('12개')
            page.evaluate("window.__hold='reports/forms'")
            date.fill('2026-11-10'); query.click()
            page.wait_for_function('window.__gates.length===1')
            date.fill('2026-11-11'); query.click()
            expect(daily).to_have_text('11개')
            page.evaluate('window.__gates.shift()()')
            expect(daily).to_have_text('11개')
            page.evaluate("window.__hold='reports/forms/download'")
            xlsx.click()
            page.wait_for_function('window.__gates.length===1')
            date.fill('2026-11-09')
            page.evaluate('window.__gates.shift()()')
            expect(xlsx).to_be_disabled()
            page.wait_for_timeout(150)
            assert page.evaluate('window.__downloads.length') == 0
            query.click(); expect(daily).to_have_text('9개')
            page.evaluate('window.__pending=true')
            query.click()
            expect(panel.get_by_text('월보 · 2026-11: 배출표 3장이 미배정입니다.', exact=True)).to_be_visible()
            expect(xlsx).to_be_disabled()
            expect(panel.get_by_role('button', name='두 서식 ZIP', exact=True)).to_be_disabled()
            expect(panel.get_by_role('button', name='일일 대장 ODT', exact=True)).to_be_enabled()
            page.evaluate('window.__fail=true')
            query.click()
            expect(panel.get_by_role('button', name='서식 다시 시도')).to_be_visible()
            expect(date).to_have_value('2026-11-09')
            page.evaluate('window.__pending=false')
            panel.get_by_role('button', name='서식 다시 시도').click()
            expect(xlsx).to_be_enabled()
            page.evaluate("window.__hold='reports/forms/download'")
            xlsx.click(); page.wait_for_function('window.__gates.length===1')
            page.evaluate("document.getElementById('app').replaceChildren();window.__gates.shift()()")
            page.wait_for_timeout(150)
            assert page.evaluate('window.__downloads.length') == 0
            mount()
            expect(xlsx).to_be_enabled()
            page.evaluate("window.__hold='reports/forms/download'")
            xlsx.click(); page.wait_for_function('window.__gates.length===1')
            page.evaluate("async () => {const C=await import('/pc/ui/core.js');C.setBoot(null,{sessionChanged:true});window.__gates.shift()();}")
            page.wait_for_timeout(150)
            assert page.evaluate('window.__downloads.length') == 0, '로그아웃 후 개인정보 다운로드'
            mount()
            expect(xlsx).to_be_enabled()
            page.evaluate("window.__hold='reports/forms/download'")
            xlsx.click(); page.wait_for_function('window.__gates.length===1')
            page.evaluate("async () => {const C=await import('/pc/ui/core.js');const saved=C.boot;C.setBoot(null,{sessionChanged:true});C.setBoot(saved,{sessionChanged:true});window.__gates.shift()();}")
            page.wait_for_timeout(150)
            assert page.evaluate('window.__downloads.length') == 0, '동일 계정 재로그인 뒤 옛 개인정보 다운로드'
            mount('LEAD')
            expect(panel.get_by_label('일일 대장 수거 수량')).to_have_text('12개')
            expect(panel.get_by_role('button', name='월보 XLSX')).to_have_count(0)
            assert not errors, errors
            print('원본 서식 UI: 지연 조회 무시·조건 변경/화면 이탈 시 다운로드 억제·재고 부족 구분·오류 재시도·팀장 다운로드 제한 통과')
        finally:
            context.close(); browser.close()


if __name__ == '__main__':
    main()
