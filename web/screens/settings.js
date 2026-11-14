// 휴대폰 화면: 설정
import * as BIO from '../bio.js';
import * as F from '../field.js';
import * as M from '../map.js';
import * as NAV from '../nav.js';
import * as P from '../proto.js';
import * as S from '../store.js';
import { BUILTIN_FEES } from '../fees-data.js';
import { deriveStop } from '../fees.js';
import { won } from '../fees.js';
import { LOCK_CHOICES, TEXT_SIZES, addrRoute, allStops, applyTextSize, ask, autoLockStatus, btn, commit, errMsg, go, h, isDark, isSolo, prefs, render, setAutoLockEnabled, setLockMs, setPrefs, setSt, sheet, smsTemplates, st, stopCamera, tabBar, toast, unsent } from '../core.js';
import { CHECKS, installCard, runningAsApp } from '../widgets.js';
import { APP_VERSION } from '../app.js';

function autoLockControls(times) {
  const owner = st, hint = h('p', { class: 'small' }), error = h('p', { class: 'msg err', role: 'alert' });
  const toggle = btn('', async () => {
    if (st !== owner || !S.isUnlocked() || autoLockStatus().pending) return;
    const enabled = !autoLockStatus().enabled, pending = setAutoLockEnabled(enabled);
    sync();
    const saved = await pending;
    if (!root.isConnected || st !== owner) return;
    sync();
    if (saved) toast(enabled ? '자동 잠금을 켰습니다' : '자동 잠금을 껐습니다');
  }, 'opt big', { role: 'switch', 'aria-label': '자동 잠금' });
  const root = h('div', { class: 'auto-lock-controls' }, h('div', { class: 'fld-label' }, '자동 잠금'), toggle, hint, times, error);
  function sync() {
    const status = autoLockStatus();
    toggle.textContent = status.pending ? '설정 저장 중…' : status.enabled ? '자동 잠금 켜짐' : '자동 잠금 꺼짐';
    toggle.setAttribute('aria-checked', String(status.enabled));
    toggle.classList.toggle('on', status.enabled); toggle.disabled = !!status.pending;
    times.disabled = !status.enabled || !!status.pending;
    hint.textContent = status.enabled ? '무조작 시간이 지나면 잠깁니다. 카메라·전화·문자·길안내 앱은 10분, 다른 앱은 2분까지 자리를 비울 수 있습니다.'
      : '자동으로 잠그지 않습니다. 필요할 때 첫 화면의 [잠금]을 누르세요. 앱을 다시 열 때는 PIN을 입력합니다.';
    error.textContent = status.error;
  }
  sync();
  // 저장을 기다리며 설정 화면을 다시 열어도 이 부분만 갱신해 다른 입력 초안을 보존한다.
  const pending = autoLockStatus().pending;
  if (pending) pending.then(() => { if (root.isConnected && st === owner) sync(); });
  return root;
}

