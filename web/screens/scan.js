// 휴대폰 화면: 목록 받기(QR)·수신 코드
import * as B from '../backup.js';
import * as F from '../field.js';
import * as P from '../proto.js';
import * as S from '../store.js';
import { BUILTIN_FEES } from '../fees-data.js';
import { COLLECT_TTL_MS, btn, commit, errMsg, flowSteps, footBar, go, h, mdOf, render, st, startCamera, stopCamera, toast, todayKST, ui, unsent } from '../core.js';
import { purgeReleased } from './export.js';

async function loadCollector() {
  const saved = (await S.getRaw('collect')) || {};
  for (const [k, b] of Object.entries(saved)) if (Date.now() - b.firstSeen > COLLECT_TTL_MS) delete saved[k];
  return new P.Collector(saved);
}

// 지난 날짜 목록의 결과가 아직 PC에 반영되지 않았으면, QR을 다 읽고 나서 거절하지 말고 카메라를 켜기 전에 먼저 알린다.
function prevDayOwed() {
  const dl = st.daylist;
  if (!dl || dl.work_date >= todayKST()) return null;
  const u = unsent();
  if (!u.length) return null;
  const sent = new Set();
  for (const x of st.exports || []) if (!x.confirmed) for (const e of u) if (e.seq >= x.from && e.seq <= x.to) sent.add(e.seq);
  return { date: dl.work_date, count: u.length, waiting: sent.size, notSent: u.length - sent.size };
}
function screenScanOwed(o) {
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '오늘 목록 받기')),
    h('section', { class: 'card notice owed-card', role: 'alert' },
      h('h2', {}, `${mdOf(o.date)} 결과가 아직 PC에 반영되지 않았습니다`),
      h('p', {}, o.notSent ? `보내지 않은 기록 ${o.notSent}건` + (o.waiting ? ` · 보냈지만 결과 반영 코드를 받지 않은 기록 ${o.waiting}건` : '')
        : `보냈지만 결과 반영 코드를 받지 않은 기록 ${o.waiting}건`),
      h('p', { class: 'small' }, o.notSent ? '먼저 [결과 보내기]로 PC에 보내 주세요. 새 날짜 목록은 이전 결과가 반영된 뒤에 받습니다.'
        : 'PC 담당자에게 결과 반영 코드(D-)를 받아 [결과 보내기]에 넣거나, PC가 결과를 가져온 뒤 만든 새 목록을 받으면 저절로 확인됩니다.'),
      btn(o.notSent ? '결과 보내기' : '결과 반영 코드 넣기', () => go('#/export'), 'primary huge'),
      btn('PC가 이미 가져왔음 — 목록 QR 읽기', () => { ui.scanAck = true; render(); }, 'big'),
      h('p', { class: 'small' }, 'PC가 결과를 가져왔다면 새 목록에 반영 정보가 함께 와서 코드 없이 확인됩니다. 아직이면 목록을 다 읽은 뒤에도 받지 않습니다.')));
}
async function screenScan() {
  const owed = prevDayOwed();
  if (owed && !ui.scanAck) return screenScanOwed(owed);
  const pc = await S.getRaw('pc'), dev = await S.getRaw('device');
  const col = await loadCollector();
  let current = Object.keys(col.bundles).sort((a, b) => col.bundles[b].firstSeen - col.bundles[a].firstSeen)[0];
  const video = h('video', { playsinline: true, muted: true, class: 'cam' });
  const bar = h('div', { class: 'progress track', role: 'progressbar', 'aria-label': '받은 조각' }, h('div', { class: 'fill' }));
  const count = h('p', { class: 'count' }, '목록 QR을 비추세요');
  const miss = h('p', { class: 'small' });
  const msg = h('p', { class: 'msg' });
  const show = () => {
    if (!current) return;
    const b = col.bundles[current], got = Object.keys(b.parts).length;
    bar.firstChild.style.width = `${(100 * got) / b.total}%`;
    bar.setAttribute('aria-label', `받은 조각 ${got}/${b.total}`);
    count.textContent = `조각 ${got} / ${b.total}`;
    const m = col.missing(current);
    miss.textContent = m.length && m.length <= 12 ? '남은 번호: ' + m.map((i) => i + 1).join(', ') : '';
  };
  show();
  let finishing = false, reads = 0;
  const finish = async () => {
    finishing = true; stopCamera(); msg.textContent = '확인 중…';
    try {
      const envBytes = col.assemble(current);
      const opened = await P.openEnvelope(envBytes, await P.importEcdsaPub(pc.pcSignPub), dev.kex.privateKey, dev.id);
      if (opened.payload.schema === 'dg.qrtest/1') {
        // 실측 모드: 가상 목록이므로 저장하지 않고 시간·장수·인식 횟수와 복원 확인 코드만 보여 준다
        const b = col.bundles[current];
        const exp = P.expandDaylist({ ...opened.payload, schema: 'dg.daylist/2' }, BUILTIN_FEES);
        ui.qrtest = { secs: ((Date.now() - b.firstSeen) / 1000).toFixed(1), frames: b.total, reads: reads, count: exp.stops.length, code: opened.receipt.slice(0, 4), params: opened.payload.params };
        col.drop(current); await S.putRaw('collect', col.bundles);
        go('#/qrtest-result');
        return;
      }
      const pl = P.expandDaylist(opened.payload, BUILTIN_FEES);
      if (pl.schema !== 'dg.daylist/1') throw Object.assign(new Error('목록 형식 아님'), { code: 'BAD_VERSION' });
      if (!(pl.fee_table && (pl.fee_table.version_id === BUILTIN_FEES.version_id || pl.fee_table.base_version === BUILTIN_FEES.version_id || pl.fee_table.rows))) throw Object.assign(new Error(''), { code: 'FEE_VERSION_MISSING' });
      const hash = P.toHex(await P.sha256(envBytes));
      const old = st.daylist;
      // v0.13 결과 반영 정보(rel): PC가 이미 가져온 기록·사진을 코드 입력 없이 반영됨으로. 목록을 받지 않게 되더라도(지난 날짜 등)
      // 서명된 PC 정보이므로 먼저 적용한다. rel이 말한 번호를 넘는 기록은 그대로 미전송으로 남는다.
      const rel = P.normRel(pl.rel);
      delete pl.rel;
      const relDone = rel ? F.applyRelease(st, rel) : null;
      if (relDone) {
        const purged = purgeReleased();
        await commit();
        ui.relDone = { ...relDone, purged };
      } else ui.relDone = null;
      if (hash === st.lastBundleHash) { msg.textContent = '이미 받은 목록입니다. 바뀐 내용이 없습니다.'; }
      else {
        if (old && old.work_date === pl.work_date && pl.assign_ver < old.assign_ver) throw Object.assign(new Error(''), { code: 'QR_STALE_VERSION' });
        // 지난 날짜 목록은 받지 않는다(이미 지운 주민 정보가 다시 들어오지 않게)
        if (old && pl.work_date < old.work_date) throw Object.assign(new Error(''), { code: 'QR_OLD_DATE' });
        if (old && old.work_date !== pl.work_date && unsent().length) throw Object.assign(new Error(''), { code: 'UNSENT_PREVIOUS' });
        // 새 갱신본: 이름·주소·품목·순서만 바꾸고, 현장 기록(이벤트)은 그대로 둔다.
        let diff = null;
        if (old && old.work_date === pl.work_date) {
          // 같은 날 갱신본: 이 휴대폰이 이미 기록한 지점을 PC가 재방문처럼 돌려보낸 부분은 이 휴대폰 기록 기준으로(기록이 낡은 것으로 보이지 않게)
          F.carryOver(old, pl, st.events);
          diff = F.diffDaylist(old, pl);
          const ids = new Set(pl.stops.map((s) => s.request_id));
          const withEvents = new Set(st.events.map((e) => e.request_id));
          for (const s of old.stops) if (!ids.has(s.request_id) && withEvents.has(s.request_id)) pl.stops.push({ ...s, removed: true });
          // 이미 개인정보를 지운 곳은 다시 받아도 지운 채로 둔다(결과 반영 확인 뒤에는 휴대폰에 다시 두지 않음)
          for (const s of pl.stops) { const o = old.stops.find((x) => x.request_id === s.request_id); if (o && o.pii_purged && !s.pii_purged) F.purgeStopPII(s, o.pii_purged); }
          // 바뀐 표시는 그 지점을 열어 볼 때까지 남긴다(이전 표시 중 아직 안 본 것도 유지)
          const keep = Object.fromEntries(Object.entries(st.listChanges || {}).filter(([id]) => ids.has(id)));
          for (const [id, f] of Object.entries(diff.changes)) keep[id] = [...new Set([...(keep[id] || []), ...f])];
          st.listChanges = keep;
        } else {
          // 새 날짜 목록: 이전 목록(결과 반영이 확인된 것)의 이름·연락처·주소는 이미 이 목록으로 바뀌어 남지 않는다.
          // 이전 날짜 기록에 남은 메모 글·좌표도 지운다(수량·결과 기록은 사진 회수 때까지 남김).
          for (const e of st.events) if (e.seq <= st.releasedSeq && e.request_id) F.purgeEventPII(e);
          st.listChanges = {};
          // 재방문 건: 전날 기록(도착·연락·결과·사진·금액 고지)이 오늘 방문 상태로 섞이지 않게, 이 목록을 받은 시점 이후 기록만 본다
          for (const s of pl.stops) s.since_seq = st.seq;
        }
        st.daylist = pl; st.lastBundleHash = hash;
        // 현장 접수: PC 목록에 들어왔으면(같은 접수 ID) PC 것을 쓰고, PC 반영이 확인된 다른 날 건은 이 휴대폰에서 내린다
        const inList = new Set(pl.stops.map((s) => s.request_id));
        const sentIntake = (s) => st.events.some((e) => e.kind === 'FIELD_INTAKE' && e.request_id === s.request_id && e.seq <= st.releasedSeq);
        const keepFs = (st.fieldStops || []).filter((s) => !inList.has(s.request_id) && !(sentIntake(s) && s.planned_date !== pl.work_date));
        const dropped = new Set((st.fieldStops || []).filter((s) => !keepFs.includes(s)).map((s) => s.request_id));
        // 내린 현장 접수의 보낸 기록(이름·연락처·주소)은 바로 지운다(PC가 원본을 가짐)
        for (const e of st.events) if (dropped.has(e.request_id) && e.seq <= st.releasedSeq) F.purgeEventPII(e);
        st.fieldStops = keepFs;
        // 이 휴대폰에서 바꾼 방문 순서는 새 목록(새 날짜·갱신본)에는 쓰지 않는다
        if (st.localOrder && (st.localOrder.work_date !== pl.work_date || st.localOrder.assign_ver !== pl.assign_ver)) st.localOrder = null;
        ui.diffSummary = diff && diff.summary ? diff.summary : '';
        await commit();
        if (ui.diffSummary) toast(ui.diffSummary);
      }
      col.drop(current); await S.putRaw('collect', col.bundles);
      const rc = opened.receipt;
      ui.receipt = { code: rc.slice(0, 4) + ' ' + rc.slice(4), count: pl.stops.filter((s) => !s.removed).length, ver: pl.assign_ver, diff: ui.diffSummary || '', rel: ui.relDone };
      ui.diffSummary = ''; ui.relDone = null;
      // 수신 코드는 PC 담당자가 받아 적을 때까지 첫 화면에 남긴다(다시 보기 단추). [PC에 알렸음]을 누르거나 다음 목록을 받으면 내린다.
      st.lastReceipt = { code: rc, at: new Date().toISOString(), work_date: pl.work_date, ver: pl.assign_ver, count: ui.receipt.count, diff: ui.receipt.diff, told: false };
      await commit();
      go('#/received');
    } catch (e) {
      msg.textContent = errMsg(e);
      if (['AUTH_FAIL', 'WRONG_DEVICE', 'QR_STALE_VERSION', 'BAD_VERSION'].includes(e.code)) { col.drop(current); await S.putRaw('collect', col.bundles); current = null; }
      finishing = false;
    }
  };
  setTimeout(() => startCamera(video, async (text) => {
    if (finishing) return;
    let r;
    try { r = col.add(text); } catch (e) { if (e.code === 'QR_NOT_OURS' || e.code === 'BAD_VERSION') msg.textContent = errMsg(e); return; }
    if (!current) current = r.key;
    if (r.key !== current) { msg.textContent = '다른 목록의 QR이 섞였습니다. 지금 받던 목록을 계속 비춰 주세요.'; return; }
    reads += 1;
    if (!r.dup) { msg.textContent = ''; S.putRaw('collect', col.bundles); if (navigator.vibrate) navigator.vibrate(20); }
    show();
    if (col.complete(current)) finish();
  }).catch((e) => { msg.textContent = errMsg(e); }), 0);
  return h('main', {},
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '오늘 목록 받기')),
    flowSteps(['QR 비추기', '조각 모으기', '수신 코드'], current ? 1 : 0),
    h('section', { class: 'card cam-card' }, video, bar, count, miss, msg,
      h('p', { class: 'small' }, '다 받기 전에는 저장하지 않습니다. 중간에 멈춰도 24시간 안에 이어서 받을 수 있습니다.')),
    current ? btn('받던 조각 버리고 처음부터', async () => { col.drop(current); await S.putRaw('collect', col.bundles); render(); }, 'ghost link') : null);
}

