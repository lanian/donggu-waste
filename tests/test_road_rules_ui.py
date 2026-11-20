"""PC 도로 통행 정보 화면과 작업 폴링을 가상 HTTP 응답으로 확인한다."""
import os
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

from test_pc_session_revocation import SessionHTTPTestCase


ROAD = {'id': 'school-link', 'name': '가상 학교길', 'coords': [[126.9224, 35.146], [126.9238, 35.146]], 'distance_m': 12}
OTHER = {'id': 'side-link', 'name': '가상 샛길', 'coords': [[126.9228, 35.1455], [126.9228, 35.146]], 'distance_m': 34}
DETOUR = [ROAD['coords'][1], [126.926, 35.148], [126.921, 35.148], ROAD['coords'][0]]
DATE = '2026-11-18'
EVIDENCE = '가상 현장 안내 표지 확인'


def trip(distance, seconds, coordinates):
    return {'code': 'Ok', 'distance_m': distance, 'duration_s': seconds, 'coordinates': coordinates}


class RoadRulesAPI:
    def __init__(self, context, origin, *, offline=False):
        self.state = {'available': True, 'active_id': 'base', 'previous_id': None, 'rules': [], 'busy': False, 'pending': None}
        self.posts = []
        self.lookups = []
        self.jobs = {}
        self.hold_preview = False
        self.bad_trip = False
        self.fail_status = False
        self.fail_lookup = False
        self.reject_apply = False
        context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(origin) else r.abort())
        context.route('https://tiles.openfreemap.org/**', lambda r: r.abort() if offline else r.fulfill(
            json={'version': 8, 'sources': {}, 'layers': [{'id': 'fixture', 'type': 'background', 'paint': {'background-color': '#edf2f6'}}]}))
        context.route(origin + 'api/road-rules**', self.rules)
        context.route(origin + 'api/jobs/*', self.poll)

    def rules(self, route):
        url = urlsplit(route.request.url)
        operation = url.path.removeprefix('/api/road-rules').strip('/')
        if not operation:
            if self.fail_status:
                return route.fulfill(status=503, json={'error': '가상 상태 조회 오류'})
            return route.fulfill(json=self.state)
        if operation == 'roads':
            self.lookups.append(parse_qs(url.query))
            if self.fail_lookup:
                return route.fulfill(status=503, json={'error': '가상 도로 후보 조회 오류'})
            return route.fulfill(json={'roads': [ROAD, OTHER]})
        body = route.request.post_data_json
        self.posts.append((operation, body))
        if operation == 'apply' and self.reject_apply:
            self.state.update(active_id='판-다른', previous_id='base', pending=None)
            return route.fulfill(status=409, json={'error': '도로 자료의 버전이 바뀌었습니다.'})
        job = str(len(self.posts))
        self.jobs[job] = (operation, body)
        route.fulfill(status=202, json={'job_id': job, 'status': 'QUEUED'})

    def poll(self, route):
        job = urlsplit(route.request.url).path.rsplit('/', 1)[-1]
        operation, body = self.jobs[job]
        if operation == 'preview':
            if self.hold_preview:
                return route.fulfill(json={'status': 'RUNNING'})
            road = ROAD if body['road_id'] == ROAD['id'] else OTHER
            before = {'forward': trip(140, 17, road['coords']), 'reverse': trip(140, 27, road['coords'][::-1])}
            after = ({**before, 'reverse': trip(650, 125, DETOUR)} if body['direction'] == 'forward' else
                     {**before, 'forward': {'code': 'NoRoute', 'distance_m': None, 'duration_s': None, 'coordinates': []}})
            if self.bad_trip:
                after['reverse'] = {'code': 'NoSegment', 'distance_m': None, 'duration_s': None, 'coordinates': []}
            result = {'preview_id': 'preview-' + job, 'road': road, 'before': before, 'after': after,
                      **{k: body[k] for k in ('base_id', 'direction', 'checked_on', 'evidence')}}
            self.state['pending'] = result
        elif operation == 'apply':
            pending = self.state['pending']
            assert pending['preview_id'] == body['preview_id'], body
            rules = [{'road_id': pending['road']['id'], 'name': pending['road']['name'],
                      **{k: pending[k] for k in ('direction', 'checked_on', 'evidence')}}]
            self.state.update(active_id='판-001', previous_id=self.state['active_id'], pending=None, rules=rules)
            result = self.state
        else:
            assert operation == 'rollback' and body['active_id'] == self.state['active_id'], body
            self.state.update(active_id=self.state['previous_id'], previous_id=None, pending=None, rules=[])
            result = self.state
        route.fulfill(json={'status': 'DONE', 'result': result})


def open_tab(page, origin):
    page.goto(origin + '#/settings/basic')
    tab = page.get_by_role('navigation', name='설정 묶음').get_by_role('link', name='도로 통행 정보')
    expect(tab).to_be_visible(timeout=3000)
    tab.click()
    expect(tab).to_have_attribute('aria-current', 'page')


