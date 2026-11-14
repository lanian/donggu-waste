"""암호화 사진 미리보기: 잠금/화면 이동 중 읽기 정착과 현재 화면 오류 안내."""
import argparse
import base64
import io
import json
import os
from pathlib import Path
import tempfile

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_mobile_collection_save import open_case


def photo_case(browser, url):
    context, page, receipt = open_case(browser, url)
    data = io.BytesIO()
    Image.new('RGB', (8, 8), '#0b7a55').save(data, 'JPEG')
    page.evaluate("""async image=>{
        const C=window.__collectionCore,S=await import('./store.js');
        await S.putSecureBytes('ph:preview-test',Uint8Array.from(atob(image),c=>c.charCodeAt(0)));
        await C.addEvent(C.st.daylist.stops[0],'PHOTO_META',{photo_id:'preview-test',phase:'BEFORE',taken_at:new Date().toISOString()});
        window.__photoReadOriginal=IDBObjectStore.prototype.get;
        window.__photoCompleteOriginal=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'oncomplete');
        window.__photoCallbacks=[];window.__photoHold=false;window.__photoFail=false;
        window.__photoURLs=[];const create=URL.createObjectURL;
        URL.createObjectURL=function(blob){const u=create.call(this,blob);window.__photoURLs.push(u);return u;};
        IDBObjectStore.prototype.get=function(key){
            if(key==='b:ph:preview-test') {
                if(window.__photoFail)throw new DOMException('synthetic preview read failure','UnknownError');
                if(window.__photoHold)this.transaction.__previewRead=true;
            }
            return window.__photoReadOriginal.apply(this,arguments);
        };
        Object.defineProperty(IDBTransaction.prototype,'oncomplete',{...window.__photoCompleteOriginal,set(fn){
            window.__photoCompleteOriginal.set.call(this,typeof fn!=='function'?fn:function(event){
                if(this.__previewRead)window.__photoCallbacks.push(()=>fn.call(this,event));
                else fn.call(this,event);
            });
        }});
        window.__photoSerial=record=>JSON.stringify({iv:[...new Uint8Array(record.iv)],ct:[...new Uint8Array(record.ct)]});
        window.__photoRawBefore=window.__photoSerial(await S.getRaw('b:ph:preview-test'));
    }""", base64.b64encode(data.getvalue()).decode())
    errors = []
    page.on('pageerror', lambda error: errors.append({'message': str(error), 'stack': getattr(error, 'stack', '')}))
    return context, page, errors


def unchanged(page):
    return page.evaluate("""async()=>{
        const C=window.__collectionCore,S=await import('./store.js');
        return window.__photoSerial(await S.getRaw('b:ph:preview-test'))===window.__photoRawBefore
            && C.st.events.filter(e=>e.kind==='PHOTO_META').length===1;
    }""")


def hold(page):
    page.evaluate('async()=>{window.__photoHold=true;await window.__collectionCore.render();}')
    page.wait_for_function('()=>window.__photoCallbacks.length>0')


def release(page):
    page.evaluate('()=>{window.__photoHold=false;window.__photoCallbacks.splice(0).forEach(fn=>fn());}')
    page.wait_for_timeout(200)


def lock_case(browser, url, shots):
    context, page, errors = photo_case(browser, url)
    try:
        hold(page)
        page.evaluate('async()=>{await window.__collectionCore.lockNow();}')
        expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
        release(page)
        assert not errors, f'잠금 뒤 사진 읽기 오류가 처리되지 않음: {errors}'
        assert page.evaluate('window.__photoURLs.length') == 0, '잠금 뒤 옛 미리보기 URL 생성 금지'
        page.get_by_placeholder('PIN', exact=True).fill(PIN)
        page.get_by_role('button', name='잠금 해제', exact=True).click()
        page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
        assert unchanged(page), '잠금 경쟁은 사진 암호문/메타데이터를 변경하지 않는다'
        page.screenshot(path=str(shots / 'photo-after-lock.png'))
        print('사진 미리보기: 읽기중잠금·LOCKED처리·옛URL차단·재잠금해제정상표시·원본보존 통과', flush=True)
    finally:
        context.close()


def stale_case(browser, url, shots):
    context, page, errors = photo_case(browser, url)
    try:
        hold(page)
        page.goto(url + '#/home')
        expect(page.locator('.home-tools')).to_be_visible()
        release(page)
        assert not errors, errors
        assert page.evaluate('window.__photoURLs.length') == 0, '화면을 떠난 사진 읽기는 URL을 만들지 않는다'
        assert unchanged(page)
        print('사진 미리보기: 화면이동뒤늦은읽기·옛URL차단·원본보존 통과', flush=True)
    finally:
        context.close()


def failure_case(browser, url, shots):
    context, page, errors = photo_case(browser, url)
    try:
        page.evaluate('async()=>{window.__photoFail=true;await window.__collectionCore.render();}')
        expect(page.get_by_text('사진 미리보기를 불러오지 못했습니다. 사진 기록은 유지됩니다.', exact=True)).to_be_visible()
        assert not errors, errors
        page.evaluate('window.__photoFail=false')
        assert unchanged(page)
        page.get_by_text('사진 미리보기를 불러오지 못했습니다. 사진 기록은 유지됩니다.', exact=True).scroll_into_view_if_needed()
        page.screenshot(path=str(shots / 'photo-preview-error.png'))
        page.evaluate('async()=>{window.__photoFail=false;await window.__collectionCore.render();}')
        page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
        expect(page.get_by_text('사진 미리보기를 불러오지 못했습니다. 사진 기록은 유지됩니다.', exact=True)).to_have_count(0)
        assert unchanged(page)
        print('사진 미리보기: 현재화면읽기실패안내·오류처리·원본유지·다시그리기복구 통과', flush=True)
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=('all', 'lock', 'stale', 'failure'), default='all')
    args = parser.parse_args()
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-photo-preview-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    failures = []
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            for name, case in [('lock', lock_case), ('stale', stale_case), ('failure', failure_case)]:
                if args.case not in ('all', name):
                    continue
                try:
                    case(browser, url, shots)
                except Exception as error:
                    failures.append({'case': name, 'error': str(error)})
                    print(name + ' FAIL: ' + str(error), flush=True)
        finally:
            browser.close()
    if failures:
        (output / 'failures.json').write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding='utf-8')
        raise AssertionError(f'{len(failures)} photo preview cases failed')


if __name__ == '__main__':
    main()
