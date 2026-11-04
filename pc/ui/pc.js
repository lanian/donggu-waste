// 동구 대형폐기물 PC 업무 화면 (이 PC의 127.0.0.1 서버만 사용) — 시작점: 라우터(주소 → 화면)·틀(메뉴·머리)·단축키.
// 화면은 screens/*.js, 공통 도우미는 core.js·widgets.js. 새 화면: screens/이름.js 에 screenX 를 만들고 아래 screens·NAV·GROUPS 에 넣은 뒤
// 서버(pc/dgpc/server.py UI_FILES)에 파일을 더한다.
import * as NAV from '/web/nav.js';
import { $app, api, applyPrefs, boot, btn, clearMaps, dlgStack, go, h, pendingActions, post, prefsControls, setBoot, setRender, stopSessionTimer, touchSession } from './core.js';
import { screenAccess } from './screens/access.js';
import { screenClose } from './screens/close.js';
import { screenDispatch } from './screens/dispatch.js';
import { screenFees } from './screens/fees.js';
import { screenHelp } from './screens/help.js';
import { paintBadges, refreshBadges, screenHome } from './screens/home.js';
import { screenLogin } from './screens/login.js';
import { screenPilot } from './screens/pilot.js';
import { screenQrTest } from './screens/qrtest.js';
import { screenRefunds } from './screens/refunds.js';
import { resetIntake, screenRequest } from './screens/request.js';
import { screenRequests } from './screens/requests.js';
import { screenResults } from './screens/results.js';
import { screenRevenue } from './screens/revenue.js';
import { screenSend } from './screens/send.js';
import { SETTING_TABS, screenSettings } from './screens/settings.js';
import { screenStats } from './screens/stats.js';
import { screenMe, screenUsers } from './screens/users.js';

