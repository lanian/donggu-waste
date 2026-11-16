// 휴대폰 화면: 처음 설정·잠금 해제
import * as B from '../backup.js';
import * as BIO from '../bio.js';
import * as S from '../store.js';
import { afterUnlock, appLogo, btn, errMsg, go, h, isIOS, loadState, msgBox, pendingPhotos, render, say } from '../core.js';
import { runningAsApp } from '../widgets.js';

function screenSetup() {
  const msg = msgBox('', { id: 'pin-setup-msg' });
  const p1 = h('input', { type: 'password', inputmode: 'numeric', autocomplete: 'new-password', placeholder: '숫자 6자리 이상', class: 'code-in', 'aria-label': '새 PIN' });
  const p2 = h('input', { type: 'password', inputmode: 'numeric', autocomplete: 'new-password', placeholder: '한 번 더', class: 'code-in', 'aria-label': '새 PIN 한 번 더' });
  // 틀리면 문구(role=alert)와 함께 그 칸에 aria-invalid + aria-describedby
  const bad = (input, text) => {
    for (const x of [p1, p2]) { x.removeAttribute('aria-invalid'); x.removeAttribute('aria-describedby'); }
    if (input) { input.setAttribute('aria-invalid', 'true'); input.setAttribute('aria-describedby', msg.id); input.focus(); }
    say(msg, text, !!input);
  };
  const ok = btn('PIN 설정', async () => {
    if (!/^\d{6,}$/.test(p1.value)) return bad(p1, '숫자 6자리 이상으로 정해 주세요.');
    if (p1.value !== p2.value) return bad(p2, '두 번 입력한 PIN이 다릅니다.');
    ok.disabled = true; bad(null, '설정 중…');
    try {
      await S.setupPin(p1.value);
      await S.persist();
      await loadState(); go('#/mode'); render();
    } catch (e) { say(msg, 'PIN을 설정하지 못했습니다: ' + errMsg(e), true); ok.disabled = false; }
  }, 'primary big');
  return h('main', { class: 'center' }, appLogo('app-icon'),
    h('h1', {}, '동구 대형폐기물 수거'),
    h('p', {}, '이 휴대폰에서 쓸 잠금 PIN을 정하세요. 업무 자료는 이 PIN으로 잠긴 채 휴대폰 안에만 저장됩니다.'),
    p1, p2, ok, msg,
    isIOS() && !runningAsApp() ? h('p', { class: 'callout warn small' }, '아이폰은 Safari와 홈 화면 앱이 자료를 따로 보관합니다. 먼저 공유 단추(□↑) → [홈 화면에 추가]로 설치하고, 설치한 앱에서 PIN을 정하세요.') : null,
    h('p', { class: 'small' }, '시제품입니다. 가상 자료로만 사용하세요.'));
}

function screenLock() {
  const msg = msgBox('', { id: 'pin-msg' });
  const pin = h('input', { type: 'password', inputmode: 'numeric', autocomplete: 'current-password', placeholder: 'PIN', class: 'code-in', 'aria-label': 'PIN' });
  const ok = btn('잠금 해제', async () => {
    ok.disabled = true; say(msg, '확인 중…');
    try { await S.unlock(pin.value); await afterUnlock(); }
    catch (e) {
      ok.disabled = false; pin.value = '';
      // PIN 오류: 문구는 role=alert, PIN 칸은 aria-invalid + aria-describedby로 문구와 잇는다
      pin.setAttribute('aria-invalid', 'true'); pin.setAttribute('aria-describedby', msg.id);
      say(msg, e.until && e.until > Date.now() ? `여러 번 틀렸습니다. ${Math.ceil((e.until - Date.now()) / 1000)}초 뒤 다시 시도하세요.` : 'PIN이 맞지 않습니다.', true);
      pin.focus();
    }
  }, 'primary big');
  pin.addEventListener('input', () => { pin.removeAttribute('aria-invalid'); });
  pin.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
  // 지문·얼굴: 등록되어 있고 PRF로 데이터 키를 풀 수 있을 때만
  const bioBtn = btn('지문·얼굴로 열기', async () => {
    bioBtn.disabled = true; say(msg, '생체 인증 중…');
    try { const B = await S.getBio(); const out = await BIO.bioEvaluate(B.credId, B.prfSalt, B.transports); await S.unlockBio(out); await afterUnlock(); }
    catch (e) { bioBtn.disabled = false; say(msg, e.code === 'BIO_FAIL' || e.code === 'NO_PRF' ? e.message : e.name === 'NotAllowedError' ? '생체 인증을 취소했거나 시간이 지났습니다. 다시 누르거나 PIN으로 여세요.' : `생체 인증을 하지 못했습니다(${e.name || '오류'}). PIN으로 여세요.`, true); }
  }, 'big', { hidden: true });
  S.getBio().then((b) => { if (b) bioBtn.hidden = false; }).catch(() => {});
  setTimeout(() => pin.focus(), 50);
  const waiting = pendingPhotos.length ? h('p', { class: 'callout warn pending-photo', role: 'status' }, `사진 ${pendingPhotos.length}장 저장 대기 — 잠금 해제 후 저장됩니다`) : null;
  return h('main', { class: 'center' }, appLogo('app-icon'), h('h1', {}, '잠겨 있습니다'), waiting, pin, ok, bioBtn, msg);
}

// ------------------------------------------------------------ 홈

export {
  screenLock, screenSetup,
};
