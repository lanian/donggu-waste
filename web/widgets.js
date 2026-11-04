// 여러 화면이 함께 쓰는 부품: 품목 고르기(현장 추가·접수 등록)·앱 설치 안내.
import * as NAV from './nav.js';
import * as P from './proto.js';
import * as S from './store.js';
import { searchFees } from './fees.js';
import { won } from './fees.js';
import { addEvent, btn, camera, commit, go, h, isIOS, launchApp, lineFromRow, render, sheet, st, toast, ui } from './core.js';

// [길안내]: 설정에서 고른 지도 앱(기본 카카오맵)을 목적지 좌표로 연다. 넘기는 것은 좌표와 "N번 수거지"(반입지는 "반입지")뿐 —
// 이름·연락처·주소·접수번호는 넘기지 않는다(nav.js). 앱이 없으면(1.5초 안에 다른 앱으로 넘어가지 않으면) 웹 지도 또는 기본 지도 앱으로.
const NO_LOC = '위치 없음 — 위치 지정 필요';
/** stop: 기록을 남길 접수(반입지는 null — 접수 없는 팀 기록은 PC에서 충돌이 되므로 남기지 않음), loc: {lat, lon}, label: "N번 수거지"|"반입지" */
async function openNav(stop, loc, label) {
  if (!NAV.hasCoord(loc)) { toast(NO_LOC, 'bad'); return; }
  const u = NAV.navUrls(st.navApp || NAV.DEFAULT_NAV_APP, { lat: loc.lat, lon: loc.lon, label }, { ios: isIOS(), appname: location.origin });
  if (stop) await addEvent(stop, 'NAV_INTENT', { app: u.app });
  launchApp(u);
}
// 길안내 단추: 위치(좌표)가 없어도 꺼 두지 않는다(눌러도 반응 없는 것처럼 보임). 누르면 이유와 [지도에서 위치 정하기]를 보여 준다.
// 지도 앱에는 좌표와 "N번 수거지"만 넘기므로, 좌표 없이 주소를 지도 앱으로 보내지는 않는다.
const navBtn = (text, stop, loc, label, cls = '') => (NAV.hasCoord(loc)
  ? btn(text, () => openNav(stop, loc, label), 'nav-btn ' + cls, { 'aria-label': `${text} (${NAV.NAV_APPS[st.navApp] || NAV.NAV_APPS.kakao})` })
  : btn(text, () => noLocNav(stop), 'nav-btn no-loc ' + cls, { 'aria-label': `${text}: ${NO_LOC}` }));
