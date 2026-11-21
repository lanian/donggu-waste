import { PAY, STATUS, boot, h, statusChip } from '../core.js';

const paymentTasks = {
  AMOUNT_PENDING: '품목 단가 확인', PAID_AMOUNT_PENDING: '품목 단가와 납부액 대조',
  UNPAID: '수납 기록 확인', UNDERPAID: '부족한 납부액 확인',
  OVERPAID_REVIEW: '과납 금액 확인', REFUND_NEEDED: '환불 처리 확인',
  OVERREFUNDED_REVIEW: '환불 금액 확인',
};

function nextStep(request) {
  const p = request.progress;
  if (p.revisit_confirmed && ['RECEIVED', 'PARTIAL', 'NOT_COLLECTED', 'ON_HOLD'].includes(p.status)) {
    if (boot?.today && p.planned_date < boot.today) return '지난 재방문 결과와 일정 확인';
    if (p.last_outcome && p.planned_date === recordedDay(request.state?.outcome_ts)) return '재방문 결과와 다음 일정 확인';
    if (p.has_location === false) return '재방문 배출 위치 확인';
    return p.assigned_to ? '재방문 수거 결과 확인' : '재방문 수거팀 배정';
  }
  switch (p.status) {
    case 'RECEIVED':
      if (!p.planned_date) return '수거 예정일 지정';
      if (p.has_location === false) return '배출 위치 확인';
      return '수거팀 배정';
    case 'ASSIGNED': return p.last_outcome || p.revisit_confirmed ? '재방문 수거 결과 확인' : '수거 결과 확인';
    case 'IN_PROGRESS': return '현장 수거 결과 확인';
    case 'PARTIAL': return '남은 품목과 재방문 일정 확인';
    case 'NOT_COLLECTED': return '미수거 사유와 재방문 일정 확인';
    case 'ON_HOLD': return '보류 사유와 재개 일정 확인';
    case 'DONE':
    case 'CANCELLED': return paymentTasks[p.pay_status] || '추가 수거 없음';
    default: return '접수 상세 확인';
  }
}

function previousResult(p) {
  return p.last_outcome && p.last_outcome !== p.status ? STATUS[p.last_outcome] : null;
}

export function progressCell(request) {
  const p = request.progress;
  const prior = p && previousResult(p);
  return h('div', { class: 'request-progress-cell' },
    statusChip(request.status), p ? h('span', { class: 'progress-next' }, nextStep(request)) : null,
    prior ? h('small', {}, '직전 수거 결과 · ' + prior) : null);
}

function quantities(groups) {
  return groups.map(({ qty, unit_label }) => `${qty}${unit_label}`).join(' · ') || '0';
}

const fact = (title, value) => h('div', {}, h('dt', {}, title), h('dd', {}, value));
const koreanDay = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul' });
function recordedDay(timestamp) {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  return Number.isFinite(date.getTime()) ? koreanDay.format(date) : '';
}

export function progressPanel(request) {
  const p = request.progress;
  const header = h('h2', {}, '접수 진행 상황', statusChip(request.status));
  const card = h('section', { class: 'card request-progress', 'aria-label': '접수 진행 상황' }, header);
  if (!p) {
    card.append(h('p', { class: 'muted' }, '진행 세부정보를 확인할 수 없습니다.'));
    return card;
  }
  const prior = previousResult(p);
  const day = recordedDay(request.state?.outcome_ts);
  const receiptDay = recordedDay(request.received_at);
  const arrival = recordedDay(request.state?.field?.arrive?.at);
  const last = STATUS[p.last_outcome];
  const history = [
    receiptDay && `접수 ${receiptDay}`,
    arrival && `현장 도착 ${arrival}`,
    !arrival && !last && p.has_visit && '현장 품목 기록 있음',
    last && `${prior ? '직전 수거 결과' : '수거 결과'} · ${last}${day ? ' ' + day : ''}`,
  ].filter(Boolean);
  const counts = p.status === 'CANCELLED' ? `누적 ${quantities(p.collected)}`
    : `누적 ${quantities(p.collected)} / 남음 ${quantities(p.remaining)}`;
  const scheduledRevisit = p.revisit_confirmed && !['CANCELLED', 'DONE'].includes(p.status);
  card.append(
    h('p', { class: 'request-next-action' }, h('span', {}, '다음 할 일'), h('strong', {}, nextStep(request))),
    h('dl', { class: 'request-progress-facts' },
      fact('수거 예정일', scheduledRevisit ? `${p.planned_date} · 재방문 약속 확정` : p.planned_date || '미정'),
      fact('배정 팀', p.assigned_to || '현재 배정 없음'),
      fact('수거 수량', counts),
      p.pay_status && PAY[p.pay_status] ? fact('수납 상태', PAY[p.pay_status]) : null));
  if (history.length) card.append(h('ul', { class: 'request-progress-records', 'aria-label': '수거 경과' },
    ...history.map((record) => h('li', {}, record))));
  return card;
}
