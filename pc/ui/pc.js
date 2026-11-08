// 동구 대형폐기물 PC 업무 화면 (이 PC의 127.0.0.1 서버만 사용) — 시작점: 라우터(주소 → 화면)·틀(메뉴·머리)·단축키.
// 화면은 screens/*.js, 공통 도우미는 core.js·widgets.js. 새 화면은 아래 screenImports·screenExports·NAV·GROUPS에 등록한 뒤
// 서버(pc/dgpc/server.py UI_FILES)에 파일을 더한다.
// 이전 서버의 정적 파일 허용 목록에서도 열리는 진입점을 유지한다.
// 첫 파일·연결·화면 준비를 구분한다. 자동 재시도와 업무 상태 변경은 하지 않는다.
const card = document.getElementById('startup-card');
const title = document.getElementById('startup-title');
const detail = document.getElementById('startup-detail');
const elapsed = document.getElementById('startup-elapsed');
const retry = document.getElementById('startup-retry');
const fallback = document.getElementById('startup-fallback');
const help = document.getElementById('startup-help');
const steps = [...document.querySelectorAll('.startup-steps li')];
const started = performance.now();
let trouble = false;
const phases = { session: [1, '로그인 상태를 확인하고 있습니다.'], workspace: [1, '업무 설정을 확인하고 있습니다.'], screen: [2, '필요한 업무 화면을 준비하고 있습니다.'] };
function phase(event) {
  const next = phases[event.detail?.phase];
  if (!next || trouble || !card?.isConnected) return;
  detail.textContent = next[1];
  steps.forEach((step, i) => {
    step.classList.toggle('current', i === next[0]);
    step.classList.toggle('done', i < next[0]);
    if (i === next[0]) step.setAttribute('aria-current', 'step'); else step.removeAttribute('aria-current');
  });
}
function finish() {
  clearTimeout(deadline); clearInterval(ticker);
  removeEventListener('dg:startup', phase);
}
function problem(delayed) {
  if (!card?.isConnected) { finish(); return; }
  trouble = true;
  card.classList.add(delayed ? 'is-delayed' : 'is-error');
  card.setAttribute('role', 'alert'); card.setAttribute('aria-busy', 'false');
  title.textContent = delayed ? '업무 화면 준비가 지연되고 있습니다.' : 'PC 업무 화면을 불러오지 못했습니다.';
  detail.textContent = '실행 앱에서 서버 상태를 확인한 뒤 화면을 다시 불러와 주세요.';
  help.textContent = '다시 불러오기는 사용자가 버튼을 눌렀을 때만 진행합니다.';
  retry.hidden = false; fallback.hidden = true;
  if (!delayed) finish();
}
retry?.addEventListener('click', () => location.reload());
addEventListener('dg:startup', phase);
const ticker = setInterval(() => {
  if (!card?.isConnected) { finish(); return; }
  elapsed.textContent = `경과 ${Math.floor((performance.now() - started) / 1000)}초`;
}, 1000);
const deadline = setTimeout(() => problem(true), 15000);

