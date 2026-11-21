// PC 화면: 목록 전송
import { api, boot, btn, chip, go, h, pagehead, post, toast, wrap } from '../core.js';

async function screenSend(batchArg) {
  const s = (screenSend.s ||= { date: boot.today });
  const date = h('input', { type: 'date', value: s.date });
  const selected = batchArg ? new Set(batchArg.split(',')) : null;
  const body = h('div');
  const includeOther = h('input', { type: 'checkbox' });
  const filter = h('label', { hidden: true }, includeOther, ' 배차 없는 팀·이전 목록 보기');
  let all = [], generation = 0;
  function render() {
    const lastSent = new Map();
    for (const batch of all) {
      if (batch.bundles.length && !lastSent.has(batch.crew_id)) lastSent.set(batch.crew_id, batch);
    }
    // 이전 QR이 휴대폰에 남아 있으면 배차가 0곳이어도 새 목록으로 교체해야 한다.
    function needsCard(batch) {
      if (batch.crew_archived || batch.status !== 'ACTIVE') return false;
      if (batch.stops.length || batch.final_unload) return true;
      const sent = lastSent.get(batch.crew_id);
      const bundle = sent?.bundles[0];
      return !!bundle && (!bundle.receipt_confirmed_at || bundle.sent_stops !== false || !!sent.final_unload);
    }
    const current = all.filter(needsCard);
    filter.hidden = !!selected || current.length === all.length;
    const batches = selected ? all.filter((b) => b.status === 'ACTIVE' && selected.has(b.id))
      : includeOther.checked ? all : current;
    body.replaceChildren(...(batches.length ? batches.map((b) => {
      const last = b.bundles[0];
      const canSend = b.status === 'ACTIVE' && !b.crew_archived;
      const code = h('input', { placeholder: '휴대폰 화면의 수신 코드 (R-XXXX XXXX)', 'aria-label': '수신 코드', style: 'width:260px' });
      const msg = h('span', { class: 'msg' });
      return h('div', { class: 'card' + (b.status !== 'ACTIVE' ? ' muted' : '') },
        h('div', { class: 'row', style: 'justify-content:space-between' },
          h('h2', {}, `${b.crew_name} · ${b.stops.length}곳 `, b.assign_ver > 1 ? h('span', { class: 'muted' }, `${b.assign_ver}차 목록 `) : null, b.crew_archived ? chip('보관된 팀') : b.status === 'ACTIVE' ? chip('현재', 'ok') : chip('이전 목록')),
          h('div', { class: 'row' }, b.status === 'ACTIVE' ? btn('배차표 인쇄', () => window.open('/print/batch/' + b.id.slice(0, 8), 'dg-print'), 'small') : null,
          canSend ? btn('QR 표시', wrap(async () => {
            const r = await post('bundles', { batch_id: b.id });
            window.open('/bundle/' + r.bundle_id.slice(0, 8), 'dg-qr', 'width=760,height=900');
            toast(`QR ${r.frames}장 (${r.count}건) 표시`); load();
          }, msg), 'primary', { disabled: !b.device_id }) : null)),
        !b.crew_archived && !b.device_id ? h('div', { class: 'notice' }, '이 팀에 휴대폰이 연결되어 있지 않아 QR을 보낼 수 없습니다. ', boot.me.role === 'ADMIN' ? h('a', { href: '#/settings/crews' }, '설정에서 연결하기 →') : '운영 관리자에게 연결을 요청하세요.') : null,
        b.resend_needed && canSend ? h('div', { class: 'notice' }, '전송 뒤에 수정된 접수가 있습니다. QR을 다시 표시해 새 목록을 전달하세요. 급한 변경은 수거팀에 전화로 먼저 알리세요.') : null,
        b.final_unload ? h('p', { class: 'notice info small' }, [b.final_unload.eta, b.final_unload.label, '최종 하차 후 차고지 복귀'].filter(Boolean).join(' · ')) : null,
        b.status === 'ACTIVE' && !b.stops.length && !b.final_unload && needsCard(b)
          ? h('p', { class: 'notice info small' }, last?.sent_stops === false
            ? '빈 목록 QR을 표시했습니다. 휴대폰의 수신 코드를 확인하세요.'
            : '배차가 0곳입니다. 휴대폰에 이전 배차가 남지 않도록 빈 목록을 전달하세요.') : null,
        h('div', { class: 'stopchain', 'aria-label': '방문 순서' }, ...b.stops.map((x) => h('span', { class: 'node' }, h('b', {}, x.seq), x.name))),
        last ? h('div', { class: 'row' }, last.receipt_confirmed_at ? chip('휴대폰 수신 확인됨', 'ok') : [chip('QR 표시함 · 수신 미확인', 'warn'), h('span', { class: 'muted small' }, '수신 코드(R-):')],
          last.receipt_confirmed_at ? null : code, last.receipt_confirmed_at ? null : btn('수신 확인', wrap(async () => { await post(`bundles/${last.id}/receipt`, { code: code.value }); toast('수신이 확인되었습니다'); load(); }, msg)), msg)
          : h('p', { class: 'muted' }, '아직 전송하지 않았습니다.'));
    }) : [h('p', { class: 'muted' }, selected ? '전송할 변경 갱신본을 찾을 수 없습니다. 현재 배정을 확인하세요.' : all.length ? '이 날짜에 전송할 배차가 없습니다.' : '이 날짜의 배정이 없습니다. [배정·노선]에서 확정하세요.')]));
  }
  const load = wrap(async () => {
    const request = ++generation;
    s.date = date.value;
    body.replaceChildren();
    filter.hidden = true;
    const result = await api('batches?date=' + encodeURIComponent(date.value) + '&for_send=1');
    if (request !== generation) return;
    all = result;
    render();
  });
  includeOther.addEventListener('change', render);
  const showAll = () => go('#/send?date=' + encodeURIComponent(date.value));
  date.addEventListener('change', selected ? showAll : load); load();
  date.setAttribute('aria-label', '수거일'); date.id = 'send-date';
  return h('div', {}, pagehead('목록 전송 (QR)'),
    selected ? h('div', { class: 'notice info row' }, '변경 팀의 최신 목록', btn('전체 팀 보기', showAll, 'small')) : null,
    h('div', { class: 'card searchbox', role: 'search', 'aria-label': '조회조건' }, h('div', { class: 'cond' }, h('label', { class: 'cond-l', for: 'send-date' }, '수거일'), h('div', { class: 'cond-v' }, date),
      h('span', { class: 'cond-l' }, '안내'), h('div', { class: 'cond-v wide2' }, h('span', { class: 'muted small' }, 'QR 화면 표시와 휴대폰 수신은 다릅니다. 휴대폰이 보여 주는 수신 코드(R-)를 입력해야 "수신 확인"이 됩니다.'))), filter), body);
}

// ------------------------------------------------------------ 결과·사진 가져오기, 충돌함

export {
  screenSend,
};
