"""설정 행의 이전 인증 응답/재전송/화면 이동이 새 초안을 덮지 않는지 확인."""
import os
from playwright.sync_api import expect, sync_playwright
from test_reports_labels_ui import fixture_server

FIXTURE = r"""async () => {
  const C=await import('/pc/ui/core.js');
  const dong={id:'a',name:'가상A동',active:true,crew_id:'c',weekdays:[],keywords:[],depot:{lat:35.14,lon:126.9,label:'가상 차고지'}};
  C.setBoot({me:{username:'A',role:'ADMIN',prefs:{}},today:'2026-11-12',dongs:[dong],crews:[{id:'c',name:'가상팀'}],settings:{depot:dong.depot}});
  window.__renders=0;window.__posts=0;window.__status=200;window.__done=false;window.__serverDong=dong;window.__holdGet=false;
  C.setRender(async()=>{window.__renders++;});
  const original=window.fetch;
  window.fetch=async (path,options={})=>{
    if(!String(path).startsWith('/api/'))return original(path,options);
    if(options.method==='POST'){
      const body=JSON.parse(options.body || '{}');
      window.__posts++;
      await new Promise(resolve=>window.__release=resolve);
      window.__done=true;
      if(window.__status===200)window.__serverDong={...dong,...body,keywords:typeof body.keywords==='string'?body.keywords.split(',').filter(Boolean):body.keywords};
      return new Response(JSON.stringify(window.__status===401?{error:'fixture expired',login:true}:{id:'a'}),{status:window.__status});
    }
    const response=new Response(JSON.stringify([window.__serverDong]));
    if(window.__holdGet){window.__holdGet=false;await new Promise(resolve=>window.__releaseGet=resolve);}
    return response;
  };
  const {dongCard}=await import('/pc/ui/screens/settings.js');
  document.getElementById('app').replaceChildren(dongCard());
}"""


