// 휴대폰 화면: 기기 등록
import * as P from '../proto.js';
import * as S from '../store.js';
import { btn, errMsg, flowSteps, footBar, go, h, render, startCamera, stopCamera, toast, ui } from '../core.js';
import { drawQr } from './resultqr.js';

async function screenRegister() {
  const pc = await S.getRaw('pc');
  let dev = await S.getRaw('device');
  if (!dev) {
    const keys = await P.generateDeviceKeys();
    dev = { id: P.randomBytes(16), kex: keys.kex, sign: keys.sign };
    await S.putRaw('device', dev);
  }
  const back = h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '기기 등록'));
  const STEPS = ['PC 등록 QR 읽기', '기기 코드 전달', '지문 숫자 대조'];
  if (!pc || ui.rescanPc) {
    const video = h('video', { playsinline: true, muted: true, class: 'cam' });
    const msg = h('p', { class: 'msg' }, '관리자 PC의 [기기 등록 QR]을 비추세요.');
    setTimeout(() => startCamera(video, async (text) => {
      try {
        const reg = P.parseRegistration(text);
        stopCamera();
        await S.putRaw('pc', reg); ui.rescanPc = false; render();
      } catch (e) { msg.textContent = errMsg(e); }
    }).catch((e) => { msg.textContent = errMsg(e); }), 0);
    return h('main', {}, back, flowSteps(STEPS, 0), h('section', { class: 'card cam-card' }, video, msg));
  }
  const kexRaw = await P.exportRaw(dev.kex.publicKey), signRaw = await P.exportRaw(dev.sign.publicKey);
  const code = await P.deviceCode(dev.id, kexRaw, signRaw);
  const fp = await P.fingerprint(pc.pcSignPub, pc.pcKexPub, dev.id, kexRaw, signRaw);
  const ta = h('textarea', { readonly: true, rows: 4, class: 'code' }); ta.value = code;
  const qr = h('canvas', { class: 'qrcanvas devqr', role: 'img', 'aria-label': '기기 코드 QR' });
  setTimeout(() => { if (qr.isConnected) drawQr(qr, code, 'Byte'); }, 0);
  return h('main', { class: 'with-dock' }, back, flowSteps(STEPS, 1),
    h('section', { class: 'card' },
      h('h2', {}, h('span', { class: 'stepno' }, '2'), '기기 코드 전달'),
      h('p', {}, '관리자 PC [설정 → 기기 → 휴대폰 QR 웹캠으로 읽기]를 누른 뒤, 이 QR을 PC 웹캠에 비추세요. 읽으면 PC에 바로 등록됩니다. (공개키만 들어 있습니다)'),
      h('div', { class: 'qrwrap' }, qr),
      h('details', { class: 'devcode-alt' }, h('summary', {}, 'PC에 웹캠이 없을 때: 코드 복사'),
        h('p', { class: 'small' }, '코드를 복사해 업무 메일 등 승인된 경로로 관리자에게 보내면 PC에 붙여 넣어 등록합니다.'),
        ta, btn('코드 복사', async () => { try { await navigator.clipboard.writeText(code); toast('복사했습니다'); } catch { ta.select(); } }, 'big'))),
    h('section', { class: 'card' },
      h('h2', {}, h('span', { class: 'stepno' }, '3'), '지문 숫자 대조'),
      h('p', {}, 'PC 화면에 나온 지문 9자리가 아래와 같은지 확인하세요. 같으면 PC에서 [지문 같음 · 사용 시작]을 눌러야 이 휴대폰으로 목록을 보낼 수 있습니다.'),
      h('p', { class: 'fp' }, `${fp.slice(0, 3)} ${fp.slice(3, 6)} ${fp.slice(6)}`),
      h('p', { class: 'small' }, '기기 번호 ' + P.uuidStr(dev.id).slice(0, 8))),
    btn('PC 등록 QR 다시 읽기', () => { ui.rescanPc = true; render(); }, 'ghost link'),
    footBar(btn('완료', () => go('#/home'), 'primary huge')));
}

// ------------------------------------------------------------ 목록 받기

export {
  screenRegister,
};