async function screenSettings() {
  const dev = await S.getRaw('device');
  let persisted = '확인 불가';
  try { persisted = navigator.storage && navigator.storage.persisted ? ((await navigator.storage.persisted()) ? '보존 요청됨' : '보존 요청 안 됨') : '지원 안 함'; } catch {}
  let quota = '';
  try { const e = await navigator.storage.estimate(); quota = `${(e.usage / 1048576).toFixed(1)}MB 사용`; } catch {}
  const set = async (patch, msg) => { Object.assign(st, patch); if ('lockMin' in patch) setLockMs(patch.lockMin * 60000); await commit(); if (msg) toast(msg); render(); };
  const choice = (opts, cur, onPick) => h('div', { class: 'grid' + Math.min(opts.length, 3) }, ...opts.map(([v, t]) => btn(t, () => onPick(v), cur === v ? 'opt on' : 'opt')));

  // 지문·얼굴 (WebAuthn PRF)
  const bio = await S.getBio().catch(() => null);
  const bioOk = bio ? true : await BIO.bioAvailable();
  const bioCard = h('section', { class: 'card' }, h('h2', {}, '잠금'),
    autoLockControls(h('fieldset', { class: 'auto-lock-times' }, h('legend', { class: 'fld-label' }, '무조작 시간'),
      choice(LOCK_CHOICES.map((m) => [m, `${m}분`]), LOCK_CHOICES.includes(st.lockMin) ? st.lockMin : 10, (v) => set({ lockMin: v }, `${v}분 동안 조작이 없으면 잠급니다`)))),
    h('div', { class: 'fld-label' }, '지문·얼굴로 열기'),
    bio ? h('div', {}, h('p', { class: 'small' }, `등록됨 (${bio.at.slice(0, 10)}). 데이터 키는 생체 인증 뒤에만 풀리는 키로 감싸 저장됩니다.`),
      btn('지문·얼굴 등록 해제', async () => { await S.removeBio(); toast('지문·얼굴 잠금 해제를 껐습니다'); render(); }, 'big'))
      : bioOk ? btn('지문·얼굴 잠금 해제 등록', async () => {
        const r = await sheet({ title: '지문·얼굴 등록', text: '먼저 지금 PIN을 넣어 주세요. 그 다음 휴대폰의 지문·얼굴 인증 창이 뜹니다. PIN은 계속 쓸 수 있습니다.',
          input: { type: 'password', label: 'PIN', placeholder: 'PIN', required: 'PIN을 넣어 주세요.' }, choices: [{ label: '다음', value: true, cls: 'primary', needInput: true }] });
        if (!r) return;
        // 실패하면 원인을 화면에 남긴다(담당자에게 알려 줄 수 있게). 개인정보는 들어가지 않음.
        let step = '인증 정보 만들기';
        try {
          const c = await BIO.bioCreate();
          if (!c.prfOut) {
            // 등록 응답에 키 보호 값이 없는 휴대폰(예: Galaxy의 Samsung Pass): 새로 누른 단추에서 한 번 더 인증해야 한다
            step = '한 번 더 인증';
            const go2 = await sheet({ title: '한 번 더 인증해 주세요', text: '이 휴대폰은 등록 직후 지문·얼굴을 한 번 더 확인해야 키 보호를 켤 수 있습니다.',
              choices: [{ label: '지문·얼굴 확인', value: true, cls: 'primary' }], cancel: '그만두기' });
            if (!go2) { toast('등록을 그만두었습니다'); return render(); }
            c.prfOut = await BIO.bioEvaluate(c.credId, c.prfSalt, c.transports);
          }
          step = 'PIN 확인·저장';
          await S.enrollBio(r.text, c);
          st.bioLastError = null; await commit();
          toast('지문·얼굴 잠금 해제를 등록했습니다');
        } catch (e) {
          st.bioLastError = { at: new Date().toISOString(), step, name: String(e.name || ''), code: String(e.code || ''), message: String(e.message || '').slice(0, 200), ua: navigator.userAgent.slice(0, 160) };
          await commit();
          toast(e.code === 'BAD_PIN' ? 'PIN이 맞지 않습니다.' : e.code === 'NO_PRF' ? e.message : e.name === 'NotAllowedError' ? '생체 인증을 취소했거나 시간이 지났습니다. 다시 해 보세요.' : `등록하지 못했습니다(${step}): ${e.name || ''} ${e.message || ''}`.trim(), 'bad');
        }
        render();
      }, 'big') : h('p', { class: 'small' }, '이 휴대폰(또는 브라우저)은 키 보호(PRF)를 지원하는 지문·얼굴 인증이 없어 PIN만 씁니다.'),
    !bio && st.bioLastError ? h('details', { class: 'small bio-err' }, h('summary', {}, `마지막 등록 오류 (${st.bioLastError.at.slice(0, 16).replace('T', ' ')}) — 담당자에게 알려 주세요`),
      h('pre', { class: 'small' }, [`단계: ${st.bioLastError.step}`, `오류: ${st.bioLastError.name} ${st.bioLastError.code}`.trim(), st.bioLastError.message, `앱 ${APP_VERSION} · ${st.bioLastError.ua}`].filter(Boolean).join('\n'))) : null);

  // 작업자
  const wIns = [0, 1, 2, 3, 4].map((i) => h('input', { type: 'text', class: 'search', placeholder: `작업자 ${i + 1} (예: 김수거)`, value: (st.workers || [])[i] || '', 'aria-label': `작업자 ${i + 1} 이름`, maxlength: 10 }));
  const workerCard = h('section', { class: 'card' }, h('h2', {}, '작업자'),
    h('p', { class: 'small' }, '이 휴대폰을 함께 쓰는 수거팀 이름(짧게). 첫 화면에서 고른 작업자가 모든 현장 기록에 남습니다. 교대할 때 PIN 없이 바꿀 수 있습니다.'),
    ...wIns, btn('작업자 저장', async () => {
      const ws = [...new Set(wIns.map((x) => x.value.trim().slice(0, 10)).filter(Boolean))];
      await set({ workers: ws, worker: ws.includes(st.worker) ? st.worker : ws[0] || '' }, ws.length ? `작업자 ${ws.length}명을 저장했습니다` : '작업자 목록을 비웠습니다');
    }, 'big'));

  // 문자 문구
  const tpls = smsTemplates();
  const tIns = tpls.map((t) => { const ta = h('textarea', { rows: 3, 'aria-label': `문자 문구: ${t.label}` }); ta.value = t.text; return ta; });
  const smsMsg = h('p', { class: 'msg err', role: 'alert' });
  const smsCard = h('section', { class: 'card' }, h('h2', {}, '문자 문구'),
    h('p', { class: 'small' }, `넣을 수 있는 자리표시: ${Object.entries(F.SMS_VARS).map(([k, v]) => `${k} ${v}`).join(', ')}. 주민 이름·연락처·주소는 넣을 수 없습니다.`),
    ...tpls.map((t, i) => h('label', { class: 'fld' }, h('span', {}, t.label), tIns[i])),
    smsMsg,
    h('div', { class: 'grid2' },
      btn('문구 저장', async () => {
        for (let i = 0; i < tpls.length; i++) { const err = F.checkSmsTemplate(tIns[i].value, allStops()); if (err) { smsMsg.textContent = `${tpls[i].label}: ${err}`; return; } }
        await set({ smsTemplates: tpls.map((t, i) => ({ id: t.id, label: t.label, text: tIns[i].value.trim() })) }, '문자 문구를 저장했습니다');
      }, 'opt'),
      btn('기본 문구로', () => set({ smsTemplates: null }, '기본 문구로 되돌렸습니다'), 'opt')));

  // 지도
  const tc = await M.tileCacheInfo();
  const pmsg = h('p', { class: 'msg', role: 'status' });
  const prefetch = btn('동구 지도 미리 받기', async () => {
    if (!navigator.serviceWorker || !navigator.serviceWorker.controller) { pmsg.textContent = '앱을 한 번 닫았다 다시 연 뒤에 받을 수 있습니다(오프라인 저장 준비 중).'; return; }
    prefetch.disabled = true;
    const pts = allStops().filter((s) => !s.removed && s.status !== 'CANCELLED').map((s) => deriveStop(s, st.events).loc).filter(Boolean);
    try {
      const r = await M.prefetchTiles(M.bboxAround(pts), (n, t) => { pmsg.textContent = `받는 중 ${n} / ${t}`; });
      pmsg.textContent = r.fail === r.total ? '지도 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.' : `${pts.length ? '오늘 지점 주변' : '동구 전체'} 지도 조각 ${r.total - r.fail}개를 받았습니다${r.fail ? ` (실패 ${r.fail})` : ''}.`;
      if (r.fail < r.total && st) { st.mapPrefetchedAt = new Date().toISOString(); commit(); }
    } catch { pmsg.textContent = '지도 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.'; }
    prefetch.disabled = false;
  }, 'big', { disabled: !st.mapEnabled });
  const mapCard = h('section', { class: 'card' }, h('h2', {}, '지도'),
    h('p', { class: 'small' }, '지도를 켜면 바탕 지도 조각을 인터넷(OpenFreeMap)에서 받습니다. 이름·연락처·주소·핀 위치는 지도 서버로 보내지 않습니다.'),
    btn(st.mapEnabled ? '지도 사용 끄기' : '지도 사용 켜기', async () => { st.mapEnabled = !st.mapEnabled; await commit(); render(); }, 'big'),
    h('p', { class: 'small' }, `인터넷이 약한 곳에 대비해 오늘 지점 주변(없으면 동구 전체)의 넓은 범위 지도(줌 14 이하)를 미리 받아 둡니다. 보관 ${tc.count}개 · ${(tc.bytes / 1048576).toFixed(1)}MB (최대 50MB, 오래 안 쓴 것부터 지움)`),
    prefetch, pmsg);

  // 길안내 앱
  const navCard = h('section', { class: 'card' }, h('h2', {}, '길안내 앱'),
    h('div', { class: 'grid2', role: 'group', 'aria-label': '길안내 앱' }, ...Object.entries(NAV.NAV_APPS).map(([k, t]) =>
      btn(t, () => set({ navApp: k, navAppSet: true }, `길안내는 ${t}로 엽니다`), (st.navApp || NAV.DEFAULT_NAV_APP) === k ? 'opt on' : 'opt'))),
    h('p', { class: 'small' }, "지도 앱에는 목적지 좌표와 'N번 수거지'(반입지는 '반입지') 표시만 전달됩니다. 이름·연락처·주소는 넘기지 않습니다. 앱이 없으면 카카오맵·네이버지도는 웹 지도로, T맵은 기본 지도 앱으로 엽니다."));

  // 주소 검색 (행정안전부 도로명주소 API)
  const kIn = h('input', { type: 'text', class: 'search', autocomplete: 'off', spellcheck: 'false', 'aria-label': '주소 검색 승인키', placeholder: '주소 검색 승인키 (confmKey)', value: st.jusoKey || '' });
  const cIn = h('input', { type: 'text', class: 'search', autocomplete: 'off', spellcheck: 'false', 'aria-label': '좌표 승인키', placeholder: '좌표제공 승인키 (confmKey)', value: st.jusoCoordKey || '' });
  const jusoCard = h('section', { class: 'card' }, h('h2', {}, '주소 검색'),
    h('p', { class: 'small' }, '행정안전부 도로명주소 검색·좌표제공 API(business.juso.go.kr)를 씁니다. 보내는 것은 검색칸에 적은 주소 낱말과 승인키, 고른 건물의 식별 번호뿐이며 이름·연락처는 보내지 않습니다. 승인키는 주소정보누리집에서 기관이 발급받아 넣습니다(이 휴대폰에 암호화 저장).'),
    h('div', { class: 'fld-label' }, '주소 검색 경로'),
    choice([['direct', '직접'], ['pc', 'PC 경유']], addrRoute(), (v) => set({ addrRoute: v })),
    h('p', { class: 'small' }, addrRoute() === 'pc' ? 'PC 경유: 휴대폰에서는 검색하지 않습니다. PC 연결 방식에서는 PC 접수 화면의 [주소 찾기](PC의 주소 자료 + PC 서버가 대신 묻는 행안부 검색)로 주소·좌표를 정해 목록에 실어 보냅니다. 휴대폰과 PC는 실시간으로 연결되지 않으므로 휴대폰에서 PC로 검색을 보내지는 않습니다.'
      : '직접: 이 휴대폰이 주소 서버에 바로 묻습니다. 브라우저가 주소 서버 응답을 막으면(CORS) 검색이 되지 않으니, 그때는 직접 적고 지도에서 위치를 지정하거나 PC에서 주소 작업을 하세요.'),
    h('label', { class: 'fld' }, h('span', {}, '주소 검색 승인키'), kIn),
    h('label', { class: 'fld' }, h('span', {}, '좌표 승인키'), cIn),
    btn('승인키 저장', () => set({ jusoKey: kIn.value.trim(), jusoCoordKey: cIn.value.trim() }, kIn.value.trim() ? '승인키를 저장했습니다' : '승인키를 지웠습니다'), 'big'));

  return h('main', { class: 'with-tabs' },
    h('header', { class: 'bar' }, h('h1', { class: 'bar-title' }, '설정·정보')),
    // 화면(밝기·글자 크기)은 맨 위에: 자주 바꾸고, '자동'이 휴대폰의 어두운 화면을 따라가 어둡게만 보이는 경우가 있어 찾기 쉽게
    h('section', { class: 'card screen-card' }, h('h2', {}, '화면'),
      h('div', { class: 'fld-label' }, '밝기'),
      h('div', { class: 'seg theme-seg', role: 'group', 'aria-label': '밝기 테마' }, ...[['auto', '자동'], ['light', '밝게'], ['dark', '어둡게']].map(([k, t]) =>
        btn([h('span', { class: 'tp tp-' + k, 'aria-hidden': 'true' }, h('i'), h('i')), h('span', {}, t)], async () => { await setPrefs({ theme: k }); render(); },
          'opt seg-opt' + ((prefs.theme || 'auto') === k ? ' on' : ''), { 'aria-label': t }))),
      h('p', { class: 'small' }, (prefs.theme || 'auto') === 'auto' ? `자동: 휴대폰 설정을 따릅니다 (지금 ${isDark() ? '어둡게' : '밝게'}). 첫 화면 위 ☀/🌙로 바로 바꿀 수 있습니다.` : '첫 화면 위 ☀/🌙로도 바꿀 수 있습니다.'),
      h('div', { class: 'fld-label' }, '글자 크기'),
      h('div', { class: 'seg size-seg', role: 'group', 'aria-label': '글자 크기' }, ...TEXT_SIZES.map(([k, t, px]) =>
        btn(t, async () => { st.textSize = k; applyTextSize(); await commit(); render(); }, 'opt seg-opt' + ((st.textSize || '') === k ? ' on' : ''), { 'data-px': px }))),
      h('div', { class: 'fld-label' }, '야외 고대비'),
      h('p', { class: 'small' }, '햇빛 아래에서 잘 보이게 글자·테두리를 진하게 합니다. 휴대폰의 "대비 높이기" 설정을 켜도 적용됩니다.'),
      choice([[false, '끔'], [true, '켬']], !!prefs.hc, async (v) => { await setPrefs({ hc: v }); render(); })),
    bioCard, workerCard,
    await installCard(),
    h('section', { class: 'card' }, h('h2', {}, '도움말'), h('p', { class: 'small' }, '한 지점에서 무엇을 어떤 순서로 하는지 한 장으로 봅니다.'),
      btn('현장 처리 순서', () => go('#/help'), 'big')),
    h('section', { class: 'card' }, h('h2', {}, '현장 점검'), h('p', { class: 'small' }, '설치·지문·주소 검색·길안내·카메라가 이 휴대폰에서 되는지 눌러 보고, 결과를 담당자에게 보냅니다.'),
      btn(`현장 점검${st.deviceCheck && Object.keys(st.deviceCheck).length ? ` (${CHECKS.filter(([k]) => st.deviceCheck[k] && st.deviceCheck[k].result).length}/${CHECKS.length})` : ''}`, () => go('#/check'), 'big')),
    h('section', { class: 'card' }, h('dl', {},
      h('dt', {}, '앱 버전'), h('dd', {}, APP_VERSION),
      h('dt', {}, '실행 위치'), h('dd', {}, runningAsApp() ? '설치한 앱' : '브라우저 탭'),
      h('dt', {}, '사용 방식'), h('dd', {}, st.mode === 'SOLO' ? '이 휴대폰만 사용' : 'PC와 연결'),
      h('dt', {}, '기기 번호'), h('dd', {}, dev ? P.uuidStr(dev.id).slice(0, 8) : '미등록'),
      h('dt', {}, '저장소'), h('dd', {}, `${persisted} ${quota}`),
      h('dt', {}, '미전송 기록'), h('dd', {}, unsent().length + '건'))),
    h('section', { class: 'card' }, h('h2', {}, '현장 기록'),
      h('div', { class: 'fld-label' }, '도착·사진에 휴대폰 위치 남기기'),
      h('p', { class: 'small' }, '[도착]을 누르거나 사진을 찍을 때 위치 권한이 허용되어 있으면 위치·오차를 함께 기록합니다(결과 파일로 PC에만 전달).'),
      choice([[true, '남김'], [false, '안 남김']], st.geoEvidence !== false, (v) => set({ geoEvidence: v })),
      isSolo() ? null : h('div', {}, h('div', { class: 'fld-label' }, '미전송 알림'),
        h('p', { class: 'small' }, '보내지 않은 기록이 이 시간보다 오래되면 첫 화면에 알립니다. 오후 5시 뒤에는 1건이라도 있으면 알립니다.'),
        choice([[1, '1시간'], [3, '3시간'], [6, '6시간']], st.unsentWarnH || 3, (v) => set({ unsentWarnH: v })),
        btn('안전용 암호화 백업', () => go('#/backup'), 'big')),
      isSolo() ? h('div', {}, h('div', { class: 'fld-label' }, '개인정보 보관 기간'),
        h('p', { class: 'small' }, '처리가 끝난 접수는 이 기간이 지나고 백업이 끝났으면 첫 화면에서 알린 뒤 이름·연락처·주소를 지웁니다.'),
        choice([[30, '30일'], [90, '90일'], [180, '180일']], st.retentionDays || 90, (v) => set({ retentionDays: v }))) : null),
    smsCard, navCard, jusoCard, mapCard,
    isSolo() ? soloFeeCard() : null,
    h('section', { class: 'card' },
      isSolo() ? null : btn('기기 등록 화면', () => go('#/register'), 'big'),
      btn('사용 방식 바꾸기', async () => {
        if (st.requests.length || st.daylist || st.events.length) return toast('자료가 있어 바꿀 수 없습니다. 백업·정리 후 바꿔 주세요.', 'bad');
        st.mode = null; await commit(); go('#/mode');
      }, 'big'),
      h('p', { class: 'small' }, '이 앱은 서버에 자료를 보내지 않습니다. 브라우저의 "사이트 데이터 삭제"를 하면 보내지 않은 기록이 사라지니 주의하세요.'),
      btn('이 휴대폰의 모든 자료 지우기', async () => {
        const u = unsent().length;
        const rec = new Set(st.recoveredPhotos || []);
        const ph = [...st.events.filter((e) => e.kind === 'PHOTO_META').map((e) => e.body.photo_id), ...(st.keptPhotos || []).map((p) => p.photo_id)].filter((x) => !rec.has(x)).length;
        if (!(await ask((u ? `보내지 않은 기록 ${u}건이 사라집니다. ` : '') + (ph ? `PC 회수가 확인되지 않은 사진 ${ph}장도 사라집니다. ` : '') + 'PIN, 기기 키, 목록, 사진을 모두 지웁니다. 다시 등록해야 합니다. 되돌릴 수 없습니다.', '모두 지우기', { title: '이 휴대폰의 모든 자료 지우기', danger: true }))) return;
        if ((u || ph) && !(await ask(`정말 지울까요? 보내지 않은 기록 ${u}건${ph ? `과 회수 안 된 사진 ${ph}장` : ''}은 어디에도 남지 않습니다.`, '그래도 지우기', { title: '한 번 더 확인', danger: true }))) return;
        stopCamera();
        try { await S.wipeAll(); } catch (e) { return toast('지우지 못했습니다: ' + errMsg(e), 'bad'); }
        setSt(null); location.hash = ''; location.reload();
      }, 'danger')),
    tabBar('set'));
}