// ------------------------------------------------------------ 틀·라우팅
let renderSeq = 0;
async function render(fromNav) {
  // 작업 끝의 다시 그리기(fromNav 아님)인데 진행 중인 작업이 모두 다른 화면에서 시작했으면, 지금 화면을 덮지 않는다
  if (fromNav !== true && pendingActions.size && !pendingActions.has(location.hash)) return;
  const my = ++renderSeq;
  clearMaps();
  let view;
  try {
    if (!boot) {
      const st = await api('state');
      if (!st.logged_in) { view = await screenLogin(); }
      else { setBoot(await api('bootstrap')); touchSession(); }
    }
    if (boot) {
      // 주소 뒤 조건(#/requests?quick=OVERDUE 등): 화면 상태에 한 번 넣고 주소에서는 지운다(다시 그려도 사용자가 바꾼 조건을 덮지 않게)
      const [pathPart, query] = (location.hash || '#/home').split('?');
      if (query) {
        const q = new URLSearchParams(query), r0 = pathPart.split('/')[1];
        if (r0 === 'requests' && q.get('quick')) screenRequests.f = { ...(screenRequests.f || {}), q: '', status: '', date_from: '', date_to: '', dong: '', crew: '', pay: '', channel: '', quick: q.get('quick'), page: 1 };
        if (r0 === 'close' && /^\d{4}-\d\d-\d\d$/.test(q.get('date') || '')) screenClose.s = { date: q.get('date') };
        if (r0 === 'refunds' && q.has('status')) screenRefunds.s = { status: q.get('status') };
        if (q.get('focus')) render.focus = q.get('focus');
        history.replaceState(null, '', pathPart);
      }
      const [, route0, arg] = pathPart.split('/');
      const route = route0;
      const role = boot.me.role;
      applyPrefs(boot.me.prefs);
      const screens = { home: screenHome, requests: screenRequests, request: () => screenRequest(arg), dispatch: screenDispatch, send: screenSend, results: screenResults, close: screenClose, qrtest: screenQrTest, fees: screenFees, stats: screenStats, pilot: screenPilot,
        settings: () => screenSettings(arg), users: screenUsers, me: screenMe, revenue: screenRevenue, refunds: screenRefunds, access: screenAccess, help: screenHelp };
      // 역할별 메뉴 (서버도 같은 기준으로 막는다)
      const NAV = [['home', '오늘 현황', 'ADMIN INTAKE FIELD LEAD'], ['requests', '접수', 'ADMIN INTAKE FIELD LEAD'], ['dispatch', '배정·노선', 'ADMIN INTAKE'], ['send', '목록 전송', 'ADMIN INTAKE'],
        ['results', '결과 가져오기', 'ADMIN INTAKE FIELD'], ['close', '일일 마감', 'ADMIN INTAKE FIELD LEAD'], ['revenue', '세입·대사', 'ADMIN INTAKE LEAD'], ['refunds', '환불', 'ADMIN INTAKE LEAD'],
        ['qrtest', 'QR 실측', 'ADMIN INTAKE FIELD'], ['stats', '통계·보고서', 'ADMIN INTAKE LEAD'], ['pilot', '시범 운영 비교', 'ADMIN INTAKE LEAD'], ['fees', '수수료표', 'ADMIN INTAKE FIELD LEAD'], ['settings', '설정', 'ADMIN'],
        ['users', role === 'ADMIN' ? '사용자·이력' : '변경 이력', 'ADMIN LEAD'], ['access', '접속기록', 'ADMIN LEAD']];
      const nav = NAV.filter(([, , r]) => r.split(' ').includes(role));
      let cur = route || 'home';
      if (boot.me.must_change) cur = 'me';
      if (!['request', 'me', 'help'].includes(cur) && !nav.some(([k]) => k === cur)) cur = 'home';
      const content = await (screens[cur] || screenHome)();
      const active = cur === 'request' ? 'requests' : cur;
      // 메뉴 묶음 (행정 업무 시스템처럼 업무 단위로 묶는다). 묶음 이름은 제목(heading)이 아니라 글자로만 둔다.
      const GROUPS = [['업무 현황', ['home']], ['접수 업무', ['requests']], ['배차·수거', ['dispatch', 'send', 'results']], ['정산·마감', ['close', 'revenue', 'refunds', 'stats', 'pilot']], ['관리', ['fees', 'qrtest', 'settings', 'users', 'access']]];
      const ICON = { home: '현', requests: '접', dispatch: '배', send: '전', results: '결', close: '마', revenue: '세', refunds: '환', stats: '통', fees: '수', qrtest: 'Q', settings: '설', users: '이', access: '기' };
      const navLabel = Object.fromEntries(NAV.map(([k, t]) => [k, t]));
      const group = GROUPS.find(([, ks]) => ks.includes(active));
      const pageName = cur === 'request' ? (arg === 'new' ? '전화 접수 등록' : '접수 상세') : cur === 'me' ? '내 계정' : cur === 'help' ? '도움말' : navLabel[active];
      const subName = cur === 'settings' ? (SETTING_TABS.find(([k]) => k === arg) || SETTING_TABS[0])[1] : null;
      const crumbs = [group ? group[0] : '개인', ...(cur === 'request' ? [navLabel.requests] : []), pageName, ...(subName ? [subName] : [])];
      const d = new Date(boot.today + 'T00:00:00');
      const dayTxt = isNaN(d) ? boot.today : `${boot.today.replace(/-/g, '.')} (${'일월화수목금토'[d.getDay()]})`;
      const logout = async (e) => { e.preventDefault(); await post('logout').catch(() => {}); setBoot(null); resetIntake(); stopSessionTimer(); document.querySelectorAll('.sess-warn').forEach((x) => x.remove()); render(); };
      const navOpen = render.navOpen || false;
      const toggleNav = () => { render.navOpen = !render.navOpen; const sh = $app.querySelector('.shell'); sh.classList.toggle('nav-open', render.navOpen); sh.querySelector('.menu-btn').setAttribute('aria-expanded', render.navOpen ? 'true' : 'false'); };
      const railBtn = () => { render.rail = !render.rail; try { localStorage.setItem('dg-rail', render.rail ? '1' : ''); } catch {} $app.querySelector('.shell').classList.toggle('rail', render.rail); };
      if (render.rail === undefined) { try { render.rail = localStorage.getItem('dg-rail') === '1'; } catch { render.rail = false; } }
      view = h('div', { class: 'shell' + (navOpen ? ' nav-open' : '') + (render.rail ? ' rail' : '') },
        h('header', { class: 'topbar' },
          h('button', { type: 'button', class: 'menu-btn', 'aria-label': '메뉴 열기·닫기', 'aria-expanded': navOpen ? 'true' : 'false', 'aria-controls': 'side-nav', onclick: toggleNav }, '☰'),
          h('a', { class: 'brand', href: '#/home' }, h('span', { class: 'logo', 'aria-hidden': 'true' }, '동'), h('span', { class: 'bt' }, h('strong', {}, '동구 대형폐기물 관리시스템'), h('small', {}, `접수·배차·수거 업무 (PC)${boot.version ? ' · v' + boot.version : ''}`))),
          h('span', { class: 'grow' }),
          h('span', { class: 'tb-date' }, h('span', { class: 'sr-only' }, '업무 기준일 '), dayTxt),
          prefsControls(),
          h('a', { href: '#/help', class: 'tb-help' + (cur === 'help' ? ' on' : ''), title: '오늘 할 일 순서와 예외 처리' }, '도움말'),
          h('a', { href: '#/me', class: 'who' + (active === 'me' ? ' on' : ''), title: '내 계정' }, h('b', {}, boot.me.display_name), h('small', {}, boot.me.role_label)),
          h('a', { href: '#', class: 'tb-logout', onclick: logout }, '로그아웃')),
        h('nav', { class: 'side', id: 'side-nav', 'aria-label': '업무 메뉴' },
          h('button', { type: 'button', class: 'rail-btn', onclick: railBtn, title: '메뉴 접기·펴기' }, h('span', { 'aria-hidden': 'true' }, '⇤'), h('span', { class: 'rail-t' }, ' 메뉴 접기')),
          ...GROUPS.map(([gt, ks]) => {
            const items = nav.filter(([k]) => ks.includes(k));
            return items.length ? h('div', { class: 'navgrp' + (group && group[0] === gt ? ' cur' : '') }, h('span', { class: 'navgrp-t', 'aria-hidden': 'true' }, gt),
              ...items.map(([k, t]) => h('a', { href: '#/' + k, 'data-k': k, class: active === k ? 'on' : '', 'aria-current': active === k ? 'page' : null, title: t, onclick: () => { render.navOpen = false; } },
                h('span', { class: 'ic', 'aria-hidden': 'true' }, ICON[k] || '·'), h('span', { class: 'nt' }, t)))) : null;
          })),
        h('main', {}, h('nav', { class: 'crumb', 'aria-label': '현재 위치' }, h('span', {}, '홈'), ...crumbs.map((c, i) => h('span', { 'aria-current': i === crumbs.length - 1 ? 'page' : null }, c))), content));
    }
  } catch (e) {
    // 화면 하나가 실패해도 메뉴는 남기고 그 자리에 안내와 다시 시도를 보여 준다
    const shell = $app.querySelector('.shell');
    const box = h('div', { class: 'notice bad', role: 'alert' }, `화면을 불러오지 못했습니다: ${e.message} `, btn('다시 시도', () => render(true), 'small'));
    if (shell && boot) { const m = shell.querySelector('main'); if (m && my === renderSeq) { m.replaceChildren(box); return; } }
    view = h('div', { class: 'pad' }, box);
  }
  if (my !== renderSeq) return;
  const prevHash = render.lastHash; render.lastHash = location.hash;
  $app.replaceChildren(view);
  if (boot) { paintBadges(); refreshBadges(); }
  if (render.focus) { const f = render.focus; render.focus = null; setTimeout(() => { const el = document.getElementById(f); if (el) el.scrollIntoView({ block: 'start' }); }, 300); }
  const h1 = $app.querySelector('main h1, .login h1, .login strong');
  if (h1) document.title = `${h1.textContent} · 동구 대형폐기물`;
  if (prevHash !== location.hash && h1 && !document.querySelector('main [autofocus]')) { h1.tabIndex = -1; if (!$app.querySelector('main input:focus')) h1.focus({ preventScroll: true }); }
}
addEventListener('hashchange', () => render(true));
// 좁은 화면에서 연 메뉴는 Esc로 닫는다
addEventListener('keydown', (e) => { if (e.key === 'Escape' && render.navOpen && !dlgStack.length) { render.navOpen = false; const sh = $app.querySelector('.shell'); if (sh) sh.classList.remove('nav-open'); } });
// 단축키 (입력 칸 밖에서): / 찾기, N 새 접수 / 입력 중: Ctrl+Enter 저장
addEventListener('keydown', (e) => {
  if (!boot || dlgStack.length) return;  // 대화 상자가 열려 있으면 단축키를 쓰지 않는다
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '');
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { const b = document.querySelector('[data-save]'); if (b && !b.disabled) { e.preventDefault(); b.click(); } return; }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === '/') { const q = document.querySelector('main input[type=search]'); if (q) { e.preventDefault(); q.focus(); } }
  else if (e.key === 'n' || e.key === 'N' || e.key === 'ㅜ') { if (['ADMIN', 'INTAKE'].includes(boot.me.role)) { e.preventDefault(); go('#/request/new'); } }
});
setRender(render);
render();