async function noLocNav(stop) {
  const what = stop ? `${stop.seq}번 수거지` : '반입지';
  const r = await sheet({ title: `${what} 위치가 없습니다`,
    text: (stop ? 'PC 목록에 이 지점의 좌표가 없어 길안내를 열 수 없습니다. 지도에서 배출 위치를 정하면 바로 길안내할 수 있고, 정한 위치는 결과 보내기 때 PC로 갑니다.'
      : 'PC 목록에 반입지 좌표가 없습니다. 지도에서 반입지 위치를 한 번 정해 두면 이 휴대폰에서 계속 씁니다.') + (st.mapEnabled ? '' : ' (지도가 꺼져 있습니다 — 설정에서 지도를 켜 주세요.)'),
    choices: st.mapEnabled ? [{ label: '지도에서 위치 정하기', value: true, cls: 'primary' }] : [], cancel: '닫기' });
  if (r) go(stop ? `#/pick/${stop.request_id}` : '#/pick/unload');
}
const unloadLoc = (u) => (u && u.lat != null && u.lon != null ? { lat: u.lat, lon: u.lon } : st.unloadSite);
// 목록에 없는 품목은 "확인 대기"로만 담는다(0원 아님). 단가는 수수료표 행에서 가져오고 화면에서 바꾸지 않는다.
// '품목 담기' 화면(접수의 품목 담기, 지점의 현장 품목 추가): 위에 검색칸(고정), 결과마다 [+ 담기] / [− N +], 아래에 담은 요약과 [완료](고정).
// 여러 품목을 연달아 담기 쉽게: 담기 단추를 눌러도 검색칸 포커스(키보드)를 유지하고 검색어를 선택해 두어, 바로 다음 품목을 치면 바뀐다.
function pickScreen({ items, state: P, table, locked = new Set(), onChange = () => {}, onDone, doneLabel = () => '완료', title = '품목 담기', back = null, intro = null }) {
  const keep = { onmousedown: (e) => e.preventDefault() };  // 단추를 눌러도 검색칸 포커스(키보드) 유지
  const q = h('input', { type: 'search', class: 'search', value: P.q, placeholder: '이름으로 찾기 (예: 의자, ㅈㄹ)', autocomplete: 'off', enterkeyhint: 'search', 'aria-label': '품목 찾기' });
  const list = h('div', { class: 'pick-list' });
  const bar = h('div', { class: 'pick-sum', 'aria-live': 'polite' });
  const lineOf = (rid) => items.find((x) => x.fee_rate_id === rid && !locked.has(x.line_id));
  const qtyOf = (rid) => items.filter((x) => x.fee_rate_id === rid).reduce((a, x) => a + x.qty, 0);
  const setLine = (l, n) => {
    if (n <= 0) items.splice(items.indexOf(l), 1);
    else { l.qty = n; l.amount = l.price_status === 'CONFIRMED' ? l.unit_price * n : null; }
  };
  const bump = (r, d) => {
    const l = lineOf(r.id);
    if (!l && d > 0) { items.push(lineFromRow(r, 1, table)); st.feeUsage = { ...(st.feeUsage || {}), [r.id]: ((st.feeUsage || {})[r.id] || 0) + 1 }; }
    else if (l) setLine(l, l.qty + d);
    if (d > 0 && navigator.vibrate) navigator.vibrate(12);
    onChange(); draw(); redrawSheet();
    if (document.activeElement === q) q.select();  // 다음 품목을 치면 검색어가 바로 바뀌게
  };
  let redrawSheet = () => {};
  // 분류 → 품목 이름 단추 → (규격이 여러 개면) 규격 고르기 창. 글자를 치지 않고 눌러서 담는다.
  const CATS = [...new Set(table.rows.map((r) => r.category))];
  const catLabel = (c) => ({ 가전제품류: '가전', 가구류: '가구', 생활용품: '생활' })[c] || c;
  const openSpecs = async (cat, name) => {
    const rows = table.rows.filter((r) => r.category === cat && r.name === name);
    if (rows.length === 1) { bump(rows[0], 1); toast(`${name} ${rows[0].spec} — ${qtyOf(rows[0].id)}개`); return; }
    const box = h('div', { class: 'pick-list sheet-list' });
    redrawSheet = () => box.replaceChildren(...rows.map((r) => row(r, true)));
    redrawSheet();
    await sheet({ title: name, text: '규격을 골라 담으세요. 다 담았으면 [완료] 또는 바깥을 누르세요.', body: box, choices: [], cancel: '완료', cancelCls: 'primary big' });
    redrawSheet = () => {};
    draw();
  };
  const nameChips = (cat) => {
    const names = [...new Set(table.rows.filter((r) => r.category === cat).map((r) => r.name))];
    return h('div', { class: 'name-chips' }, ...names.map((nm) => {
      const ids = table.rows.filter((r) => r.category === cat && r.name === nm).map((r) => r.id);
      const n = ids.reduce((a2, id) => a2 + qtyOf(id), 0);
      const multi = ids.length > 1;
      return btn([h('span', {}, nm), n ? h('b', { class: 'nc-n' }, n) : h('span', { class: 'nc-i', 'aria-hidden': 'true' }, multi ? '›' : '+')],
        () => openSpecs(cat, nm), 'name-chip' + (n ? ' on' : ''), { 'aria-label': `${nm}${n ? ` (${n}개 담김)` : ''}${multi ? ' 규격 고르기' : ' 1개 담기'}` });
    }));
  };
  const ctl = (n, onMinus, onPlus, name) => (n
    ? h('span', { class: 'pick-ctl' }, btn('−', onMinus, 'step', { ...keep, 'aria-label': `${name} 빼기` }), h('b', { class: 'val' }, n), btn('+', onPlus, 'step', { ...keep, 'aria-label': `${name} 더하기` }))
    : btn('+ 담기', onPlus, 'opt pick-add', { ...keep, 'aria-label': `${name} 담기` }));
  // 즐겨찾기(☆): [자주 쓴] 맨 앞에 늘 나온다(이 휴대폰 안에서만)
  const toggleFav = async (rid) => {
    const fv = new Set(st.feeFavs || []);
    fv.has(rid) ? fv.delete(rid) : fv.add(rid);
    st.feeFavs = [...fv]; await commit(); draw();
  };
  const row = (r, specOnly = false) => {
    const n = qtyOf(r.id), name = `${r.name} ${r.spec}`.trim();
    const fav = (st.feeFavs || []).includes(r.id);
    return h('div', { class: 'pick-row' + (n ? ' on' : '') },
      specOnly ? null : btn(fav ? '★' : '☆', () => toggleFav(r.id), 'star pick-star' + (fav ? ' on' : ''), { ...keep, 'aria-label': fav ? `${name} 즐겨찾기 해제` : `${name} 즐겨찾기` }),
      h('div', { class: 'pick-info' }, h('strong', {}, specOnly && r.spec ? r.spec : name), h('span', { class: 'p' }, `${won(r.price)} / ${r.unit_label}`),
        r.status === 'REVIEW' ? h('span', { class: 'p warnp' }, '규격 확인 필요 — 금액 확인 대기로 담김') : null),
      ctl(n, () => bump(r, -1), () => bump(r, 1), name));
  };
  const picked = () => items.map((l) => h('div', { class: 'pick-row on' },
    h('div', { class: 'pick-info' }, h('strong', {}, `${l.name} ${l.spec || ''}`.trim()),
      h('span', { class: 'p' }, l.price_status === 'CONFIRMED' ? `${won(l.unit_price)} / ${l.unit_label}` : '금액 확인 대기')),
    locked.has(l.line_id) ? h('b', { class: 'val' }, l.qty) : ctl(l.qty, () => { setLine(l, l.qty - 1); onChange(); draw(); }, () => { setLine(l, l.qty + 1); onChange(); draw(); }, l.name)));
  const draw = () => {
    const out = [];
    drawTabs();
    if (!P.q) {
      const cat = P.cat || 'FREQ';
      if (cat === 'PICKED') {
        out.push(...(items.length ? picked() : [h('p', { class: 'small' }, '아직 담은 품목이 없습니다.')]));
      } else if (cat === 'FREQ') {
        const favs = st.feeFavs || [], use = st.feeUsage || {};
        const ids = [...favs, ...Object.keys(use).sort((a, b) => use[b] - use[a]).filter((x) => !favs.includes(x))].slice(0, 15);
        const quick = ids.map((x) => table.rows.find((r) => r.id === x)).filter(Boolean);
        out.push(...(quick.length ? quick.map(row) : [h('p', { class: 'small' }, '자주 쓴 품목이 아직 없습니다. 위 분류(가전·가구·생활·기타)를 눌러 고르거나 이름을 검색하세요.')]));
      } else {
        out.push(nameChips(cat));
      }
    } else {
      const rows = searchFees(table, P.q).slice(0, 40);
      out.push(...(rows.length ? rows.map(row) : [h('p', { class: 'small' }, '찾는 품목이 없습니다. 아래 [목록에 없는 품목]으로 담으세요.')]));
    }
    list.replaceChildren(...out);
    const n = items.reduce((a, x) => a + x.qty, 0);
    const pend = items.filter((x) => x.price_status !== 'CONFIRMED').length;
    const sum = items.filter((x) => x.price_status === 'CONFIRMED').reduce((a, x) => a + x.unit_price * x.qty, 0);
    bar.textContent = items.length ? `${items.length}종 · ${n}개 · ${won(sum)}${pend ? ` + 확인 대기 ${pend}` : ''}` : '담은 품목 없음';
    if (typeof doneBtn !== 'undefined') doneBtn.textContent = doneLabel(items);
  };
  const tabs = h('div', { class: 'pick-tabs', role: 'group', 'aria-label': '품목 분류' });
  const drawTabs = () => {
    const cur = P.q ? null : (P.cat || 'FREQ');
    const n = items.length;
    tabs.replaceChildren(...[['FREQ', '자주 쓴'], ...CATS.map((c) => [c, catLabel(c)]), ['PICKED', n ? `담음 ${n}` : '담음']].map(([k, t]) =>
      btn(t, () => { P.cat = k; P.q = ''; q.value = ''; draw(); scrollTo(0, 0); }, 'tab' + (cur === k ? ' on' : '') + (k === 'PICKED' && n ? ' has' : ''), { ...keep, 'aria-pressed': cur === k ? 'true' : 'false' })));
  };
  q.addEventListener('input', () => { P.q = q.value; draw(); list.scrollTop = 0; });
  q.addEventListener('focus', () => q.select());
  const clear = btn('지우기', () => { P.q = ''; q.value = ''; draw(); q.focus(); }, 'ghost q-clear', { ...keep, 'aria-label': '검색어 지우기' });
  // 목록에 없는 품목: 금액 확인 대기로 담는다(0원 아님)
  const freeName = h('input', { type: 'text', class: 'search', placeholder: '품목 이름 (예: 안마기)', 'aria-label': '목록에 없는 품목 이름' });
  const unitSel = h('select', { 'aria-label': '단위' }, ...[['EA', '개'], ['PIECE', '쪽'], ['SET', '세트'], ['L20', '20ℓ당']].map(([v, t]) => h('option', { value: v }, t)));
  const free = h('details', { class: 'pick-free' }, h('summary', {}, '목록에 없는 품목'),
    h('p', { class: 'small' }, '단가를 정하지 않고 "확인 대기"로 담습니다. 0원으로 계산하지 않습니다.'),
    h('div', { class: 'qrow' }, freeName, unitSel),
    btn('확인 대기로 담기', () => {
      const name = freeName.value.trim();
      if (!name) return toast('품목 이름을 적어 주세요', 'bad');
      items.push({ line_id: crypto.randomUUID(), fee_rate_id: null, name, spec: '', unit: unitSel.value, unit_label: unitSel.selectedOptions[0].textContent, qty: 1, unit_price: null, amount: null, price_status: 'PENDING' });
      freeName.value = ''; onChange(); P.q = ''; q.value = ''; draw(); toast(`${name} — 확인 대기로 담음`);
    }, 'big'));
  const done = () => { P.q = ''; onDone(); };
  const doneBtn = btn(doneLabel(items), done, 'primary big');
  draw();
  return h('main', { class: 'pick-screen' },
    h('div', { class: 'pick-head' }, h('header', { class: 'bar pick-top' }, back ? btn(back.label, back.fn, 'ghost') : btn('← 완료', done, 'ghost'), h('h1', { class: 'bar-title' }, title)),
      h('div', { class: 'qrow' }, q, clear), tabs),
    intro, free, list,
    h('nav', { class: 'footbar pick-foot', 'aria-label': '담은 품목' }, bar, doneBtn),
    h('p', { class: 'small pick-table' }, `수수료표: ${table.label}`));
}