// 수신 코드 표시: 앞 글자 R(휴대폰 → PC)은 따로 보여 주고, 코드(.fp)는 PC에 그대로 넣는 8자만
const codeShow = (kind, code) => h('p', { class: 'code-show k-' + kind }, h('span', { class: 'code-kind', 'aria-label': F.CODE_KINDS[kind].label }, F.CODE_KINDS[kind].prefix + '-'), h('span', { class: 'fp' }, code));
function relNote(rel) {
  if (!rel || !(rel.events || rel.photos)) return null;
  return h('p', { class: 'notice rel-note', role: 'status' }, 'PC 반영 정보도 함께 받았습니다: ' + [rel.events ? `결과 ${rel.events}건 반영 확인` : '', rel.photos ? `사진 ${rel.photos}장 회수 확인` : '', rel.purged ? `끝난 ${rel.purged}곳 개인정보 정리` : ''].filter(Boolean).join(' · ') + ' (코드 입력 필요 없음)');
}
function screenReceived() {
  const r = ui.receipt || (st.lastReceipt && !st.lastReceipt.told ? { code: st.lastReceipt.code.slice(0, 4) + ' ' + st.lastReceipt.code.slice(4), count: st.lastReceipt.count, ver: st.lastReceipt.ver, diff: '', again: true } : null);
  if (!r) { go('#/home'); return h('main'); }
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, r.again ? btn('← 목록', () => go('#/home'), 'ghost') : null, h('h1', { class: 'bar-title' }, r.again ? '수신 코드 다시 보기' : '목록 받기 완료')),
    flowSteps(['QR 비추기', '조각 모으기', '수신 코드'], 2),
    h('section', { class: 'card center-card' },
      h('h2', {}, '목록을 받았습니다'),
      h('p', {}, `${r.count}건 · 목록 갱신본 ${r.ver}`),
      r.diff ? h('p', { class: 'notice diff-summary', role: 'status' }, `바뀐 내용: ${r.diff}`) : null,
      relNote(r.rel),
      h('p', {}, 'PC 담당자에게 아래 수신 코드를 알려 주세요. (휴대폰 → PC)'),
      codeShow('RECEIPT', r.code),
      h('p', { class: 'small' }, 'PC가 받아 적을 때까지 첫 화면에 [수신 코드 다시 보기]로 남아 있습니다.')),
    footBar(btn('오늘 순서 보기', () => { ui.receipt = null; go('#/home'); }, 'primary huge')));
}