def click_map(page):
    canvas = page.get_by_role('region', name='도로 구간 지도').locator('canvas.maplibregl-canvas')
    canvas.click(position={'x': 165, 'y': 135})


def select_road(page, direction='forward'):
    page.get_by_role('button', name='가상 학교길', exact=False).click()
    page.get_by_label('허용 방향').select_option(direction)
    page.get_by_label('확인일').fill(DATE)
    page.get_by_label('확인 근거').fill(EVIDENCE)


def features(page):
    return page.evaluate("""async () => {
      const {liveMaps} = await import('/core.js');
      return liveMaps.at(-1)?.getSource('road-rules')?.serialize().data.features || [];
    }""")


def apply_and_rollback(page, origin, mock):
    open_tab(page, origin)
    expect(page.get_by_role('region', name='도로 자료 버전')).to_contain_text('base')
    expect(page.get_by_role('button', name='이전 판으로 되돌리기')).to_be_disabled()
    click_map(page)
    expect(page.get_by_role('button', name='가상 샛길', exact=False)).to_contain_text('34m')
    lon, lat = (float(mock.lookups[-1][key][0]) for key in ('lon', 'lat'))
    assert 126.9 < lon < 127.0 and 35.1 < lat < 35.2
    select_road(page)
    expect(page.get_by_label('끝점 A', exact=True)).to_be_visible()
    expect(page.get_by_label('끝점 B', exact=True)).to_be_visible()
    page.get_by_role('button', name='양방향 미리보기').click()
    result = page.get_by_role('region', name='양방향 미리보기')
    expect(result).to_contain_text('0.65 km')
    expect(result).to_contain_text('2분 5초')
    expect(result).to_contain_text('우회')
    assert mock.posts == [('preview', {'road_id': ROAD['id'], 'direction': 'forward', 'checked_on': DATE,
                                       'evidence': EVIDENCE, 'base_id': 'base'})]
    page.get_by_label('지도 검증 방향').select_option('reverse')
    page.get_by_role('button', name='적용 전', exact=True).click()
    assert [f['geometry']['coordinates'] for f in features(page)] == [ROAD['coords'], ROAD['coords'][::-1]]
    page.get_by_role('button', name='적용 후', exact=True).click()
    assert [f['geometry']['coordinates'] for f in features(page)] == [ROAD['coords'], DETOUR]

    apply = page.get_by_role('button', name='확인 후 적용', exact=True)
    with page.expect_response('**/api/road-rules/roads?*'):
        click_map(page)
    expect(apply).to_be_enabled(timeout=2000)
    page.get_by_role('button', name='가상 학교길', exact=False).click()
    expect(apply).to_be_enabled(timeout=2000)
    assert len(mock.posts) == 1

    apply.click()
    dlg = page.get_by_role('dialog')
    expect(dlg).to_contain_text('가상 학교길')
    dlg.get_by_role('button', name='취소').click()
    assert len(mock.posts) == 1
    evidence = page.get_by_label('확인 근거')
    evidence.fill('수정한 가상 안내 표지')
    expect(apply).to_be_hidden()
    expect(page.get_by_text('입력 내용이 바뀌었습니다', exact=False)).to_be_visible(timeout=2000)
    evidence.fill(EVIDENCE)
    expect(apply).to_be_hidden()
    page.get_by_role('button', name='양방향 미리보기').click()
    apply.click()
    dlg.get_by_role('button', name='적용', exact=True).click()
    expect(page.get_by_role('region', name='도로 자료 버전')).to_contain_text('판-001')
    current = page.get_by_role('region', name='적용 규칙')
    for wanted in (ROAD['name'], 'A→B', DATE, EVIDENCE):
        expect(current).to_contain_text(wanted)
    assert mock.posts[-1] == ('apply', {'preview_id': 'preview-2'})
    page.get_by_role('button', name='이전 판으로 되돌리기').click()
    expect(dlg).to_contain_text('base')
    dlg.get_by_role('button', name='되돌리기', exact=True).click()
    expect(current).to_contain_text('적용된 규칙이 없습니다')
    assert mock.posts[-1] == ('rollback', {'active_id': '판-001'})


def changed_road_requires_preview(page, origin, mock):
    open_tab(page, origin)
    click_map(page)
    select_road(page)
    page.get_by_role('button', name='양방향 미리보기').click()
    apply = page.get_by_role('button', name='확인 후 적용', exact=True)
    expect(apply).to_be_enabled()
    page.get_by_role('button', name='가상 샛길', exact=False).click()
    expect(apply).to_be_hidden()
    expect(page.get_by_label('허용 방향')).to_have_value('')
    assert [op for op, _ in mock.posts] == ['preview']


