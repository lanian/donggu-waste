// 휴대폰 화면: 기기 등록
import * as P from '../proto.js';
import * as S from '../store.js';
import { ask, backBtn, btn, errMsg, flowSteps, footBar, go, h, msgBox, onLock, render, say, startCamera, stopCamera, toast, ui } from '../core.js';
import { drawQr } from './resultqr.js';
import { makePairSubmission, readPairRegistration, registrationNumber, submitPairPacket } from '../pairing.js';

let pairing = null, expiryTimer, numberTimer;
function clearPairing() {
  pairing?.controller?.abort();
  pairing = null;
  clearTimeout(expiryTimer);
  clearInterval(numberTimer);
}
onLock(clearPairing);
addEventListener('hashchange', () => { if (location.hash.split('?')[0] !== '#/register') clearPairing(); });
function rescan() {
  clearPairing();
  ui.pairingExpired = false;
  ui.rescanPc = true;
  render();
}
function registrationFileControls(code) {
  const file = new File([code], '수거폰-등록.dgdevice', { type: 'text/plain' });
  const msg = msgBox();
  const save = btn('등록 파일 저장', () => {
    const url = URL.createObjectURL(file);
    const link = h('a', { href: url, download: file.name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    say(msg, '등록 파일 다운로드를 시작했습니다. 파일을 PC 담당자에게 전달하세요.');
  }, 'big');
  const share = navigator.share && navigator.canShare?.({ files: [file] }) ? btn('등록 파일 공유', async () => {
    share.disabled = true;
    try {
      await navigator.share({ files: [file], title: '수거폰 등록 파일' });
      say(msg, 'PC 담당자가 파일을 열면 아래의 지문 숫자를 함께 확인하세요.');
    } catch (error) {
      if (error.name === 'AbortError') say(msg, '공유를 취소했습니다.');
      else say(msg, '파일을 공유할 수 없습니다. [등록 파일 저장]을 이용하세요.', true);
    } finally { share.disabled = false; }
  }, 'big') : null;
  return h('div', {},
    h('p', {}, '등록 파일을 업무 메일이나 승인된 매체로 PC에 전달하세요. PC의 [설정 → 기기 → 수거폰 등록 파일]에서 엽니다.'),
    h('div', { class: 'row' }, save, share), msg);
}
function pairingCard(session, dev, code) {
  const msg = msgBox(), result = h('div', {});
  const current = () => pairing === session && S.isUnlocked() && Date.now() < session.expires;
  async function displayNumber() {
    if (!current()) return;
    const number = h('output', { class: 'fp', 'aria-label': 'PC에 입력할 등록번호', 'aria-live': 'off' });
    const countdown = h('p', { class: 'small' });
    send.hidden = true;
    result.replaceChildren(number, countdown);
    let shownStep = -1;
    const update = async () => {
      if (!current()) return;
      const now = Date.now(), step = Math.floor(now / 30000);
      countdown.textContent = `다음 번호까지 ${30 - Math.floor(now / 1000) % 30}초`;
      if (shownStep !== step) {
        shownStep = step;
        const value = await registrationNumber(session.packet.signingKey, step);
        if (current() && shownStep === step) number.textContent = value;
      }
    };
    clearInterval(numberTimer);
    numberTimer = setInterval(update, 500);
    await update();
  }
  const send = btn('등록번호 받기', async () => {
    if (!current() || session.controller) return;
    const controller = session.controller = new AbortController();
    send.disabled = true;
    say(msg, '기기 등록 정보를 암호화해 보내고 있습니다.');
    try {
      session.packet ||= await makePairSubmission(session, code, dev.sign.privateKey);
      if (!current()) return;
      await submitPairPacket(session.packet, controller.signal);
      if (!current()) return;
      session.submitted = true;
      say(msg, '아래 번호를 관리자 PC에 입력하세요.');
      await displayNumber();
    } catch (error) { if (current()) say(msg, errMsg(error), true); }
    finally { session.controller = null; send.disabled = false; }
  }, 'primary big');
  if (session.submitted) displayNumber();
  return h('section', { class: 'card' }, h('h2', {}, '6자리 간편 등록'),
    h('p', {}, '등록번호는 30초마다 바뀝니다. PC가 표시한 QR은 5분 동안 사용할 수 있습니다.'),
    h('p', { class: 'small' }, `[등록번호 받기]를 누르면 기기 등록용 암호문만 ${new URL(session.relay).host}에 보냅니다.`),
    send, result, msg);
}

async function screenRegister() {
  const pc = await S.getRaw('pc');
  let dev = await S.getRaw('device');
  if (!dev) {
    const keys = await P.generateDeviceKeys();
    dev = { id: P.randomBytes(16), kex: keys.kex, sign: keys.sign };
    await S.putRaw('device', dev);
  }
  const back = h('header', { class: 'bar' }, backBtn(), h('h1', { class: 'bar-title' }, '기기 등록'));
  const STEPS = ['PC 등록 QR 읽기', '기기 코드 전달', '지문 숫자 대조'];
  if (!pc || ui.rescanPc) {
    clearPairing();
    const video = h('video', { playsinline: true, muted: true, class: 'cam' });
    const msg = msgBox('관리자 PC의 [기기 등록 QR]을 비추세요.');
    setTimeout(() => startCamera(video, async (text) => {
      try {
        if (!video.isConnected || !S.isUnlocked()) return;
        const { pc: reg, session } = readPairRegistration(text);
        stopCamera();
        if (pc && (!P.eq(reg.pcSignPub, pc.pcSignPub) || !P.eq(reg.pcKexPub, pc.pcKexPub))) {
          const change = await ask('현재 등록된 PC와 다른 QR입니다. 이 휴대폰이 목록을 받고 결과를 보낼 PC를 새로 읽은 PC로 변경할까요?', 'PC 변경', { title: '등록된 PC 변경' });
          if (!video.isConnected || !S.isUnlocked()) return;
          if (!change) { ui.rescanPc = false; render(); return; }
        }
        if (session && Date.now() >= session.expires) { ui.rescanPc = false; ui.pairingExpired = true; render(); return; }
        await S.putRaw('pc', reg);
        if (!video.isConnected || !S.isUnlocked()) return;
        ui.rescanPc = false; ui.pairingExpired = false;
        if (session) {
          pairing = session;
          expiryTimer = setTimeout(() => {
            if (pairing !== session) return;
            clearPairing(); ui.pairingExpired = true; render();
          }, Math.max(0, session.expires - Date.now()));
        }
        render();
      } catch (e) { say(msg, errMsg(e), true); }
    }).catch((e) => { say(msg, errMsg(e), true); }), 0);
    return h('main', {}, back, flowSteps(STEPS, 0), h('section', { class: 'card cam-card' }, video, msg));
  }
  const kexRaw = await P.exportRaw(dev.kex.publicKey), signRaw = await P.exportRaw(dev.sign.publicKey);
  const code = await P.deviceCode(dev.id, kexRaw, signRaw);
  if (pairing && (!P.eq(pc.pcSignPub, pairing.pc.pcSignPub) || !P.eq(pc.pcKexPub, pairing.pc.pcKexPub))) clearPairing();
  if (pairing || ui.pairingExpired) return h('main', { class: 'with-dock' }, back,
    flowSteps(['PC 등록 QR 읽기', 'PC에 등록번호 입력'], 1),
    pairing ? pairingCard(pairing, dev, code) : h('section', { class: 'card' }, h('h2', {}, '등록 시간이 끝났습니다'), h('p', {}, 'PC에서 새 간편 등록 QR을 열고 다시 읽으세요.')),
    btn('PC 등록 QR 다시 읽기', rescan, 'ghost link'), footBar(btn('완료', () => go('#/home'), 'primary huge')));
  const fp = await P.fingerprint(pc.pcSignPub, pc.pcKexPub, dev.id, kexRaw, signRaw);
  const ta = h('textarea', { readonly: true, rows: 4, class: 'code' }); ta.value = code;
  const qr = h('canvas', { class: 'qrcanvas devqr', role: 'img', 'aria-label': '기기 코드 QR' });
  setTimeout(() => { if (qr.isConnected) drawQr(qr, code, 'Byte'); }, 0);
  return h('main', { class: 'with-dock' }, back, flowSteps(STEPS, 1),
    h('section', { class: 'card' },
      h('h2', {}, h('span', { class: 'stepno' }, '2'), '기기 코드 전달'),
      registrationFileControls(code),
      h('p', { class: 'small' }, 'PC에 웹캠이 있으면 [휴대폰 QR 웹캠으로 읽기]를 누르고 아래 QR을 비춰도 됩니다.'),
      h('div', { class: 'qrwrap' }, qr),
      h('details', { class: 'devcode-alt' }, h('summary', {}, '기기 코드 복사'),
        h('p', { class: 'small' }, '코드를 복사해 업무 메일 등 승인된 경로로 관리자에게 보내면 PC에 붙여 넣어 등록합니다.'),
        ta, btn('코드 복사', async () => { try { await navigator.clipboard.writeText(code); toast('복사했습니다'); } catch { ta.select(); } }, 'big'))),
    h('section', { class: 'card' },
      h('h2', {}, h('span', { class: 'stepno' }, '3'), '지문 숫자 대조'),
      h('p', {}, '관리자와 함께 PC 화면의 지문 9자리가 아래와 같은지 확인하세요. 번호가 일치하면 관리자가 PC에서 비밀번호와 Google OTP로 이 휴대폰을 승인합니다.'),
      h('p', { class: 'fp' }, `${fp.slice(0, 3)} ${fp.slice(3, 6)} ${fp.slice(6)}`),
      h('p', { class: 'small' }, '기기 번호 ' + P.uuidStr(dev.id).slice(0, 8))),
    btn('PC 등록 QR 다시 읽기', rescan, 'ghost link'),
    footBar(btn('완료', () => go('#/home'), 'primary huge')));
}

// ------------------------------------------------------------ 목록 받기

export {
  screenRegister,
};