function screenQrTestResult() {
  const r = ui.qrtest;
  if (!r) { go('#/home'); return h('main'); }
  return h('main', { class: 'center' },
    h('h1', {}, 'QR 실측 결과'),
    h('p', {}, `가상 ${r.count}건 · 조각 ${r.params.chunk}B · 오류 복원 ${r.params.ec}`),
    h('section', { class: 'card' }, h('dl', {},
      h('dt', {}, '걸린 시간'), h('dd', { class: 'fp' }, `${r.secs}초`),
      h('dt', {}, 'QR 장수'), h('dd', {}, `${r.frames}장`),
      h('dt', {}, '인식 횟수'), h('dd', {}, `${r.reads}회 (중복 포함)`),
      h('dt', {}, '확인 코드'), h('dd', { class: 'fp' }, r.code))),
    h('p', { class: 'small' }, 'PC [QR 실측] 표에 이 값을 입력하세요. 실측 자료는 이 휴대폰에 저장하지 않았습니다.'),
    btn('처음으로', () => { ui.qrtest = null; go('#/home'); }, 'primary big'));
}

// ------------------------------------------------------------ 접수 상세

export {
  codeShow, loadCollector, prevDayOwed, relNote, screenQrTestResult, screenReceived, screenScan, screenScanOwed,
};
