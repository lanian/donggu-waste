// 휴대폰 화면: 현장 점검
import * as BIO from '../bio.js';
import * as J from '../juso.js';
import * as L from '../load.js';
import * as NAV from '../nav.js';
import * as S from '../store.js';
import { ask, btn, camera, commit, go, h, handoff, isIOS, launchApp, render, sheet, st, tabBar, toast, wakeLock } from '../core.js';
import { CHECKS, CHECK_SPOT, runningAsApp } from '../widgets.js';
import { APP_VERSION } from '../app.js';

function deviceInfo() {
  const ua = navigator.userAgent;
  const os = /Android\s[\d.]+/.exec(ua)?.[0] || (/iPhone OS [\d_]+/.exec(ua)?.[0] || '').replace(/_/g, '.') || (isIOS() ? 'iOS' : '기타');
  const br = /SamsungBrowser\/[\d.]+/.exec(ua)?.[0] || /CriOS\/[\d.]+|Chrome\/[\d.]+/.exec(ua)?.[0] || (/Version\/[\d.]+.*Safari/.test(ua) ? 'Safari ' + /Version\/([\d.]+)/.exec(ua)[1] : '기타');
  const model = /;\s*([^;)]+)\sBuild\//.exec(ua)?.[1] || /\(Linux; Android [\d.]+; ([^;)]+)\)/.exec(ua)?.[1] || '';
  return { os, browser: br, model: model.trim(), app: APP_VERSION, standalone: runningAsApp() };
}
async function screenCheck() {
  st.deviceCheck = st.deviceCheck || {};
  const R = st.deviceCheck;
  const mark = async (k, result, note) => { R[k] = { ...(R[k] || {}), result, at: new Date().toISOString(), ...(note != null ? { note: String(note).slice(0, 120) } : {}) }; await commit(); render(); };
  // 카메라 시험 중 앱이 다시 열렸는지: 시험 전에 표시를 남기고, 돌아왔을 때 결과가 없으면 '다시 열림'
  try { if (sessionStorage.getItem('dg-cam-test') && !(R.camera && R.camera.result)) { sessionStorage.removeItem('dg-cam-test'); R.camera = { result: 'FAIL', note: '카메라 다녀온 뒤 앱이 다시 열림(사진 없음)', at: new Date().toISOString() }; await commit(); } } catch {}
  const autoBits = [];
  autoBits.push(['실행 위치', runningAsApp() ? '설치한 앱' : '브라우저 탭']);
  autoBits.push(['지문·얼굴', (await S.getBio().catch(() => null)) ? '등록됨' : (await BIO.bioAvailable()) ? '등록 가능(아직 안 함)' : '지원 안 함']);
  autoBits.push(['화면 켜짐 유지', 'wakeLock' in navigator ? '지원' : '지원 안 함']);
  try { autoBits.push(['자료 보존', navigator.storage && navigator.storage.persisted ? ((await navigator.storage.persisted()) ? '보존 요청됨' : '보존 요청 안 됨') : '확인 불가']); } catch {}
  const tests = {
    juso: async () => {
      if (!st.jusoKey) return toast('설정에서 주소 검색 승인키를 먼저 넣어 주세요', 'bad');
      try { const r = await J.searchAddress({ key: st.jusoKey, keyword: '동구청' }); await mark('juso', r.items.length ? 'OK' : 'FAIL', r.items.length ? `결과 ${r.items.length}건` : '결과 없음'); }
      catch (e) { await mark('juso', 'FAIL', `${e.code || e.name || '오류'}`); }
    },
    nav_kakao: () => launchApp(NAV.navUrls('kakao', { ...CHECK_SPOT, label: '시험 목적지' }, { ios: isIOS(), appname: location.origin })),
    nav_tmap: () => launchApp(NAV.navUrls('tmap', { ...CHECK_SPOT, label: '시험 목적지' }, { ios: isIOS(), appname: location.origin })),
    nav_naver: () => launchApp(NAV.navUrls('naver', { ...CHECK_SPOT, label: '시험 목적지' }, { ios: isIOS(), appname: location.origin })),
    camera: () => {
      const inp = h('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'hidden' });
      inp.addEventListener('change', async () => {
        try { sessionStorage.removeItem('dg-cam-test'); } catch {}
        const f = inp.files && inp.files[0];
        await mark('camera', f ? 'OK' : 'FAIL', f ? `사진 받음(${Math.round(f.size / 1024)}KB, 저장하지 않음)` : '사진 없음');
      });
      try { sessionStorage.setItem('dg-cam-test', '1'); } catch {}
      handoff(); document.body.append(inp); inp.click(); setTimeout(() => inp.remove(), 60000);
    },
  };
  const go3 = (k) => h('div', { class: 'grid3 check-btns', role: 'group', 'aria-label': '결과' }, ...[['OK', '된다'], ['FAIL', '안 된다'], ['NA', '해당 없음']].map(([v, t]) =>
    btn(t, () => mark(k, v), 'opt' + ((R[k] || {}).result === v ? ' on' : ''), { 'aria-pressed': (R[k] || {}).result === v ? 'true' : 'false' })));
  const done = CHECKS.filter(([k]) => R[k] && R[k].result).length;
  const summary = () => {
    const d = deviceInfo();
    const L = { OK: '된다', FAIL: '안 된다', NA: '해당 없음' };
    return [`[동구 수거 현장 점검] ${new Date().toLocaleString('ko-KR')}`, `기기: ${[d.model, d.os, d.browser].filter(Boolean).join(' · ')} · 앱 ${d.app} · ${d.standalone ? '설치한 앱' : '브라우저'}`,
      ...autoBits.map(([a, b]) => `- ${a}: ${b}`),
      ...CHECKS.map(([k, t]) => `- ${t}: ${R[k] && R[k].result ? L[R[k].result] : '아직'}${R[k] && R[k].note ? ` (${R[k].note})` : ''}`)].join('\n');
  };
  const copyBtn = btn('결과 복사(담당자에게 보내기)', async () => {
    const t = summary();
    try { await navigator.clipboard.writeText(t); toast('결과를 복사했습니다. 업무 메일·메신저에 붙여 넣어 보내세요'); }
    catch { await sheet({ title: '점검 결과', text: '아래 내용을 길게 눌러 복사하세요.', body: h('pre', { class: 'check-pre' }, t), choices: [], cancel: '닫기' }); }
  }, 'primary big');
  return h('main', { class: 'with-tabs' },
    h('header', { class: 'bar' }, btn('← 설정', () => go('#/settings'), 'ghost'), h('h1', { class: 'bar-title' }, '현장 점검')),
    h('section', { class: 'card notice' }, h('p', {}, '자동 시험으로는 확인할 수 없는 것을 이 휴대폰에서 눌러 보고 결과를 남깁니다. 결과에는 주민 정보가 들어가지 않습니다.'),
      h('p', { class: 'small' }, `진행 ${done}/${CHECKS.length}`)),
    h('section', { class: 'card' }, h('h2', {}, '이 휴대폰'), h('dl', {}, ...Object.entries(deviceInfo()).filter(([k]) => k !== 'standalone').flatMap(([k, v]) => [h('dt', {}, ({ os: '운영체제', browser: '브라우저', model: '모델', app: '앱 버전' })[k] || k), h('dd', {}, v || '-')]),
      ...autoBits.flatMap(([a, b]) => [h('dt', {}, a), h('dd', {}, b)]))),
    ...CHECKS.map(([k, t, how]) => h('section', { class: 'card check-item' + (R[k] && R[k].result ? ' r-' + R[k].result : '') },
      h('h2', {}, t, R[k] && R[k].result ? h('span', { class: 'chip ' + (R[k].result === 'OK' ? 'ok' : R[k].result === 'FAIL' ? 'bad' : '') }, ({ OK: '된다', FAIL: '안 된다', NA: '해당 없음' })[R[k].result]) : null),
      h('p', { class: 'small' }, how),
      tests[k] ? btn('시험', tests[k], 'big') : null,
      go3(k),
      (() => { const n = h('input', { type: 'text', class: 'search', placeholder: '메모 (예: 3.2초, 앱이 없어 웹으로 열림)', value: (R[k] || {}).note || '', 'aria-label': `${t} 메모`, maxlength: 120 });
        n.addEventListener('change', async () => { R[k] = { ...(R[k] || {}), note: n.value.trim().slice(0, 120) }; await commit(); }); return n; })())),
    h('div', { class: 'cta' }, copyBtn, btn('점검 결과 지우기', async () => { if (await ask('점검 결과를 모두 지울까요?', '지우기', { danger: true })) { st.deviceCheck = {}; await commit(); render(); } }, 'ghost')),
    tabBar('set'));
}

// ------------------------------------------------------------ 라우팅

export {
  deviceInfo, screenCheck,
};