// ------------------------------------------------------------ 현장 품목 추가
// 이 웹 앱은 브라우저 탭에서도 그대로 동작하고 같은 자료를 쓴다(Android). 그래서 설치하지 않아도 '설치된 앱처럼' 보일 수 있어
// 지금 어디서 실행 중인지(브라우저 탭 / 설치한 앱)를 화면에 분명히 보여 주고, 설치할 수 있으면 설치 단추를 준다.
const runningAsApp = () => matchMedia('(display-mode: standalone)').matches || matchMedia('(display-mode: fullscreen)').matches
  || matchMedia('(display-mode: minimal-ui)').matches || navigator.standalone === true;
let installEvt = null;  // 브라우저가 설치를 제안할 수 있을 때만 생김(Android Chrome·Samsung 인터넷). 이미 설치돼 있으면 생기지 않음
addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault(); installEvt = e;
  if (S.isUnlocked() && ['#/settings', '#/home', ''].includes(location.hash)) render();
});
addEventListener('appinstalled', () => { installEvt = null; toast('설치했습니다. 홈 화면의 "동구 수거"로 여세요'); if (S.isUnlocked() && location.hash === '#/settings') render(); });
// 같은 앱이 이 휴대폰에 설치돼 있는지(Android Chrome만 답함, 모르면 null)
async function appInstalled() {
  try { if (!navigator.getInstalledRelatedApps) return null; return (await navigator.getInstalledRelatedApps()).length > 0; } catch { return null; }
}
async function doInstall() {
  if (!installEvt) return;
  const e = installEvt; installEvt = null;
  try { await e.prompt(); const c = await e.userChoice; if (c && c.outcome !== 'accepted') toast('설치를 취소했습니다'); } catch {}
  render();
}
async function installCard() {
  if (runningAsApp()) return h('section', { class: 'card install-card' }, h('h2', {}, '앱 설치'),
    h('p', {}, '✓ 설치한 앱으로 실행 중입니다.'),
    h('p', { class: 'small' }, isIOS() ? '아이폰: 홈 화면 앱과 Safari는 자료를 따로 보관합니다. 업무는 이 앱에서만 하세요.' : '브라우저에서 같은 주소를 열어도 같은 자료가 보입니다. 업무는 이 앱으로 하세요.'));
  const inst = installEvt ? false : await appInstalled();
  return h('section', { class: 'card install-card notice' }, h('h2', {}, '앱 설치 — 지금은 브라우저에서 실행 중'),
    h('p', { class: 'small' }, '브라우저 탭에서도 똑같이 동작하지만, 설치해 두면 홈 화면 아이콘으로 바로 열리고 주소창 없이 전체 화면으로 씁니다. ' + (isIOS() ? '' : '설치해도 자료는 그대로입니다(같은 자료를 씀).')),
    installEvt ? btn('홈 화면에 앱 설치', doInstall, 'primary big')
      : inst ? h('p', {}, '이 휴대폰에 이미 설치되어 있습니다. 홈 화면이나 앱 목록의 "동구 수거"로 여세요. 홈 화면 아이콘만 지웠다면 앱은 남아 있습니다(휴대폰 설정 → 애플리케이션에서 지울 수 있음).')
      : isIOS() ? h('p', {}, 'Safari 아래 공유 단추(□↑) → [홈 화면에 추가]. ', h('strong', {}, '아이폰은 Safari에서 쓰던 자료가 설치한 앱으로 옮겨 가지 않습니다'), ' — 보내지 않은 결과를 먼저 보내고, 설치한 앱에서 PIN 설정·기기 등록을 새로 하세요.')
      : h('p', {}, '브라우저 메뉴(⋮)에서 [앱 설치] 또는 [홈 화면에 추가]를 누르세요. 메뉴에 [앱 열기]만 보이면 이미 설치된 것입니다.'));
}

