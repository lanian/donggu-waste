// 오늘 현황과 마감 점검에서 목록 수신과 접수 결과를 각각 표시한다.
import { chip, h, kst } from '../core.js';

const RECEIPT = {
  UNSENT: ['목록 미전송', 'warn'], UNCONFIRMED: ['수신 미확인', 'warn'], CONFIRMED: ['수신 확인', 'ok'],
};
const STAGES = {
  EMPTY: ['확인할 접수 없음', 'neutral'], WAITING: ['현장 결과 기다림', 'neutral'],
  PARTIAL: ['부분 회수', 'warn'], COMPLETE: ['회수 완료', 'ok'],
};

function recoveryTags(r) {
  const [receipt, receiptStyle] = RECEIPT[r.receipt_status];
  const [stage, stageStyle] = STAGES[r.stage];
  const label = !r.applied && r.pc ? (r.missing ? '일부 PC 처리' : 'PC 처리 완료') : stage;
  return [
    chip(r.receipt_confirmed_at ? `${receipt} · ${kst(r.receipt_confirmed_at)}` : receipt, receiptStyle),
    chip(label, stageStyle),
  ];
}

function recoveryCounts(r) {
  return h('div', { class: 'small' }, `현장 반영 ${r.applied}건 · 미회수 ${r.missing}건`,
    r.pc ? ` · PC 직접 수거 ${r.pc}건` : null,
    r.uncertain ? ` · 회차 확인 필요 ${r.uncertain}건` : null,
    h('div', { class: 'muted' }, `마지막 관련 반영 ${r.last_applied_at ? kst(r.last_applied_at) : '없음'}`));
}

export { recoveryCounts, recoveryTags };
