"""실제 서버/EXE: 기간 초기화·검색 보존·동별 전체 기간과 배차 날짜의 구분."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from playwright.sync_api import expect, sync_playwright
from test_review_ui import pc_server


def main(exe=None):
    if exe:
        from test_reports_labels_bundle import frozen_server
        server = frozen_server(exe)
    else:
        server = pc_server()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-date-ui-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with server as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        context = browser.new_context(service_workers='block', viewport={'width':1440,'height':1000})
        page = context.new_page()
        errors=[]
        page.on('pageerror',lambda error: errors.append(str(error)))
        def api(path, body=None):
            opts={'headers':{'X-DG-CSRF':'1'}}
            result=context.request.get(url+'api/'+path,**opts) if body is None else context.request.post(url+'api/'+path,data=body,**opts)
            assert result.ok, (path,result.status,result.text())
            return result.json()
        try:
            api('setup',{'username':'dates_admin','display_name':'가상 날짜 담당','password':'dates-ui-test-123'})
            boot=api('bootstrap');today=boot['today']
            dong=next(d for d in boot['dongs'] if d['name']=='충장동')
            fee=next(r['id'] for r in json.loads((Path(__file__).resolve().parent.parent/'data/fee_table.json').read_text(encoding='utf-8'))['rows'] if r['status']=='CONFIRMED' and r.get('price',0)>0)
            records=[]
            for name,day in [('조회가상과거','2026-01-02'),('조회가상현재',today)]:
                records.append(api('requests',{'name':name,'phone':'01000000000','address':'가상구 날짜로 1','dong':dong['name'],'planned_date':day,'items':[{'fee_rate_id':fee,'qty':1}]}))
            old=api('requests/'+records[0]['id'])
            api('requests/'+old['id']+'/collection',{'req_ver':old['req_ver'],'basis':old['collection']['basis'],'collected_on':'2026-01-02','items':[{'line_id':old['items'][0]['line_id'],'qty':1}],'client_req_id':'date-test-old-completion'})
            page.goto(url+'#/requests')
            page.locator('#rq-q').fill('조회가상')
            page.locator('#rq-dong').select_option('충장동')
            page.locator('#rq-from').fill(today);page.locator('#rq-to').fill(today)
            page.get_by_role('button',name='검색',exact=True).click()
            expect(page.get_by_text('조회가상현재',exact=True)).to_be_visible()
            expect(page.get_by_text('조회가상과거',exact=True)).to_have_count(0)
            page.get_by_role('button',name='날짜·상태 제한 해제',exact=True).click()
            expect(page.get_by_text('조회가상과거',exact=True)).to_be_visible()
            expect(page.locator('#rq-q')).to_have_value('조회가상')
            expect(page.locator('#rq-dong')).to_have_value('충장동')
            expect(page.get_by_label('접수 조회 범위',exact=True)).to_contain_text('전체 기간')
            expect(page.get_by_label('접수 조회 범위',exact=True)).to_contain_text('모든 상태')
            page.get_by_role('button',name='오늘',exact=True).click()
            expect(page.locator('#rq-from')).to_have_value(today)
            expect(page.get_by_text('조회가상현재',exact=True)).to_be_visible()
            expect(page.get_by_text('조회가상과거',exact=True)).to_have_count(0)
            for width in (980,390):
                page.set_viewport_size({'width':width,'height':1000})
                for label in ('어제','오늘','이번 달','전체 기간','날짜·상태 제한 해제'):
                    button=page.get_by_role('button',name=label,exact=True)
                    box=button.bounding_box()
                    assert box and box['x']>=0 and box['x']+box['width']<=width, (width,label,box)
            page.set_viewport_size({'width':1440,'height':1000})
            page.screenshot(path=str(output/'request-date-filters.png'),full_page=True)
            page.goto(url+'#/dongs')
            page.get_by_label('접수 검색어',exact=True).fill(records[0]['receipt_no'])
            page.get_by_role('button',name='조회',exact=True).click()
            expect(page.get_by_role('button',name=records[0]['receipt_no'],exact=True)).to_have_count(0)
            page.get_by_role('button',name='전체 기간 조회',exact=True).click()
            expect(page.get_by_role('button',name=records[0]['receipt_no'],exact=True)).to_be_visible()
            expect(page.get_by_label('업무 기준일',exact=True)).to_have_value(today)
            expect(page.get_by_label('접수 목록 구분',exact=True)).to_have_value('all')
            page.get_by_role('button',name='이전날',exact=True).click()
            expect(page.get_by_label('업무 기준일',exact=True)).not_to_have_value(today)
            page.get_by_role('button',name='오늘',exact=True).click()
            expect(page.get_by_label('업무 기준일',exact=True)).to_have_value(today)
            expect(page.get_by_label('접수 목록 구분',exact=True)).to_have_value('day')
            page.screenshot(path=str(output/'dong-date-filters.png'),full_page=True)
            assert not errors, errors
            print('PC 날짜 조회: 과거 완료 찾기·날짜/상태 해제·검색어/동 보존·오늘 프리셋·동별 전체기간·배차 날짜 분리·이전날/오늘 통과')
        except Exception:
            print('Browser errors:',errors)
            page.screenshot(path=str(output/'failure.png'),full_page=True)
            raise
        finally:
            context.close();browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--exe',type=Path)
    main(parser.parse_args().exe)