async function startPc() {
  const core = await import('./core.js');
  const { $app, api, applyPrefs, btn, clearMaps, dlgStack, go, h, pendingActions, post, prefsControls, setBoot, setRender, stopSessionTimer, touchSession } = core;

  // 방문한 화면만 읽는다. 통계·설정 등의 연결을 기다리느라 로그인까지 막히지 않게 한다.
  const screenImports = {
    access: () => import('./screens/access.js'), close: () => import('./screens/close.js'), dispatch: () => import('./screens/dispatch.js'), dongs: () => import('./screens/dongs.js'),
    fees: () => import('./screens/fees.js'), help: () => import('./screens/help.js'), home: () => import('./screens/home.js'),
    login: () => import('./screens/login.js'), pilot: () => import('./screens/pilot.js'), qrtest: () => import('./screens/qrtest.js'),
    refunds: () => import('./screens/refunds.js'), request: () => import('./screens/request.js'), requests: () => import('./screens/requests.js'),
    results: () => import('./screens/results.js'), revenue: () => import('./screens/revenue.js'), send: () => import('./screens/send.js'),
    reports: () => import('./screens/reports.js'), labels: () => import('./screens/numbered_labels.js'),
    settings: () => import('./screens/settings.js'), stats: () => import('./screens/stats.js'), users: () => import('./screens/users.js'),
  };
  const screenExports = { dongs: 'screenDongs', reports: 'screenReports', labels: 'screenNumberedLabels', access: 'screenAccess', close: 'screenClose', dispatch: 'screenDispatch', fees: 'screenFees', help: 'screenHelp',
    home: 'screenHome', pilot: 'screenPilot', qrtest: 'screenQrTest', refunds: 'screenRefunds', request: 'screenRequest', requests: 'screenRequests',
    results: 'screenResults', revenue: 'screenRevenue', send: 'screenSend', settings: 'screenSettings', stats: 'screenStats', users: 'screenUsers', me: 'screenMe' };
  const screenModules = new Map(), screenLoads = new Map();
  async function loadScreen(route) {
    const key = route === 'me' ? 'users' : route;
    if (screenModules.has(key)) return screenModules.get(key);
    if (!screenLoads.has(key)) {
      let timer;
      const fetching = screenImports[key]().then((module) => { screenModules.set(key, module); return module; });
      const waiting = new Promise((resolve, reject) => {
        timer = setTimeout(() => {
          const error = new Error('화면 파일을 읽는 시간이 지났습니다. 다시 시도해 주세요.');
          error.code = 'SCREEN_LOAD_FAILED'; reject(error);
        }, 10000);
        fetching.then(resolve, (cause) => {
          const error = new Error('선택한 화면 파일을 읽지 못했습니다. 다시 시도해 주세요.', { cause });
          error.code = 'SCREEN_LOAD_FAILED'; reject(error);
        });
      });
      screenLoads.set(key, waiting.finally(() => { clearTimeout(timer); screenLoads.delete(key); }));
    }
    return screenLoads.get(key);
  }
  const startupPhase = (phase) => dispatchEvent(new CustomEvent('dg:startup', { detail: { phase } }));

  // ------------------------------------------------------------ 틀·라우팅
  let renderSeq = 0;
  async function render(fromNav) {
    // 작업 끝의 다시 그리기(fromNav 아님)인데 진행 중인 작업이 모두 다른 화면에서 시작했으면, 지금 화면을 덮지 않는다
    if (fromNav !== true && pendingActions.size && !pendingActions.has(location.hash)) return;
    const my = ++renderSeq;
    clearMaps();
    let view;
    try {
      if (!core.boot) {
        // 재인증을 취소해 로그인으로 돌아갈 때 주민 양식과 메모리 초안을 즉시 비운다.
        // 같은 계정의 [로그인하고 계속] 경로는 boot를 유지하므로 작성 내용을 보존한다.
        screenModules.get('dongs')?.resetDongs();
        startupPhase('session');
        const st = await api('state', { timeoutMs: 10000 });
        if (my !== renderSeq) return;
        if (!st.logged_in) {
          startupPhase('screen');
          const login = await loadScreen('login');
          if (my !== renderSeq) return;
          view = await login.screenLogin(st);
        } else {
          startupPhase('workspace');
          const initial = await api('bootstrap', { timeoutMs: 10000 });
          if (my !== renderSeq) return;
          setBoot(initial); touchSession();
        }
      }
      if (core.boot) {
        // 주소 뒤 조건(#/requests?quick=OVERDUE 등): 화면 상태에 한 번 넣고 주소에서는 지운다(다시 그려도 사용자가 바꾼 조건을 덮지 않게)
        const [pathPart, query] = (location.hash || '#/home').split('?');
        const [, route0, arg] = pathPart.split('/');
        const route = route0;
        const role = core.boot.me.role;
        applyPrefs(core.boot.me.prefs);
        // 역할별 메뉴 (서버도 같은 기준으로 막는다)
        const NAV = [['home', '오늘 현황', 'ADMIN INTAKE FIELD LEAD'], ['dongs', '동별 관리', 'ADMIN INTAKE LEAD DONG'], ['requests', '접수', 'ADMIN INTAKE FIELD LEAD'], ['labels', '배출표 관리', 'ADMIN INTAKE LEAD'], ['dispatch', '배정·노선', 'ADMIN INTAKE'], ['send', '목록 전송', 'ADMIN INTAKE'],
          ['results', '결과 가져오기', 'ADMIN INTAKE FIELD'], ['close', '일일 마감', 'ADMIN INTAKE FIELD LEAD'], ['revenue', '세입·대사', 'ADMIN INTAKE LEAD'], ['refunds', '환불', 'ADMIN INTAKE LEAD'],
          ['qrtest', 'QR 실측', 'ADMIN INTAKE FIELD'], ['stats', '통계·보고서', 'ADMIN INTAKE LEAD'], ['reports', '작업일지·월보', 'ADMIN INTAKE LEAD'], ['pilot', '시범 운영 비교', 'ADMIN INTAKE LEAD'], ['fees', '수수료표', 'ADMIN INTAKE FIELD LEAD'], ['settings', '설정', 'ADMIN'],
          ['users', role === 'ADMIN' ? '사용자·이력' : '변경 이력', 'ADMIN LEAD'], ['access', '접속기록', 'ADMIN LEAD']];
        const nav = NAV.filter(([, , r]) => r.split(' ').includes(role));
        const defaultRoute = role === 'DONG' ? 'dongs' : 'home';
        let cur = route || defaultRoute;
        if (core.boot.me.must_change) cur = 'me';
        if (role === 'DONG' && !['dongs', 'me'].includes(cur)) cur = defaultRoute;
        if (!['request', 'me', 'help'].includes(cur) && !nav.some(([k]) => k === cur)) cur = defaultRoute;
        startupPhase('screen');
        const module = await loadScreen(cur);
        if (my !== renderSeq || !core.boot) return;
        const screen = module[screenExports[cur]];
        if (query) {
          const q = new URLSearchParams(query), r0 = pathPart.split('/')[1];
          if (r0 === cur) {
            if (cur === 'requests' && q.get('quick')) screen.f = { ...(screen.f || {}), q: '', status: '', date_from: '', date_to: '', dong: '', crew: '', pay: '', channel: '', quick: q.get('quick'), page: 1 };
            if (/^\d{4}-\d\d-\d\d$/.test(q.get('date') || '')) {
              const date = q.get('date');
              if (cur === 'dispatch') screen.s = { crews: new Set(), seen: new Set(), plan: null, ...(screen.s || {}), date };
              if (cur === 'send' || cur === 'close') screen.s = { ...(screen.s || {}), date };
            }
            if (cur === 'refunds' && q.has('status')) screen.s = { status: q.get('status') };
            if (q.get('focus')) render.focus = q.get('focus');
          }
          // 첫 비밀번호 변경으로 다른 화면을 보여 줄 때는 원래 날짜·조건을 그 화면 진입까지 남긴다.
          if (r0 === cur || !core.boot.me.must_change) history.replaceState(null, '', pathPart);
        }
        const content = await screen(...(['request', 'settings'].includes(cur) ? [arg] : []));
        const active = cur === 'request' ? 'requests' : cur;
        // 메뉴 묶음 (행정 업무 시스템처럼 업무 단위로 묶는다). 묶음 이름은 제목(heading)이 아니라 글자로만 둔다.
        const GROUPS = [['업무 현황', ['home', 'dongs']], ['접수 업무', ['requests', 'labels']], ['배차·수거', ['dispatch', 'send', 'results']], ['정산·마감', ['close', 'revenue', 'refunds', 'stats', 'reports', 'pilot']], ['관리', ['fees', 'qrtest', 'settings', 'users', 'access']]];
        const ICON = {
          home: 'M3 10.5 12 3l9 7.5M5 9v11h5v-6h4v6h5V9',
          dongs: 'M3 21h18M5 21V7l7-4v18M12 9h7v12M8 8h1M8 12h1M8 16h1M15 12h1M15 16h1',
          requests: 'M14 3H5v18h14V8l-5-5v5h5M8 12h8M8 16h5',
          labels: 'M3 6h18v12H3zM8 6v12M12 10h5M12 14h5',
          reports: 'M5 3h14v18H5zM8 7h8M8 11h8M8 15h5',
          dispatch: 'M3 6h11v11H3zM14 10h4l3 4v3h-7M7 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4M18 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4',
          send: 'm3 11 18-8-7 18-3-7-8-3 18-8M11 14l5-6',
          results: 'M21 11v1a9 9 0 1 1-5.3-8.2M8 11l4 4 9-10',
          close: 'M4 8h16v13H4zM3 3h18v5H3zM9 12h6',
          revenue: 'M5 3h14v18l-3-2-4 2-4-2-3 2V3M8 7h8M8 11h8M8 15h4',
          refunds: 'M4 10h10a6 6 0 0 1 0 12M4 10l5-5M4 10l5 5M16 3h4v10',
          stats: 'M4 3v18h17M8 16v-5M13 16V7M18 16v-9',
          pilot: 'M5 21V3h13l-3 5 3 5H5',
          fees: 'M3 3h8l10 10-8 8L3 11V3M7 7h.01',
          qrtest: 'M3 3h6v6H3zM15 3h6v6h-6zM3 15h6v6H3zM15 15h2v2h-2zM20 14v3M14 20h3M20 20h1',
          settings: 'M4 6h4M12 6h8M4 12h10M18 12h2M4 18h3M11 18h9M8 3v6M14 9v6M7 15v6',
          users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8M17 4a4 4 0 0 1 0 8M22 21v-2a4 4 0 0 0-3-3.9',
          access: 'm12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3M9 12l2 2 4-4',
          menu: 'M4 6h16M4 12h16M4 18h16',
          collapse: 'M4 4v16M20 12H9m5-5-5 5 5 5',
        };
        const navIcon = (key) => {
          const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
          for (const [name, value] of Object.entries({ viewBox: '0 0 24 24', width: '18', height: '18', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' })) svg.setAttribute(name, value);
          const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          path.setAttribute('d', ICON[key] || ICON.home);
          svg.append(path);
          return svg;
        };
        const navLabel = Object.fromEntries(NAV.map(([k, t]) => [k, t]));
        const group = GROUPS.find(([, ks]) => ks.includes(active));
        const pageName = cur === 'request' ? (arg === 'new' ? '전화 접수 등록' : '접수 상세') : cur === 'me' ? '내 계정' : cur === 'help' ? '도움말' : navLabel[active];
        const subName = cur === 'settings' ? (module.SETTING_TABS.find(([k]) => k === arg) || module.SETTING_TABS[0])[1] : null;
        const crumbs = [group ? group[0] : '개인', ...(cur === 'request' ? [navLabel.requests] : []), pageName, ...(subName ? [subName] : [])];
        const d = new Date(core.boot.today + 'T00:00:00');
        const dayTxt = isNaN(d) ? core.boot.today : `${core.boot.today.replace(/-/g, '.')} (${'일월화수목금토'[d.getDay()]})`;
        const logout = async (e) => { e.preventDefault(); await post('logout').catch(() => {}); screenModules.get('dongs')?.resetDongs(); setBoot(null); screenModules.get('request')?.resetIntake(); stopSessionTimer(); document.querySelectorAll('.sess-warn').forEach((x) => x.remove()); render(); };
        const navOpen = render.navOpen || false;
        const toggleNav = () => { render.navOpen = !render.navOpen; const sh = $app.querySelector('.shell'); sh.classList.toggle('nav-open', render.navOpen); sh.querySelector('.menu-btn').setAttribute('aria-expanded', render.navOpen ? 'true' : 'false'); };
        const railBtn = () => { render.rail = !render.rail; try { localStorage.setItem('dg-rail', render.rail ? '1' : ''); } catch {} $app.querySelector('.shell').classList.toggle('rail', render.rail); };
        if (render.rail === undefined) { try { render.rail = localStorage.getItem('dg-rail') === '1'; } catch { render.rail = false; } }
        view = h('div', { class: 'shell' + (navOpen ? ' nav-open' : '') + (render.rail ? ' rail' : '') },
          h('header', { class: 'topbar' },
            h('button', { type: 'button', class: 'menu-btn', 'aria-label': '메뉴 열기·닫기', 'aria-expanded': navOpen ? 'true' : 'false', 'aria-controls': 'side-nav', onclick: toggleNav }, navIcon('menu')),
            h('a', { class: 'brand', href: '#/' + defaultRoute }, h('span', { class: 'logo', 'aria-hidden': 'true' }, '동'), h('span', { class: 'bt' }, h('strong', {}, '동구 대형폐기물 관리시스템'), h('small', {}, `접수·배차·수거 업무 (PC)${core.boot.version ? ' · v' + core.boot.version : ''}`))),
            h('span', { class: 'grow' }),
            h('span', { class: 'tb-date' }, h('span', { class: 'sr-only' }, '업무 기준일 '), dayTxt),
            prefsControls(),
            role !== 'DONG' ? h('a', { href: '#/help', class: 'tb-help' + (cur === 'help' ? ' on' : ''), title: '오늘 할 일 순서와 예외 처리' }, '도움말') : null,
            h('a', { href: '#/me', class: 'who' + (active === 'me' ? ' on' : ''), title: '내 계정' }, h('span', { class: 'who-avatar', 'aria-hidden': 'true' }, String(core.boot.me.display_name || '').trim().slice(0, 1)), h('span', { class: 'who-copy' }, h('b', {}, core.boot.me.display_name), h('small', {}, core.boot.me.role_label))),
            h('a', { href: '#', class: 'tb-logout', onclick: logout }, '로그아웃')),
          h('nav', { class: 'side', id: 'side-nav', 'aria-label': '업무 메뉴' },
            h('button', { type: 'button', class: 'rail-btn', onclick: railBtn, title: '메뉴 접기·펴기' }, h('span', { 'aria-hidden': 'true' }, navIcon('collapse')), h('span', { class: 'rail-t' }, ' 메뉴 접기')),
            ...GROUPS.map(([gt, ks]) => {
              const items = nav.filter(([k]) => ks.includes(k));
              return items.length ? h('div', { class: 'navgrp' + (group && group[0] === gt ? ' cur' : '') }, h('span', { class: 'navgrp-t', 'aria-hidden': 'true' }, gt),
                ...items.map(([k, t]) => h('a', { href: '#/' + k, 'data-k': k, class: active === k ? 'on' : '', 'aria-current': active === k ? 'page' : null, title: t, onclick: () => { render.navOpen = false; } },
                  h('span', { class: 'ic', 'aria-hidden': 'true' }, navIcon(k)), h('span', { class: 'nt' }, t)))) : null;
            })),
          h('main', {}, h('nav', { class: 'crumb', 'aria-label': '현재 위치' }, h('span', {}, '홈'), ...crumbs.map((c, i) => h('span', { 'aria-current': i === crumbs.length - 1 ? 'page' : null }, c))), content));
      }
    } catch (e) {
      if (my !== renderSeq) return;
      // 첫 화면 파일 실패는 독립 시작 안내에서 전체 새로 불러오기로 복구한다.
      if (e.code === 'SCREEN_LOAD_FAILED' && card?.isConnected) { problem(false); return; }
      // 화면 하나가 실패해도 메뉴는 남기고 그 자리에 안내와 다시 시도를 보여 준다
      const shell = $app.querySelector('.shell');
      const box = h('div', { class: 'notice bad', role: 'alert' }, `화면을 불러오지 못했습니다: ${e.message} `, btn('다시 시도', () => render(true), 'small'));
      if (shell && core.boot) { const m = shell.querySelector('main'); if (m && my === renderSeq) { m.replaceChildren(box); return; } }
      view = h('div', { class: 'pad' }, box);
    }
    if (my !== renderSeq) return;
    const prevHash = render.lastHash; render.lastHash = location.hash;
    $app.replaceChildren(view);
    if (!card?.isConnected) finish();
    if (core.boot && core.boot.me.role !== 'DONG') {
      const home = screenModules.get('home');
      if (home) { home.paintBadges(); home.refreshBadges(); }
      else loadScreen('home').then((loaded) => { if (core.boot && my === renderSeq) { loaded.paintBadges(); loaded.refreshBadges(); } }).catch(() => {});
    }
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
    if (!core.boot || dlgStack.length) return;  // 대화 상자가 열려 있으면 단축키를 쓰지 않는다
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '');
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { const b = document.querySelector('[data-save]'); if (b && !b.disabled) { e.preventDefault(); b.click(); } return; }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === '/') { const q = document.querySelector('main input[type=search]'); if (q) { e.preventDefault(); q.focus(); } }
    else if (e.key === 'n' || e.key === 'N' || e.key === 'ㅜ') { if (['ADMIN', 'INTAKE'].includes(core.boot.me.role)) { e.preventDefault(); go('#/request/new'); } }
  });
  setRender(render);
  await render();
}
startPc().then(() => { if (!card?.isConnected) finish(); }).catch(() => problem(false));
