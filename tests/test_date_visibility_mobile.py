"""실제 암호화 모바일 자료에서 날짜/상태 때문에 가려진 접수를 찾는다."""
import datetime as dt
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import mobile_server
from test_workflow_mobile import open_mobile, seed, stop


def main():
    output=Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-date-mobile-'))/'shots'
    output.mkdir(parents=True,exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path,headless=True)
        context,page=open_mobile(browser,url)
        try:
            today=page.evaluate("import('./core.js').then(C=>C.todayKST())")
            old=(dt.date.fromisoformat(today)-dt.timedelta(days=7)).isoformat()
            future=(dt.date.fromisoformat(today)+dt.timedelta(days=2)).isoformat()
            records=[stop(1,old,receipt_no='DATE-OLD-DONE'),stop(2,today,receipt_no='DATE-TODAY'),
                     stop(3,future,receipt_no='DATE-FUTURE-CANCEL',status='CANCELLED'),stop(4,None,receipt_no='DATE-NODATE')]
            events=[{'request_id':records[0]['request_id'],'kind':'ITEM_RESULT','body':{'line_id':'line-1','qty_confirmed':1,'qty_collected':1}},
                    {'request_id':records[0]['request_id'],'kind':'ATTEMPT','body':{'outcome':'DONE'}}]
            seed(page,{'mode':'SOLO','requests':records,'events':events},registered=False)
            expect(page.get_by_label('목록에서 찾기',exact=True)).to_be_visible()
            page.get_by_role('button',name='날짜·접수 찾기',exact=True).click()
            expect(page.get_by_role('heading',name='날짜·접수 찾기',exact=True)).to_be_visible()
            before=page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.requests,C.st.events]))")
            expect(page.locator('.stops .stop')).to_have_count(4)
            page.get_by_label('찾을 접수',exact=True).fill('DATE-OLD-DONE')
            expect(page.locator('.stops .stop')).to_have_count(1)
            page.locator('.stops .stop').click()
            expect(page.get_by_role('button',name='PC 입력 도움',exact=True)).to_be_visible()
            page.get_by_role('button',name='← 목록',exact=True).click()
            expect(page.get_by_label('찾을 접수',exact=True)).to_have_value('DATE-OLD-DONE')
            page.locator('.stops .stop').click()
            page.locator('.dock-back').click()
            expect(page.get_by_label('찾을 접수',exact=True)).to_have_value('DATE-OLD-DONE')
            page.locator('.stops .stop').click()
            page.get_by_role('button',name='+ 현장 품목 추가',exact=True).click()
            expect(page.get_by_role('heading',name='현장 품목 추가',exact=True)).to_be_visible()
            page.get_by_role('button',name='← 취소',exact=True).click()
            page.get_by_role('button',name='← 목록',exact=True).click()
            expect(page.get_by_label('찾을 접수',exact=True)).to_have_value('DATE-OLD-DONE')
            page.locator('.stops .stop').click()
            page.locator('.dock-next').click()
            expect(page.get_by_label('찾을 접수',exact=True)).to_have_value('DATE-OLD-DONE')
            page.get_by_role('button',name='조회조건 초기화',exact=True).click()
            page.get_by_label('찾기 처리 상태',exact=True).select_option('DONE')
            expect(page.locator('.stops .stop')).to_have_count(1)
            page.get_by_role('button',name='조회조건 초기화',exact=True).click()
            page.get_by_label('찾기 날짜 범위',exact=True).select_option('UNDATED')
            expect(page.locator('.stops .stop')).to_have_count(1)
            page.get_by_label('찾기 날짜 범위',exact=True).select_option('DAY')
            page.get_by_label('찾기 예정일',exact=True).fill(old)
            expect(page.locator('.stops .stop')).to_have_count(1)
            page.get_by_role('button',name='조회조건 초기화',exact=True).click()
            expect(page.get_by_label('찾기 조회 범위',exact=True)).to_contain_text('전체 기간')
            assert page.evaluate("() => document.documentElement.scrollWidth<=innerWidth")
            assert page.evaluate("import('./core.js').then(C=>JSON.stringify([C.st.requests,C.st.events]))")==before
            page.screenshot(path=str(output/'mobile-all-dates.png'),full_page=True)
            page.goto(url+'#/home')
            page.get_by_label('목록에서 찾기',exact=True).fill('DATE-OLD-DONE')
            page.get_by_role('button',name='다른 날짜에서도 찾기',exact=True).click()
            expect(page.get_by_label('찾을 접수',exact=True)).to_have_value('DATE-OLD-DONE')
            expect(page.locator('.stops .stop')).to_have_count(1)
            received={k:v for k,v in records[1].items() if k!='planned_date'}  # 실제 받은 목록의 날짜는 상위 work_date에 있다.
            seed(page,{'mode':'PC','daylist':{'work_date':today,'crew':'가상 날짜팀','stops':[received]},
                       'fieldStops':[{**records[0],'field':True},{**records[3],'field':True}],'events':events})
            page.get_by_role('button',name='날짜·접수 찾기',exact=True).click()
            expect(page.locator('.stops .stop')).to_have_count(3)
            expect(page.get_by_label('찾기 자료 범위',exact=True)).to_contain_text('받은 목록')
            expect(page.get_by_label('찾기 자료 범위',exact=True)).to_contain_text('현장 접수')
            page.get_by_label('찾기 날짜 범위',exact=True).select_option('DAY')
            page.get_by_label('찾기 예정일',exact=True).fill(today)
            expect(page.locator('.stops .stop')).to_have_count(1)
            page.get_by_label('찾기 날짜 범위',exact=True).select_option('UNDATED')
            expect(page.locator('.stops .stop')).to_have_count(1)
            page.get_by_role('button',name='조회조건 초기화',exact=True).click()
            page.get_by_label('찾을 접수',exact=True).fill('DATE-NODATE')
            expect(page.locator('.stops .stop')).to_have_count(1)
            print('모바일 날짜 찾기: 390px·소량 검색·과거 완료/미래 취소/날짜 없음·전체기간·예정일·상태·상세 복귀 검색 보존·이벤트 불변·PC 연결 자료 범위 통과')
        finally:
            context.close();browser.close()


if __name__=='__main__':
    main()
