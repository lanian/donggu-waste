import qrcode from '/web/vendor/qrcode.mjs';
import { btn, h, modal, sessionOwner } from '../core.js';

function draw(canvas, text, mode) {
  const qr = qrcode(0, 'M');
  qr.addData(text, mode);
  qr.make();
  const modules = qr.getModuleCount(), quiet = 4, cell = 5;
  canvas.width = canvas.height = (modules + 2 * quiet) * cell;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'white';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.translate(quiet * cell, quiet * cell);
  qr.renderTo2dContext(ctx, cell);
}

export async function showGuestQr(pack, signal, issueTime) {
  const owner = sessionOwner();
  const deadline = issueTime + 5 * 60_000;
  const canvas = h('canvas', { role: 'img', style: 'align-self:center;width:min(100%,50vh,420px);height:auto;image-rendering:pixelated' });
  const details = h('p', {}, `${pack.work_date} · ${pack.crew} · 배차 ${pack.assign_ver}차 · 수거 ${pack.count}곳 · 5분간 표시`);
  const close = () => canvas.closest('.dlg-back')?._close(null);
  let expired = false, timer;
  const expire = () => { expired = true; canvas.width = canvas.height = 0; close(); };
  const checkTime = () => { if (Date.now() >= deadline) expire(); };
  const expiryTimer = setTimeout(expire, Math.max(0, deadline - Date.now()));
  signal.addEventListener('abort', close);
  document.addEventListener('visibilitychange', checkTime);
  window.addEventListener('focus', checkTime);
  window.addEventListener('pageshow', checkTime);
  try {
    let keyStage = true;
    while (!signal.aborted) {
      checkTime();
      if (expired) break;
      if (keyStage) {
        canvas.setAttribute('aria-label', '열쇠 QR');
        draw(canvas, pack.key_qr, 'Byte');
        keyStage = false;
        if (!await modal({ title: '미등록폰 조회용 QR', owner, ok: '목록 QR 표시', cancel: '닫기', body: [
          h('h3', { class: 'guest-qr-stage' }, '1단계 · 열쇠 QR'),
          h('p', {}, '휴대폰으로 열쇠 QR을 읽으세요. 휴대폰에 ‘열쇠 읽음’이 뜨면 이 창에서 [목록 QR 표시]를 누르세요.'),
          canvas, details,
          h('p', { class: 'small muted' }, '이 PC 화면에서 담당 수거원에게 직접 보여 주세요. 두 QR을 함께 가진 사람은 배출 주소를 볼 수 있습니다.'),
        ] })) break;
        continue;
      }

      let index = 0;
      canvas.setAttribute('aria-label', '목록 QR');
      const position = h('span', { class: 'chip', 'aria-label': '현재 QR 번호' });
      const display = () => {
        draw(canvas, pack.frames[index], 'Alphanumeric');
        position.textContent = `${index + 1} / ${pack.frames.length}`;
      };
      const play = btn('자동 재생', () => {
        if (timer) pause();
        else {
          play.textContent = '일시 정지';
          timer = setInterval(() => step(1), 1000);
        }
      });
      function pause() {
        clearInterval(timer); timer = null; play.textContent = '자동 재생';
      }
      function step(delta) {
        checkTime();
        if (expired) return;
        index = (index + delta + pack.frames.length) % pack.frames.length;
        display();
      }
      display();
      const multiple = pack.frames.length > 1;
      const done = await modal({ title: '미등록폰 조회용 QR', owner, ok: '열쇠 QR 다시 보기', cancel: '닫기', body: [
        h('h3', { class: 'guest-qr-stage list' }, '2단계 · 목록 QR'),
        h('p', {}, multiple ? '휴대폰 카메라를 목록 QR에 비춘 채 [자동 재생]을 누르세요. 빠진 장은 이전·다음 QR로 다시 볼 수 있습니다.' : '휴대폰으로 아래 목록 QR을 읽으세요. 휴대폰에 수거 목록이 열립니다.'),
        canvas, h('div', { class: 'row', style: 'justify-content:center' }, position,
          multiple ? [btn('이전 QR', () => { pause(); step(-1); }), btn('다음 QR', () => { pause(); step(1); }), play] : null), details,
      ] });
      pause();
      if (!done) break;
      keyStage = true;
    }
    if (expired) throw new Error('QR 표시 시간이 끝났습니다. 다시 보여 주려면 [미등록폰 조회용 QR]을 누르세요.');
  } finally {
    clearTimeout(expiryTimer);
    clearInterval(timer);
    canvas.width = canvas.height = 0;
    pack.key_qr = '';
    pack.frames.length = 0;
    signal.removeEventListener('abort', close);
    document.removeEventListener('visibilitychange', checkTime);
    window.removeEventListener('focus', checkTime);
    window.removeEventListener('pageshow', checkTime);
  }
}
