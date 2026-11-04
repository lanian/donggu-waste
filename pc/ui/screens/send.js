// PC 화면: 목록 전송
import { api, boot, btn, chip, h, pagehead, post, searchbox, toast, wrap } from '../core.js';

async function screenSend() {
  const s = (screenSend.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date });
  const body = h('div');
  const load = wrap(async () => {
    s.date = date.value;
    const batches = await api('batches?date=' + s.date);
    body.replaceChildren(...(batches.length ? batches.map((b) => {
      const last = b.bundles[0];
      const code = h('input', { placeholder: '휴대폰 화면의 수신 코드 (R-XXXX XXXX)', 'aria-label': '수신 코드', style: 'width:260px' });
      const msg = h('span', { class: 'msg' });
      return h('div', { class: 'card' + (b.status !== 'ACTIVE' ? ' muted' : '') },
        h('div', { class: 'row', style: 'justify-content:space-between' },
          h('h2', {}, `${b.crew_name} · ${b.stops.length}곳 `, b.assign_ver > 1 ? h('span', { class: 'muted' }, `${b.assign_ver}차 목록 `) : null, b.status === 'ACTIVE' ? chip('현재', 'ok') : chip('이전 목록')),
          h('div', { class: 'row' }, b.status === 'ACTIVE' ? btn('배차표 인쇄', () => window.open('/print/batch/' + b.id.slice(0, 8), 'dg-print'), 'small') : null,
          b.status === 'ACTIVE' ? btn('QR 표시', wrap(async () => {
            const r = await post('bundles', { batch_id: b.id });
            window.open('/bundle/' + r.bundle_id.slice(0, 8), 'dg-qr', 'width=760,height=900');
            toast(`QR ${r.frames}장 (${r.count}건) 표시`); load();
          }, msg), 'primary', { disabled: !b.device_id }) : null)),
        !b.device_id ? h('div', { class: 'notice' }, '이 팀에 휴대폰이 연결되어 있지 않아 QR을 보낼 수 없습니다. ', boot.me.role === 'ADMIN' ? h('a', { href: '#/settings/crews' }, '설정에서 연결하기 →') : '운영 관리자에게 연결을 요청하세요.') : null,
        b.resend_needed && b.status === 'ACTIVE' ? h('div', { class: 'notice' }, '전송 뒤에 수정된 접수가 있습니다. QR을 다시 표시해 새 목록을 전달하세요. 급한 변경은 수거팀에 전화로 먼저 알리세요.') : null,
        h('div', { class: 'stopchain', 'aria-label': '방문 순서' }, ...b.stops.map((x) => h('span', { class: 'node' }, h('b', {}, x.seq), x.name))),
        last ? h('div', { class: 'row' }, last.receipt_confirmed_at ? chip('휴대폰 수신 확인됨', 'ok') : [chip('QR 표시함 · 수신 미확인', 'warn'), h('span', { class: 'muted small' }, '수신 코드(R-):')],
          last.receipt_confirmed_at ? null : code, last.receipt_confirmed_at ? null : btn('수신 확인', wrap(async () => { await post(`bundles/${last.id}/receipt`, { code: code.value }); toast('수신이 확인되었습니다'); load(); }, msg)), msg)
          : h('p', { class: 'muted' }, '아직 전송하지 않았습니다.'));
    }) : [h('p', { class: 'muted' }, '이 날짜의 배정이 없습니다. [배정·노선]에서 확정하세요.')]));
  });
  date.addEventListener('change', load); load();
  date.setAttribute('aria-label', '수거일'); date.id = 'send-date';
  return h('div', {}, pagehead('목록 전송 (QR)'),
    h('div', { class: 'card searchbox', role: 'search', 'aria-label': '조회조건' }, h('div', { class: 'cond' }, h('label', { class: 'cond-l', for: 'send-date' }, '수거일'), h('div', { class: 'cond-v' }, date),
      h('span', { class: 'cond-l' }, '안내'), h('div', { class: 'cond-v wide2' }, h('span', { class: 'muted small' }, 'QR 화면 표시와 휴대폰 수신은 다릅니다. 휴대폰이 보여 주는 수신 코드(R-)를 입력해야 "수신 확인"이 됩니다.')))), body);
}

// ------------------------------------------------------------ 결과·사진 가져오기, 충돌함

export {
  screenSend,
};