def main():
    with fixture_server() as url, sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path,headless=True)
        page=browser.new_page()
        try:
            for phase in ('before', 'late200', 'late401', 'same_user_other_screen'):
                page.goto(url+'/pc/ui/index.html')
                page.evaluate(FIXTURE)
                if phase=='before':
                    page.evaluate("async()=>{const C=await import('/pc/ui/core.js');C.setBoot({...C.boot,me:{...C.boot.me,username:'B'}},{sessionChanged:true});}")
                    page.get_by_role('button',name='저장',exact=True).click()
                    page.wait_for_timeout(150)
                    assert page.evaluate('window.__posts')==0
                    continue
                page.get_by_role('button',name='저장',exact=True).click()
                page.wait_for_function('typeof window.__release==="function"')
                page.evaluate("""async (phase)=>{const C=await import('/pc/ui/core.js');
                  if(phase!=='same_user_other_screen')C.setBoot({...C.boot,me:{...C.boot.me,username:'B'}},{sessionChanged:true});
                  document.getElementById('app').replaceChildren(C.h('input',{id:'new-draft',value:'새 화면 초안'}));
                  if(phase==='late401')window.__status=401;
                  window.__release();
                }""",phase)
                page.wait_for_function('window.__done')
                page.wait_for_timeout(200)
                assert page.evaluate('window.__renders')==0, phase
                assert page.evaluate("import('/pc/ui/core.js').then(C=>C.boot?.me?.username)")==('A' if phase=='same_user_other_screen' else 'B')
                expect(page.locator('#new-draft')).to_have_value('새 화면 초안')
                expect(page.get_by_role('dialog')).to_have_count(0)
            page.goto(url+'/pc/ui/index.html');page.evaluate(FIXTURE)
            page.get_by_label('가상A동 부과번호 코드',exact=True).fill('Q1')
            page.get_by_role('button',name='저장',exact=True).click()
            page.wait_for_function('typeof window.__release==="function"')
            page.get_by_label('가상A동 부과번호 코드',exact=True).fill('')
            page.evaluate('window.__release()');page.wait_for_function('window.__done');page.wait_for_timeout(200)
            page.evaluate("async()=>{const S=await import('/pc/ui/screens/settings.js');document.getElementById('app').replaceChildren(S.dongCard());}")
            expect(page.get_by_label('가상A동 부과번호 코드',exact=True)).to_have_value('')
            page.goto(url+'/pc/ui/index.html');page.evaluate(FIXTURE)
            page.evaluate("async()=>{const C=await import('/pc/ui/core.js');C.boot.dongs[0].depot=null;const S=await import('/pc/ui/screens/settings.js');document.getElementById('app').replaceChildren(S.dongCard());}")
            page.get_by_role('button',name='가상A동 차고지 설정',exact=True).click()
            dialog=page.get_by_role('dialog',name='가상A동 차고지 설정',exact=True)
            dialog.get_by_label('차고지 위도',exact=True).fill('35.15')
            dialog.get_by_label('차고지 경도',exact=True).fill('126.91')
            dialog.get_by_role('button',name='행에 적용',exact=True).click()
            page.get_by_role('button',name='저장',exact=True).click()
            page.wait_for_function('typeof window.__release==="function"')
            page.evaluate("async()=>{const S=await import('/pc/ui/screens/settings.js');document.getElementById('app').replaceChildren(S.dongCard());}")
            page.get_by_role('button',name='가상A동 차고지 설정',exact=True).click()
            dialog.get_by_role('button',name='공통 차고지 사용',exact=True).click()
            dialog.get_by_role('button',name='행에 적용',exact=True).click()
            page.evaluate('window.__release()');page.wait_for_function('window.__done');page.wait_for_timeout(200)
            page.evaluate("async()=>{const S=await import('/pc/ui/screens/settings.js');document.getElementById('app').replaceChildren(S.dongCard());}")
            expect(page.get_by_label('가상A동 차고지 현황',exact=True)).to_contain_text('공통 차고지 사용')
            page.goto(url+'/pc/ui/index.html');page.evaluate(FIXTURE)
            page.get_by_label('가상A동 부과번호 코드',exact=True).fill('Q1')
            page.get_by_role('button',name='저장',exact=True).click()
            page.wait_for_function('typeof window.__release==="function"')
            page.evaluate("async()=>{const C=await import('/pc/ui/core.js');const S=await import('/pc/ui/screens/settings.js');document.getElementById('app').replaceChildren(S.dongCard());}")
            page.get_by_label('가상A동 부과번호 코드',exact=True).fill('Q2')
            page.evaluate('window.__release()');page.wait_for_function('window.__done');page.wait_for_timeout(200)
            page.evaluate("async()=>{const S=await import('/pc/ui/screens/settings.js');document.getElementById('app').replaceChildren(S.dongCard());}")
            expect(page.get_by_label('가상A동 부과번호 코드',exact=True)).to_have_value('Q2')
            page.goto(url+'/pc/ui/index.html');page.evaluate(FIXTURE)
            page.get_by_label('가상A동 부과번호 코드',exact=True).fill('Q1')
            page.evaluate('window.__holdGet=true')
            page.get_by_role('button',name='저장',exact=True).click()
            page.wait_for_function('typeof window.__release==="function"');page.evaluate('window.__release()')
            page.wait_for_function('typeof window.__releaseGet==="function"')
            page.evaluate("async()=>{const S=await import('/pc/ui/screens/settings.js');document.getElementById('app').replaceChildren(S.dongCard());window.__release=null;}")
            page.get_by_label('가상A동 부과번호 코드',exact=True).fill('Q2')
            page.get_by_role('button',name='저장',exact=True).click()
            page.wait_for_function('typeof window.__release==="function"');page.evaluate('window.__release()')
            page.wait_for_function("async()=>{const C=await import('/pc/ui/core.js');return C.boot?.dongs?.[0]?.code==='Q2';}")
            page.evaluate('window.__releaseGet()');page.wait_for_timeout(200)
            assert page.evaluate("import('/pc/ui/core.js').then(C=>C.boot.dongs[0].code)")=='Q2'
            print('설정 행: 이전 인증 요청 차단·late200/401 응답 및 자동 재로그인 차단·다른 화면 초안 보존 통과')
        finally:
            browser.close()


if __name__=='__main__':
    main()