// 단독 방식: 확인 필요 품목의 단가 정하기 (근거 필수, 바꾼 이력 보존)
function soloFeeCard() {
  const ov = st.feeOverrides || {};
  const review = BUILTIN_FEES.rows.filter((r) => r.status !== 'CONFIRMED');
  const msg = h('p', { class: 'msg' });
  return h('section', { class: 'card' }, h('h2', {}, `확인 필요 품목 단가 (${review.length})`),
    h('p', { class: 'small' }, '원문 규격이 겹치거나 단위가 불명확한 품목입니다. 담당 부서 기준을 확인한 것만 정하세요. 정한 단가는 그 뒤 새로 접수·추가하는 품목에만 쓰입니다.'),
    ...review.map((r) => {
      const price = h('input', { type: 'number', inputmode: 'numeric', min: 1, placeholder: '원', value: ov[r.id] ? ov[r.id].price : '' });
      const why = h('input', { type: 'text', placeholder: '근거 (예: 청소과 확인 11/3)', value: ov[r.id] ? ov[r.id].reason : '' });
      return h('div', { class: 'line' }, h('div', { class: 'lhead' }, h('strong', {}, `${r.name} ${r.spec}`), h('em', { class: 'tag' }, ov[r.id] ? `확정 ${won(ov[r.id].price)}` : '확인 대기')),
        h('p', { class: 'small' }, r.note || ''), h('div', { class: 'grid2' }, price, why),
        h('div', { class: 'grid2' },
          btn('저장', async () => {
            const n = parseInt(price.value, 10);
            if (!(n > 0)) return (msg.textContent = '단가는 0원보다 커야 합니다. 모르면 확인 대기로 두세요.');
            if (!why.value.trim()) return (msg.textContent = '근거를 적어 주세요.');
            st.feeOverrides = { ...ov, [r.id]: { price: n, reason: why.value.trim(), at: new Date().toISOString() } };
            st.feeRev = (st.feeRev || 0) + 1; (st.feeLog ||= []).push({ id: r.id, price: n, reason: why.value.trim(), at: new Date().toISOString() });
            await commit(); toast('저장했습니다'); render();
          }, 'opt'),
          ov[r.id] ? btn('확인 대기로 되돌리기', async () => {
            const { [r.id]: _, ...rest } = ov; st.feeOverrides = rest; st.feeRev = (st.feeRev || 0) + 1;
            (st.feeLog ||= []).push({ id: r.id, price: null, reason: '되돌림', at: new Date().toISOString() }); await commit(); render();
          }, 'opt') : h('span')));
    }), msg);
}

export {
  screenSettings, soloFeeCard,
};