def stale_and_offline(page, origin, mock):
    open_tab(page, origin)
    expect(page.get_by_text('바탕 지도를 불러오지 못했습니다', exact=False)).to_be_visible()
    mock.fail_lookup = True
    click_map(page)
    expect(page.get_by_text('가상 도로 후보 조회 오류', exact=False)).to_be_visible()
    mock.fail_lookup = False
    click_map(page)
    select_road(page, 'reverse')
    mock.hold_preview = True
    preview = page.get_by_role('button', name='양방향 미리보기')
    apply = page.get_by_role('button', name='확인 후 적용')
    with page.expect_response('**/api/road-rules/preview'):
        preview.click()
    expect(preview).to_be_disabled()
    page.get_by_label('확인 근거').fill('바뀐 가상 안내 표지')
    mock.hold_preview = False
    expect(preview).to_be_enabled()
    expect(apply).to_be_hidden()
    assert len(mock.posts) == 1
    preview.click()
    result = page.get_by_role('region', name='양방향 미리보기')
    expect(result).to_contain_text('경로 없음')
    page.get_by_label('지도 검증 방향').select_option('forward')
    assert [f['geometry']['coordinates'] for f in features(page)] == [ROAD['coords']]
    page.reload()
    expect(apply).to_be_enabled()
    expect(page.get_by_label('확인 근거')).to_have_value('바뀐 가상 안내 표지')
    assert len(mock.posts) == 2
    mock.reject_apply = True
    apply.click()
    page.get_by_role('dialog').get_by_role('button', name='적용', exact=True).click()
    expect(page.get_by_text('도로 자료의 버전이 바뀌었습니다', exact=False)).to_be_visible()
    expect(page.get_by_role('region', name='도로 자료 버전')).to_contain_text('판-다른')
    expect(apply).to_be_hidden()
    preview.click()
    expect(apply).to_be_enabled()
    assert mock.posts[-1][1]['base_id'] == '판-다른'


def status_errors(page, origin, mock):
    mock.fail_status = True
    open_tab(page, origin)
    expect(page.get_by_text('가상 상태 조회 오류', exact=False)).to_be_visible()
    refresh = page.get_by_role('button', name='상태 새로고침')
    mock.fail_status = False
    mock.state.update(available=False, reason='가상 오프라인 도로 자료 없음')
    refresh.click()
    expect(page.get_by_text('가상 오프라인 도로 자료 없음')).to_be_visible()
    expect(page.get_by_role('region', name='도로 구간 지도')).to_be_hidden()
    mock.state.update(available=True, busy=True)
    refresh.click()
    expect(page.get_by_text('다른 도로 작업이 진행 중입니다', exact=False)).to_be_visible()
    click_map(page)
    select_road(page)
    preview = page.get_by_role('button', name='양방향 미리보기')
    expect(preview).to_be_disabled()
    mock.state['busy'] = False
    refresh.click()
    expect(preview).to_be_enabled()
    assert not mock.posts
    mock.bad_trip = True
    preview.click()
    expect(page.get_by_text('경로를 확인하지 못했습니다', exact=False).first).to_be_visible()
    expect(page.get_by_role('button', name='확인 후 적용')).to_be_disabled()


def rollback_without_edit_kit(page, origin, mock):
    mock.state.update(available=False, reason='가상 편집 자료 없음', active_id='판-001', previous_id='base', busy=True)
    open_tab(page, origin)
    versions = page.get_by_role('region', name='도로 자료 버전')
    restore = page.get_by_role('button', name='이전 판으로 되돌리기')
    expect(versions).to_contain_text('판-001')
    expect(restore).to_be_disabled()
    refresh = page.get_by_role('button', name='상태 새로고침')
    mock.state['busy'] = False
    mock.fail_status = True
    refresh.click()
    expect(page.get_by_text('가상 상태 조회 오류', exact=False)).to_be_visible()
    expect(restore).to_be_disabled()
    mock.fail_status = False
    refresh.click()
    expect(restore).to_be_enabled(timeout=2000)
    expect(page.get_by_role('region', name='도로 구간 지도')).to_be_hidden()
    expect(page.get_by_role('button', name='양방향 미리보기')).to_be_hidden()
    expect(page.get_by_role('button', name='확인 후 적용')).to_be_hidden()
    restore.click()
    dialog = page.get_by_role('dialog')
    expect(dialog).to_contain_text('기본 도로 자료 (base)')
    dialog.get_by_role('button', name='되돌리기', exact=True).click()
    expect(versions).not_to_contain_text('판-001')
    expect(restore).to_be_disabled()
    assert mock.posts == [('rollback', {'active_id': '판-001'})] and not mock.lookups


def main():
    fixture = SessionHTTPTestCase()
    fixture.setUp()
    try:
        origin = f'http://127.0.0.1:{fixture.port}/'
        name, value = fixture.admin_cookie.split('=', 1)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
            for case in (rollback_without_edit_kit, apply_and_rollback, changed_road_requires_preview,
                         stale_and_offline, status_errors):
                with browser.new_context(viewport={'width': 1440, 'height': 1050}, service_workers='block') as context:
                    mock = RoadRulesAPI(context, origin, offline=case is stale_and_offline)
                    context.add_cookies([{'name': name, 'value': value, 'url': origin}])
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    case(page, origin, mock)
                    assert not errors, errors
                    print(f'PASS {case.__name__}')
            browser.close()
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    main()
