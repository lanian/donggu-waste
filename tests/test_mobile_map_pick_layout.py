"""휴대폰 배출 위치·반입지 지도에서 가려짐 없이 조작하고 실제 저장한 좌표로 복귀한다."""
import os

from playwright.sync_api import expect, sync_playwright

from test_mobile_intake_save import FAIL_WRITE
from test_review_ui import mobile_server
from test_workflow_mobile import daylist, open_mobile, seed, stop


def assert_tappable(button):
    # locator.click()의 자동 스크롤 없이 지금 보이는 화면에서 버튼 전체를 누를 수 있어야 한다.
    result = button.evaluate("""e => {
        const r=e.getBoundingClientRect(), v=visualViewport;
        const ys=[r.top+6, r.top+r.height/2, r.bottom-6];
        return {top:r.top, bottom:r.bottom, height:r.height, viewport:v.height,
            inside:r.top>=v.offsetTop && r.bottom <= v.offsetTop+v.height+1,
            hit:ys.every(y=>e.contains(document.elementFromPoint(r.left+r.width/2,y)))};
    }""")
    assert result['inside'] and result['hit'] and result['height'] >= 44, (button.inner_text(), result)


def check(browser, url, mode, width, height, *, large=False):
    ctx, page = open_mobile(browser, url)
    try:
        page.set_viewport_size({'width': width, 'height': height})
        ctx.grant_permissions(['geolocation'])
        ctx.set_geolocation({'latitude': 35.147312, 'longitude': 126.928123, 'accuracy': 4})
        ctx.route('https://tiles.openfreemap.org/**', lambda r: r.fulfill(json={
            'version': 8, 'sources': {}, 'layers': [{'id': 'base', 'type': 'background'}]}))
        day = page.evaluate("import('./core.js').then(C=>C.todayKST())")
        record = stop(1, day, lat=35.141, lon=126.926, address='광주 동구 가상로 10 가상아파트 105동 수거장')
        seed(page, {'mode': mode, 'mapEnabled': True, 'jusoKey': 'fixture',
                    'daylist': daylist(day, [record]) if mode == 'PC' else None,
                    'requests': [record] if mode == 'SOLO' else []})
        if large:
            page.evaluate("""async () => {const C=await import('./core.js');
                C.st.textSize='xl'; C.applyTextSize(); await C.setPrefs({theme:'dark',hc:true});}""")
        page.evaluate("document.documentElement.style.setProperty('--safe-bottom','20px')")
        back = url + '#/stop/' + record['request_id']
        page.goto(back)
        page.evaluate("import('./core.js').then(C=>{window.__mapPickCore=C})")
        enter = page.get_by_role('button', name='위치 고치기', exact=True)
        save = page.get_by_role('button', name='이 위치로 저장', exact=True)
        gps = page.get_by_role('button', name='지금 내 위치로 이동', exact=True)
        cancel = page.get_by_role('button', name='← 취소', exact=True)
        pending_map = []
        if mode == 'PC':
            ctx.route('**/vendor/maplibre/maplibre-gl.mjs', lambda route: pending_map.append(route))
        enter.click()
        if mode == 'PC':
            # 고정 버튼에서 먼저 GPS를 누른 뒤 느리게 열린 지도가 원래 좌표로 되돌아가면 안 된다.
            assert_tappable(gps)
            gps.click()
            expect(page.locator('.gps-note')).to_contain_text('내 위치')
            assert len(pending_map) == 1
            pending_map[0].continue_()
            ctx.unroute('**/vendor/maplibre/maplibre-gl.mjs')
            page.wait_for_function("()=>!!window.__mapPickCore.liveMap")
            actual = page.evaluate("import('./core.js').then(C=>C.liveMap.getCenter().toArray())")
            assert all(abs(a-b)<1e-6 for a,b in zip(actual,[126.928123,35.147312])), actual
        expect(save).to_be_enabled()
        assert_tappable(save)
        assert_tappable(gps)
        assert_tappable(cancel)
        if large:
            search = page.get_by_role('searchbox', name='주소 검색어')
            search.focus()
            # 화면 키보드처럼 레이아웃은 그대로 두고 보이는 영역만 줄인다.
            page.evaluate("""() => {
                Object.defineProperty(visualViewport,'height',{configurable:true,value:320});
                visualViewport.dispatchEvent(new Event('resize'));
            }""")
            assert_tappable(save)
            assert_tappable(gps)
            expect(search).to_be_focused()
            page.evaluate("""() => {
                delete visualViewport.height;
                visualViewport.dispatchEvent(new Event('resize'));
            }""")
            search.blur()
            page.evaluate(FAIL_WRITE)
            assert not page.evaluate("import('./core.js').then(C=>C.commit())")
            expect(page.locator('#save-fail')).to_be_visible()
            assert_tappable(save)
            assert_tappable(gps)
            action_top = save.bounding_box()['y']
            banner = page.locator('#save-fail').bounding_box()
            assert banner['y'] + banner['height'] <= action_top, (banner, action_top)
            page.evaluate('window.__failIntake=false')
            assert page.evaluate("import('./core.js').then(C=>C.commit())")

        cancel.click()
        expect(page).to_have_url(back)
        before = page.evaluate("""async()=>{const C=await import('./core.js');
            return [C.findStop('workflow-1').lat, C.st.events.filter(e=>e.kind==='LOCATION_FIX').length];}""")
        assert before == [35.141, 0]
        enter.click()
        expect(save).to_be_enabled()
        canvas = page.locator('.mapbox canvas')
        canvas.scroll_into_view_if_needed()
        # 실제 보이는 지도 중앙에서 마우스로 이동한다. 좌표는 지도 크기에 상대적이어야 한다.
        region = canvas.evaluate("""e => {
            const r=e.getBoundingClientRect(), bar=document.querySelector('main .bar').getBoundingClientRect(),
                footer=document.querySelector('main button.primary.big').getBoundingClientRect();
            return {x:r.left+r.width*.45,y:(Math.max(bar.bottom,r.top)+Math.min(footer.top,r.bottom))/2};
        }""")
        x, y = region['x'], region['y']
        assert canvas.evaluate('(e,p)=>e.contains(document.elementFromPoint(p.x,p.y))', region)
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x+36, y, steps=6)
        page.mouse.up()
        page.wait_for_function("""()=>{const m=window.__mapPickCore.liveMap;
            return m && !m.isMoving() && Math.abs(m.getCenter().lng-126.926)>1e-6;}""")
        save.evaluate("""async b=>{const {liveMap}=await import('./core.js');
            b.addEventListener('click',()=>{const c=liveMap.getCenter();
                window.__pickedCenter=[Math.round(c.lat*1e6)/1e6,Math.round(c.lng*1e6)/1e6];
            },{capture:true,once:true});}""")
        assert_tappable(save)
        save.click()
        expect(page).to_have_url(back)
        stored = page.evaluate("""async()=>{const s=await (await import('./store.js')).getSecure('state');
            const p=s.mode==='SOLO'?s.requests[0]:s.events.find(e=>e.kind==='LOCATION_FIX').body;
            return [p.lat,p.lon];}""")
        assert stored == page.evaluate('window.__pickedCenter'), stored
        if large:
            page.goto(url+'#/unload')
            page.get_by_role('button', name='반입지 위치 지정', exact=True).click()
            assert_tappable(gps)
            gps.click()
            expect(page.locator('.gps-note')).to_contain_text('내 위치')
            assert_tappable(save)
            save.click()
            expect(page).to_have_url(url+'#/unload')
            site = page.evaluate("import('./store.js').then(async S=>(await S.getSecure('state')).unloadSite)")
            assert site == {'lat': 35.147312, 'lon': 126.928123}, site
        print(f'{width}×{height} {mode} 저장·GPS·취소 가림 없음, 지도 조작과 좌표 저장 확인', flush=True)
    finally:
        ctx.close()


if __name__ == '__main__':
    with mobile_server() as url, sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path,
                               args=['--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        try:
            check(b, url, 'PC', 360, 640)
            check(b, url, 'SOLO', 390, 844)
            check(b, url, 'SOLO', 320, 480, large=True)
        finally:
            b.close()
