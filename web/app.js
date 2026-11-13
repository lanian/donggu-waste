// 동구 대형폐기물 수거 시제품 - 휴대폰 앱 (서버 없는 PWA) — 시작점: 라우터(주소 → 화면)·서비스 워커 업데이트·저장소 준비.
// 업무 데이터는 이 기기 안에서만 암호화해 보관한다. 외부로 보내는 통신 코드는 없다.
// 화면은 screens/*.js, 공통은 core.js·widgets.js. 새 화면 파일은 sw.js 의 FILES 에도 넣는다(오프라인 실행). APP_VERSION 은 sw.js VERSION 과 같게.
import * as S from './store.js';
import { $app, appLogo, applyPrefs, btn, clearLiveMap, clearPhotoUrls, fieldEditable, flushSteps, h, isSolo, mergePrefs, prefs, setRegisteredNow, setRender, setSt, st, stepFlush, stopCamera, ui, wakeRoute, wantWake } from './core.js';
import { screenAdd } from './screens/add.js';
import { screenBackup } from './screens/backupview.js';
import { screenCheck } from './screens/check.js';
import { screenCalculator } from './screens/calculator.js';
import { screenExport } from './screens/export.js';
import { screenHelp } from './screens/help.js';
import { screenHome } from './screens/home.js';
import { screenFind } from './screens/find.js';
import { screenIntake } from './screens/intake.js';
import { screenLock, screenSetup } from './screens/lock.js';
import { screenMap, screenPick } from './screens/mapview.js';
import { screenMode } from './screens/mode.js';
import { screenRegister } from './screens/register.js';
import { screenResultQr } from './screens/resultqr.js';
import { screenQrTestResult, screenReceived, screenScan } from './screens/scan.js';
import { screenSettings } from './screens/settings.js';
import { screenFix, screenPcInputHelp, screenStop } from './screens/stop.js';
import { screenUnload } from './screens/unload.js';

setRender(render);  // 화면 모듈이 core 의 render() 로 다시 그릴 수 있게
export const APP_VERSION = '0.14.14';
let renderSeq = 0;
async function render() {
  const my = ++renderSeq;
  stopCamera();
  clearPhotoUrls();
  clearLiveMap();
  let view;
  // 모아 두었다 저장할 수량이 있으면 다시 그리기 전에 먼저 저장(다시 그린 화면이 옛 수량을 보이지 않게)
  if (stepFlush.size && S.isUnlocked() && st) { try { await flushSteps(); } catch {} if (my !== renderSeq) return; }
  try {
  if (!storeReady && !(await Promise.race([storeWait.then(() => true), new Promise((r) => setTimeout(() => r(false), 2500))]))) {
    // 저장소가 아직 안 열림(다른 창이 붙잡고 있는 등): 멈춘 것처럼 두지 않고 안내하고, 열리면 저절로 계속한다
    view = waitScreen();
    storeWait.then(() => render(), () => render());
  } else if (!(await S.hasPin())) view = screenSetup();
  else if (!S.isUnlocked()) view = screenLock();
  else {
    let [, route, arg] = (location.hash || '#/home').split('/');
    if (!st.mode && route !== 'mode') route = 'mode';
    if (isSolo() && ['scan', 'register', 'export', 'received', 'resultqr'].includes(route)) route = 'home';
    if (!isSolo() && route === 'intake' && arg && !fieldEditable(arg)) route = 'home';  // PC 연결 방식: 보내기 전 휴대폰 접수만 고침, 그 밖은 정정 요청
    if (isSolo() && route === 'fix') route = 'home';
    const previous = (ui.lastRoute || '').split('/');
    const related = ['stop', 'add', 'fix', 'intake', 'map', 'pick', 'pc-input'].includes(route) && arg === ui.stopBackId;
    if (route === 'stop' && ui.lastRoute !== location.hash) {
      const sameDetail = ['add', 'fix', 'intake', 'map', 'pick', 'pc-input'].includes(previous[1]) && previous[2] === arg && ui.stopBackId === arg;
      if (ui.lastRoute === '#/find') ui.stopBackTo = '#/find';
      else if (!sameDetail) ui.stopBackTo = '#/home';
      ui.stopBackId = arg;
    } else if (route !== 'find' && !related) { ui.stopBackTo = '#/home'; ui.stopBackId = null; }
    view = await ({
      home: screenHome, calculator: screenCalculator, find: screenFind, scan: screenScan, received: screenReceived, register: screenRegister,
      'qrtest-result': screenQrTestResult, resultqr: screenResultQr, stop: () => screenStop(arg), 'pc-input': () => screenPcInputHelp(arg), add: () => screenAdd(arg), export: screenExport, settings: screenSettings, unload: screenUnload,
      check: screenCheck, help: screenHelp, mode: screenMode, intake: async () => { setRegisteredNow(!!(await S.getRaw('pc')) && !!(await S.getRaw('device'))); return screenIntake(arg); }, fix: () => screenFix(arg), backup: screenBackup, map: () => screenMap(arg), pick: () => screenPick(arg),
    }[route] || screenHome)();
  }
  } catch (e) { view = errorScreen(e); }
  if (my !== renderSeq) return; // 그 사이 다른 화면으로 이동
  wantWake(S.isUnlocked() && wakeRoute());
  const sameRoute = ui.lastRoute === location.hash, y = scrollY; // 같은 화면을 다시 그릴 때는 스크롤 유지
  $app.replaceChildren(view);
  scrollTo(0, sameRoute ? y : 0);
  if (ui.pcInputBack && location.hash === '#/stop/' + ui.pcInputBack.id) {
    const help = [...view.querySelectorAll('button')].find((button) => button.textContent === 'PC 입력 도움');
    help?.focus({ preventScroll: true }); scrollTo(0, ui.pcInputBack.y); ui.pcInputBack = null;
  } else if (!sameRoute && location.hash.startsWith('#/pc-input/')) {
    const heading = view.querySelector('h1'); if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }); }
  }
  ui.lastRoute = location.hash;
}
addEventListener('hashchange', render);

