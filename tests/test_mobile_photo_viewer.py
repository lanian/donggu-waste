"""암호화 현장 사진: 앱 안 전체 사진 조회·닫기·원본 불변 회귀."""
import os
from pathlib import Path
import tempfile

from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN, mobile_server
from test_mobile_photo_preview import photo_case, unchanged, release


def viewer_case(browser, url, shots):
    context, page, errors = photo_case(browser, url)
    try:
        context.grant_permissions(['clipboard-read', 'clipboard-write'])
        page.evaluate("navigator.clipboard.writeText('가상 사진 조회 이전 클립보드')")
        external = []
        page.on('request', lambda request: external.append(request.url) if request.url.startswith('http') and not request.url.startswith(url) else None)
        page.evaluate("""()=>{window.__viewerRevoked=[];const revoke=URL.revokeObjectURL;
          URL.revokeObjectURL=function(value){window.__viewerRevoked.push(value);return revoke.call(this,value);};}""")
        page.evaluate('async()=>{await window.__collectionCore.render();}')
        button = page.get_by_role('button', name='수거 전 사진 크게 보기', exact=True)
        expect(button).to_be_visible()
        button.click()
        dialog = page.get_by_role('dialog', name='현장 사진 크게 보기', exact=True)
        expect(dialog).to_be_visible()
        image = dialog.locator('.photo-viewer img')
        expect(image).to_be_visible()
        page.wait_for_function("()=>document.querySelector('.photo-viewer img')?.naturalWidth>0")
        assert image.evaluate('(img)=>getComputedStyle(img).objectFit') == 'contain'
        assert image.bounding_box()['width'] > 88
        expect(dialog).to_contain_text('수거 전')
        viewer_url = image.get_attribute('src')
        dialog.get_by_role('button', name='확대', exact=True).click()
        expect(dialog.get_by_role('button', name='맞춤 보기', exact=True)).to_have_attribute('aria-pressed', 'true')
        assert dialog.locator('.photo-view-stage').evaluate('(el)=>el.scrollWidth>el.clientWidth')
        dialog.get_by_role('button', name='맞춤 보기', exact=True).click()
        page.screenshot(path=str(shots / 'photo-whole-view.png'))
        dialog.get_by_role('button', name='닫기', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(button).to_be_focused()
        assert page.evaluate('value=>window.__viewerRevoked.includes(value)', viewer_url)
        assert unchanged(page)
        assert not errors, errors
        assert len(context.pages) == 1
        assert not external, external
        assert page.evaluate('navigator.clipboard.readText()') == '가상 사진 조회 이전 클립보드'
        button.click()
        expect(dialog).to_be_visible()
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        expect(button).to_be_focused()
        button.click()
        page.wait_for_function("()=>document.querySelector('.photo-viewer img')?.naturalWidth>0")
        locked_url = dialog.locator('.photo-viewer img').get_attribute('src')
        page.evaluate('async()=>{await window.__collectionCore.lockNow();}')
        expect(dialog).to_have_count(0)
        expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
        assert page.evaluate('value=>window.__viewerRevoked.includes(value)', locked_url)
        page.get_by_placeholder('PIN', exact=True).fill(PIN)
        page.get_by_role('button', name='잠금 해제', exact=True).click()
        page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
        expect(dialog).to_have_count(0)
        assert unchanged(page)
        print('PASS: 현장 사진을 앱 안에서 전체 보기·닫기·초점 복귀하고 원본을 보존함', flush=True)
    finally:
        context.close()


def failed_case(browser, url, shots, missing):
    context, page, errors = photo_case(browser, url)
    try:
        page.evaluate('async()=>{await window.__collectionCore.render();}')
        page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
        if missing:
            page.evaluate("""async()=>{const S=await import('./store.js');window.__savedPhoto=await S.getRaw('b:ph:preview-test');await S.delRaw('b:ph:preview-test');}""")
        else:
            page.evaluate('window.__photoFail=true')
        page.get_by_role('button', name='수거 전 사진 크게 보기', exact=True).click()
        dialog = page.get_by_role('dialog', name='현장 사진 크게 보기', exact=True)
        expect(dialog.get_by_role('button', name='사진 다시 불러오기', exact=True)).to_be_visible()
        expect(dialog).to_contain_text('사진 파일이 이 휴대폰에 없습니다.' if missing else '사진을 불러오지 못했습니다.')
        assert page.evaluate('()=>window.__collectionCore.st.events.filter(e=>e.kind==="PHOTO_META").length') == 1
        if missing:
            page.evaluate("async()=>{const S=await import('./store.js');await S.putRaw('b:ph:preview-test',window.__savedPhoto);}")
        else:
            page.evaluate('window.__photoFail=false')
        dialog.get_by_role('button', name='사진 다시 불러오기', exact=True).click()
        page.wait_for_function("()=>document.querySelector('.photo-viewer img')?.naturalWidth>0")
        expect(dialog.locator('.photo-viewer img')).to_be_visible()
        dialog.get_by_role('button', name='사진 닫기', exact=True).click()
        assert unchanged(page)
        assert not errors, errors
        print('PASS: 사진 ' + ('파일 없음' if missing else '읽기 실패') + ' 안내/재조회는 원본 기록을 변경하지 않음', flush=True)
    finally:
        context.close()


def late_case(browser, url, shots, lock):
    context, page, errors = photo_case(browser, url)
    try:
        page.evaluate('async()=>{await window.__collectionCore.render();}')
        page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
        page.evaluate('window.__photoHold=true')
        before = page.evaluate('window.__photoURLs.length')
        page.get_by_role('button', name='수거 전 사진 크게 보기', exact=True).click()
        page.wait_for_function('()=>window.__photoCallbacks.length>0')
        dialog = page.get_by_role('dialog', name='현장 사진 크게 보기', exact=True)
        if lock:
            page.evaluate('async()=>{await window.__collectionCore.lockNow();}')
            expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible()
        else:
            page.goto(url + '#/home')
        expect(dialog).to_have_count(0)
        release(page)
        assert page.evaluate('window.__photoURLs.length') == before, '늦은 읽기는 URL을 만들지 않음'
        if lock:
            page.get_by_placeholder('PIN', exact=True).fill(PIN)
            page.get_by_role('button', name='잠금 해제', exact=True).click()
            page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
            expect(dialog).to_have_count(0)
        assert unchanged(page)
        assert not errors, errors
        print('PASS: 사진 읽기 중 ' + ('잠금/PIN 재열기' if lock else '화면 이탈') + '는 늦은 사진 URL과 창을 차단함', flush=True)
    finally:
        context.close()


def back_case(browser, url, shots):
    context, page, errors = photo_case(browser, url)
    try:
        page.goto(url + '#/home')
        page.goto(url + '#/stop/workflow-1')
        page.get_by_role('button', name='수거 전 사진 크게 보기', exact=True).click()
        expect(page.get_by_role('dialog', name='현장 사진 크게 보기', exact=True)).to_be_visible()
        page.go_back()
        expect(page).to_have_url(url + '#/home')
        expect(page.get_by_role('dialog', name='현장 사진 크게 보기', exact=True)).to_have_count(0)
        assert unchanged(page)
        assert not errors, errors
        print('PASS: 브라우저 뒤로 가기는 사진 창을 닫고 원래 조회 화면으로 이동함', flush=True)
    finally:
        context.close()


def owner_case(browser, url, shots):
    context, page, errors = photo_case(browser, url)
    try:
        page.evaluate('async()=>{await window.__collectionCore.render();}')
        page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
        page.evaluate('window.__photoHold=true')
        page.get_by_role('button', name='수거 전 사진 크게 보기', exact=True).click()
        page.wait_for_function('()=>window.__photoCallbacks.length>0')
        page.evaluate("""async()=>{const C=window.__collectionCore;
          C.setSt({...C.st,daylist:{...C.st.daylist,stops:C.st.daylist.stops.map(s=>({...s}))}});await C.commit();await C.render();
        }""")
        expect(page.get_by_role('dialog', name='현장 사진 크게 보기', exact=True)).to_have_count(0)
        before = page.evaluate('window.__photoURLs.length')
        release(page)
        page.wait_for_function("()=>document.querySelector('.thumbs img')?.naturalWidth>0")
        # 새 화면의 정상 thumbnail read도 동시에 보류됐으므로 URL은 한 개만 늘어난다.
        assert page.evaluate('window.__photoURLs.length') == before + 1
        assert unchanged(page)
        assert not errors, errors
        print('PASS: 상태 교체 뒤 늦은 사진 조회는 이전 창을 다시 열지 않음', flush=True)
    finally:
        context.close()


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-photo-viewer-'))
    shots = output / 'shots'
    shots.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            viewer_case(browser, url, shots)
            failed_case(browser, url, shots, missing=False)
            failed_case(browser, url, shots, missing=True)
            late_case(browser, url, shots, lock=False)
            late_case(browser, url, shots, lock=True)
            back_case(browser, url, shots)
            owner_case(browser, url, shots)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
