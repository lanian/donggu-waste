// 휴대폰 화면: 결과 QR 표시
import * as M from '../map.js';
import qrcode from '../vendor/qrcode.mjs';
import { btn, flowSteps, footBar, go, h, ui } from '../core.js';

function drawQr(canvas, text, mode = 'Alphanumeric') {
  const q = qrcode(0, 'M');
  q.addData(text, mode);
  q.make();
  const n = q.getModuleCount(), quiet = 4, size = Math.min(canvas.parentElement ? canvas.parentElement.clientWidth : 360, 520);
  const cell = Math.floor(size / (n + quiet * 2));
  canvas.width = canvas.height = cell * (n + quiet * 2);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#000';
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) ctx.fillRect((c + quiet) * cell, (r + quiet) * cell, cell, cell);
}
function screenResultQr() {
  const r = ui.resultQr;
  if (!r) { go('#/export'); return h('main'); }
  const canvas = h('canvas', { class: 'qrcanvas', 'aria-label': '결과 QR' });
  const count = h('p', { class: 'count' });
  let i = 0, fps = 3, timer = null;
  const show = () => { if (!canvas.isConnected) return clearTimeout(timer); drawQr(canvas, r.frames[i]); count.textContent = `${i + 1} / ${r.frames.length}`; i = (i + 1) % r.frames.length; timer = setTimeout(show, 1000 / fps); };
  setTimeout(show, 0);
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 결과 보내기', () => { clearTimeout(timer); ui.resultQr = null; go('#/export'); }, 'ghost'), h('h1', { class: 'bar-title' }, '결과 QR')),
    flowSteps(['결과 파일', '결과 반영 코드', '사진 파일', '사진 회수 코드', '정리'], 0),
    h('section', { class: 'card center-card' },
      h('p', {}, 'PC [결과 가져오기] → [웹캠으로 읽기]를 누르고 이 화면을 웹캠에 비추세요. 화면 밝기를 최대로 하면 잘 읽힙니다.'),
      h('div', { class: 'qrbox' }, canvas), count,
      h('p', { class: 'small' }, `${r.name} · 이름·연락처·주소는 들어 있지 않습니다. PC가 알려 주는 결과 반영 코드(D-)를 [결과 보내기]에 입력하세요.`)),
    footBar(h('div', { class: 'grid2' }, btn('느리게', () => { fps = Math.max(1, fps - 1); }, 'opt'), btn('빠르게', () => { fps = Math.min(8, fps + 1); }, 'opt'))));
}

// ------------------------------------------------------------ 설정

export {
  drawQr, screenResultQr,
};