// ------------------------------------------------------------ 서비스 워커 (업데이트는 사용자가 눌러야 적용)
let storeReady = false;
const storeWait = S.whenReady().then(() => { storeReady = true; });
function waitScreen() {
  return h('main', { class: 'center' }, appLogo('app-icon'), h('h1', {}, '자료를 여는 중…'),
    h('p', {}, '잠시만 기다려 주세요. 열리면 저절로 계속합니다.'),
    h('p', { class: 'small' }, '오래 걸리면: 이 앱이 다른 창(브라우저 탭이나 홈 화면 앱)에도 열려 있으면 그 창을 닫아 주세요.'),
    btn('다시 시도', () => location.reload(), 'ghost'));
}
// 오류가 나면 '불러오는 중…'에 머물지 않고 이유와 [다시 시도]를 보여 준다
function errorScreen(e) {
  return h('main', { class: 'center' }, appLogo('app-icon'), h('h1', {}, '불러오지 못했습니다'),
    h('p', {}, '오류가 났습니다. [다시 시도]를 누르고, 계속되면 이 화면을 담당자에게 보여 주세요.'),
    h('p', { class: 'small' }, `${(e && (e.code || e.name)) || '오류'}: ${(e && e.message) || ''} · 앱 ${APP_VERSION}`),
    btn('다시 시도', () => location.reload(), 'primary big'));
}
// 다른 창에서 '모든 자료 지우기'를 하면 이 창도 처음 화면으로
try { new BroadcastChannel('dg-store').onmessage = (e) => { if (e.data === 'wiped') { setSt(null); location.hash = ''; location.reload(); } }; } catch {}
// 화면 설정(테마·고대비)은 잠금 화면 전에 적용
let booted = false;
const boot = () => { if (!booted) { booted = true; render(); } };
storeWait.then(() => S.getRaw('prefs')).then((p) => { if (p) mergePrefs(p); }).catch(() => {}).finally(() => { applyPrefs(); boot(); });
setTimeout(boot, 2500);  // 저장소가 늦게 열리면 먼저 '자료를 여는 중' 안내