// ------------------------------------------------------------ 도움말: 현장 처리 순서 (한 장, 고정 글)
// 자동 시험(가상 브라우저)으로는 확인할 수 없는 것을 수거팀이 이 휴대폰에서 눌러 보고 결과를 남긴다.
// 결과에는 개인정보가 없고(기기·브라우저 정보와 된다/안 된다/메모), [결과 복사]로 담당자에게 전달한다.
const CHECK_SPOT = { lat: 35.1462, lon: 126.9233 };  // 시험용 좌표(동구 부근 공공장소), 주민 위치 아님
const CHECKS = [
  ['install', '앱 설치', '홈 화면 아이콘으로 열리고, 설정 → 실행 위치가 "설치한 앱"으로 나오는지'],
  ['bio', '지문·얼굴 잠금 해제', '설정 → 잠금에서 등록하고, 잠근 뒤 지문·얼굴로 열리는지'],
  ['juso', '주소 검색(행안부)', '승인키를 넣은 뒤 아래 [시험]으로 검색이 되는지(브라우저가 막으면 안 됨)'],
  ['nav_kakao', '길안내: 카카오맵', '[시험]으로 카카오맵이 열리고 목적지가 맞는지(앱이 없으면 웹 지도)'],
  ['nav_tmap', '길안내: T맵', '[시험]으로 T맵이 열리는지'],
  ['nav_naver', '길안내: 네이버지도', '[시험]으로 네이버지도가 열리는지'],
  ['camera', '사진(카메라 다녀오기)', '[시험]으로 사진을 찍고 돌아왔을 때 사진이 남는지(앱이 다시 열리며 사라지면 "안 됨")'],
  ['qr', '목록 QR 받기', 'PC [QR 실측]으로 몇 초 만에 읽히는지(메모에 초)'],
  ['call', '전화·문자 앱', '지점 화면 [전화]·[문자]로 전화·문자 앱이 열리고, 돌아오면 그대로인지'],
];

export {
  CHECKS, CHECK_SPOT, NO_LOC, appInstalled, doInstall, installCard, installEvt, navBtn,
  noLocNav, openNav, pickScreen, runningAsApp, unloadLoc,
};
