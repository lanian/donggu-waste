"""등록 자료가 없는 휴대폰에서 세 종류의 기록을 남기고 PC용 파일을 복호화한다."""
import base64
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from playwright.sync_api import expect, sync_playwright

from test_review_ui import PIN, mobile_server
from test_workflow_mobile import open_mobile, route, seed


def main():
    with mobile_server() as url, sync_playwright() as pw, tempfile.TemporaryDirectory() as tmp:
        browser = pw.chromium.launch(executable_path=os.getenv('CHROME') or pw.chromium.executable_path)
        ctx, page = open_mobile(browser, url)
        try:
            seed(page, {}, registered=False)
            today = page.evaluate("async () => (await import('./core.js')).todayKST()")
            key = AESGCM.generate_key(bit_length=256)
            work = dict(schema='dg.guest-work/1', work_date=today, crew='가상팀', crew_id='mock',
                        assign_ver=1, issue_no=71, ticket='synthetic-ticket', return_key=base64.urlsafe_b64encode(key).decode().rstrip('='),
                        stops=[dict(seq=1,dong='시험동',address='시험길 1',items=[dict(name='의자',spec='',qty=2,unit='개')])])
            page.evaluate("async x=>(await import('./store.js')).putSecure('guestList', x)",work)
            route(page, '#/guest-list')
            page.get_by_role('button',name='1번 수거 결과 기록').click(timeout=3000)
            page.get_by_label('의자 수거 수량').fill('1')
            page.get_by_label('변동·미수거 사유').fill('1개 미배출')
            page.get_by_role('button',name='수거 결과 저장').click()
            expect(page.get_by_text('부분 수거',exact=True)).to_be_visible()
            page.get_by_role('button',name='현장 접수 추가').click()
            for label,value in [('신청인','가상 신청인'),('연락처','01000001234'),('배출 주소','시험로 9'),('행정동','시험동'),('품목명','책상')]:
                page.get_by_label(label,exact=True).fill(value)
            page.get_by_role('button',name='현장 접수 저장').click()
            page.get_by_role('button',name='하차 기록 추가').click()
            page.get_by_label('하차지').fill('가상 반입지')
            page.get_by_role('button',name='배차로 돌아가기').click()
            page.get_by_role('button',name='입력 중인 하차 기록 이어쓰기').click()
            expect(page.get_by_label('하차지')).to_have_value('가상 반입지')
            page.get_by_label('하차 중량(kg)').fill('17.5')
            page.get_by_role('button',name='하차 기록 저장').click()
            page.get_by_role('button',name='하차 기록 수정').click(timeout=3000)
            page.get_by_label('하차 중량(kg)').fill('18.5')
            page.get_by_role('button',name='하차 기록 저장').click()
            # 목록 삭제·만료와 별개로 저장한 기록을 다시 열고 파일로 전달할 수 있다.
            page.evaluate("async ()=>(await import('./store.js')).putSecure('guestList',null)")
            route(page, '#/home')
            route(page, '#/guest-list')
            page.evaluate("""() => {
                navigator.canShare=()=>true;
                navigator.share=async()=>{throw new DOMException('cancel','AbortError')};
            }""")
            route(page, '#/guest-list')
            page.get_by_role('button',name='결과 파일 공유').click()
            expect(page.get_by_text('공유를 취소했습니다.',exact=False)).to_be_visible()
            route(page, '#/guest-list')
            expect(page.get_by_role('button',name='하차 기록 수정')).to_be_visible(timeout=2000)
            expect(page.get_by_role('button',name='반영한 기록 정리')).to_have_count(0)
            page.evaluate("() => { navigator.share=async()=>{throw new DOMException('gesture','NotAllowedError')}; }")
            page.get_by_role('button',name='결과 파일 공유').click()
            expect(page.get_by_text('[결과 파일 저장]을 사용하세요.',exact=False)).to_be_visible()
            # 공유를 기다리는 사이 파일 저장이 성공한 후의 취소는 그 성공을 되돌리지 않는다.
            page.evaluate("""() => { navigator.share=()=>new Promise((_,reject)=>{
                window.cancelGuestShare=()=>reject(new DOMException('cancel','AbortError'));
                document.body.dataset.guestShare='waiting';
            }); }""")
            page.get_by_role('button',name='결과 파일 공유').click()
            expect(page.locator('body')).to_have_attribute('data-guest-share','waiting')
            with page.expect_download() as download:
                page.get_by_role('button',name='결과 파일 저장').click()
            page.evaluate("window.cancelGuestShare()")
            file = download.value
            assert file.suggested_filename.endswith('.dgwork')
            dest = Path(tmp)/'out.dgwork'
            file.save_as(dest)
            pack = json.loads(dest.read_text('utf-8'))
            decode = lambda s: base64.urlsafe_b64decode(s + '=' * (-len(s)%4))
            records = json.loads(AESGCM(key).decrypt(decode(pack['iv']),decode(pack['ciphertext']),work['ticket'].encode()))['entries']
            assert [r['kind'] for r in records]==['collection','intake','unload']
            assert records[0]['qty']==[1] and records[1]['name']=='가상 신청인' and records[2]['weight_kg']==18.5
            assert len({r['id'] for r in records})==3
            expect(page.get_by_role('button',name='하차 기록 수정')).to_have_count(0)
            with page.expect_download() as retransmit:
                page.get_by_role('button',name='결과 파일 저장').click()
            retransmit.value.save_as(dest)
            repeat=json.loads(dest.read_text('utf8'))
            assert json.loads(AESGCM(key).decrypt(decode(repeat['iv']),decode(repeat['ciphertext']),work['ticket'].encode()))['entries']==records
            page.get_by_role('button',name='반영한 기록 정리').click()
            page.get_by_role('button',name='취소',exact=True).click()
            expect(page.get_by_role('button',name='결과 파일 저장')).to_be_visible()
            assert page.evaluate("async () => !(await (await import('./store.js')).getRaw('device'))")
            # 이미 그려 둔 화면에서 보관 기록이 외부 정리된 상태를 재현한다.
            page.evaluate("""async () => {
                const S=await import('./store.js'), groups=await S.getSecure('guestWorkJournal');
                Object.assign(groups[0], {closed:true,entries:[],exported:0,revision:groups[0].revision+1});
                delete groups[0].return_key;
                await S.putSecure('guestWorkJournal',groups);
            }""")
            page.get_by_role('button',name='결과 파일 저장').click()
            expect(page.get_by_text('다른 창에서 현장 기록이 바뀌었습니다.',exact=False)).to_be_visible(timeout=3000)
            replacement = {**work, 'ticket':'synthetic-second-ticket', 'issue_no':72}
            page.evaluate("async x=>(await import('./store.js')).putSecure('guestList', x)",replacement)
            route(page,'#/guest-list')
            page.get_by_role('button',name='하차 기록 추가').click()
            # 바로 잠가도 마지막 입력을 저장하고 다음날에는 현재 목록 없이 초안만 복원한다.
            page.get_by_label('하차지').fill('야간 반입지')
            page.evaluate("async ()=>(await import('./core.js')).lockNow()")
            tomorrow = page.evaluate("async ()=>(await import('./core.js')).todayKST(1)")
            page.clock.set_fixed_time(datetime.fromisoformat(tomorrow+'T08:00:00+09:00'))
            page.get_by_label('PIN',exact=True).fill(PIN)
            page.get_by_role('button',name='잠금 해제',exact=True).click()
            expect(page.get_by_label('PIN',exact=True)).to_have_count(0)
            route(page,'#/guest-list')
            expect(page.get_by_role('button',name='1번 수거 결과 기록')).to_have_count(0)
            page.get_by_role('button',name='입력 중인 하차 기록 이어쓰기').click()
            expect(page.get_by_label('하차지')).to_have_value('야간 반입지')
            page.get_by_label('하차 중량(kg)').fill('20')
            page.get_by_role('button',name='하차 기록 저장').click()
            expect(page.get_by_role('button',name='결과 파일 저장')).to_have_count(1)
            page.evaluate("() => { navigator.share=async()=>{}; }")
            page.get_by_role('button',name='결과 파일 공유').click()
            expect(page.get_by_role('button',name='반영한 기록 정리')).to_be_visible()
            print('기기 등록 없이 부분 수거·접수·하차 저장, 목록 교체 뒤 암호화 결과 파일 내보내기 통과')
        finally:
            ctx.close()
            browser.close()


if __name__=='__main__':
    main()
