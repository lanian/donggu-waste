// PC 화면: 결과·사진 가져오기·충돌함·단독 휴대폰 옮기기
import { Collector } from '/web/proto.js';
import { CONTACT, NOTICE, NO_PHOTO, REASON, SMS_T, STATUS, STICKER, api, boot, btn, chip, fmtPhone, grid, h, kst, pagehead, post, promptDlg, statusChip, toast, wrap } from '../core.js';
import { loadJsQR } from '../widgets.js';
import { refreshBadges } from './home.js';

// 확인 코드 표시 (휴대폰과 같은 이름·앞 글자): 수신 코드 R- (휴대폰 → PC), 결과 반영 코드 D-, 사진 회수 코드 P- (PC → 휴대폰)
const CODE_NAME = { R: '수신 코드', D: '결과 반영 코드', P: '사진 회수 코드' };
const codeEl = (kind, code) => h('span', { class: 'code-wrap', title: CODE_NAME[kind] }, h('span', { class: 'code-kind' }, kind + '-'), h('span', { class: 'big' }, code ? `${code.slice(0, 4)} ${code.slice(4)}` : ''));
const IMP_LABEL = { applied: '반영', skipped_duplicate: '중복 건너뜀', conflict: '충돌함으로', review: '현장 요청 → 충돌함', field_intakes: '현장 접수 만듦', photos_expected: '사진 기록', recovered: '사진 회수',
  already: '이미 회수', hash_mismatch: '해시 불일치', unexpected: '결과보다 먼저 온 사진', purged: '파기 자료 제외', seq_reused: '순번 겹침(반영·확인만)', stored_other: '기타 기록 보관', requests: '접수', events: '기록', unloads_applied: '하차' };
const impSummary = (r) => Object.entries(r || {}).filter(([k, v]) => IMP_LABEL[k] && typeof v === 'number' && v).map(([k, v]) => `${IMP_LABEL[k]} ${v}`).join(' · ') || '변경 없음';
const IMP_KIND = { RESULT: '결과', PHOTO: '사진', SOLO: '단독 옮김', PC: 'PC 수거 입력' };
async function screenResults() {
  // 1) 파일 넣기: 결과(.dgr)·사진(.dgp)을 여러 개 한꺼번에(고르기·끌어 놓기). 결과를 먼저, 사진을 나중에 가져온다.
  const runBody = h('tbody');
  const runCard = h('div', { class: 'card', id: 'import-run', hidden: true }, h('h2', {}, '방금 가져온 파일'),
    h('div', { class: 'tablewrap' }, h('table', { class: 'grid imp-run' }, h('thead', {}, h('tr', {}, ...['파일', '종류', '팀·기기', '결과', '휴대폰에 알려 줄 코드'].map((t) => h('th', { scope: 'col' }, t)))), runBody)));
  let recent = [];
  const teamOf = (impId) => { const x = recent.find((y) => y.id === impId); return x ? `${x.crew || '(팀 없음)'} · ${x.device_label || (x.device_id || '').slice(0, 8)}` : ''; };
  const addRun = (name, kind, r, err) => {
    runCard.hidden = false;
    const isPhoto = kind === 'PHOTO';
    const code = r && r.release_code ? h('span', {}, h('small', { class: 'block muted' }, isPhoto ? '사진 회수 코드' : '결과 반영 코드'), codeEl(isPhoto ? 'P' : 'D', r.release_code))
      : r && isPhoto && !r.duplicate_file ? h('span', { class: 'err small' }, '해시가 맞지 않는 사진이 있어 코드를 만들지 않았습니다. 휴대폰에서 사진을 지우지 마세요.') : '';
    const tr = h('tr', { class: 'imp-row' + (err ? ' bad' : ''), 'data-import': r && r.import_id ? r.import_id : '' }, h('td', { class: 'mono' }, name), h('td', {}, isPhoto ? '사진' : '결과'),
      h('td', { class: 'team' }, r && r.import_id ? teamOf(r.import_id) : ''),
      h('td', {}, err ? h('span', { class: 'err' }, err) : [r.duplicate_file ? chip('이미 가져온 파일 (변경 없음)', 'neutral') : null, impSummary(r),
        r.seq_reused ? h('div', { class: 'notice warn small' }, `휴대폰 복원으로 순번이 겹친 기록 ${r.seq_reused}건 — 반영했고 확인만 하면 됩니다.`) : null]),
      h('td', {}, code));
    runBody.prepend(tr);
  };
  const fill = () => runBody.querySelectorAll('tr[data-import]').forEach((tr) => { const t = teamOf(tr.dataset.import); if (t) tr.querySelector('.team').textContent = t; });
  const importFiles = wrap(async (files) => {
    const list = [...files].filter((f) => /\.(dgr|dgp)$/i.test(f.name));
    const skip = [...files].length - list.length;
    if (skip) toast(`.dgr·.dgp가 아닌 파일 ${skip}개는 건너뛰었습니다`, true);
    list.sort((a, b) => (/\.dgp$/i.test(a.name) ? 1 : 0) - (/\.dgp$/i.test(b.name) ? 1 : 0));  // 결과 먼저
    for (const f of list) {
      const photo = /\.dgp$/i.test(f.name);
      try { addRun(f.name, photo ? 'PHOTO' : 'RESULT', await api(photo ? 'import/photos' : 'import/result', { method: 'POST', raw: new Uint8Array(await f.arrayBuffer()) })); }
      catch (e) { addRun(f.name, photo ? 'PHOTO' : 'RESULT', null, e.message); }
    }
    await loadRecent(); fill(); if (boot.me.role !== 'FIELD') loadConf(); refreshBadges(true);
  });
  const fileIn = h('input', { type: 'file', multiple: true, accept: '.dgr,.dgp', 'aria-label': '결과·사진 파일 (여러 개)', onchange: (e) => { const fs = [...e.target.files]; e.target.value = ''; importFiles(fs); } });
  const drop = h('div', { class: 'dropzone', tabindex: '-1' }, h('b', {}, '결과 파일(.dgr)·사진 파일(.dgp)을 여기에 끌어 놓으세요'), h('span', { class: 'muted small' }, '여러 팀·여러 개를 한꺼번에 넣어도 됩니다. 결과를 먼저, 사진을 나중에 가져옵니다.'),
    h('label', { class: 'btnlike' }, '파일 고르기', fileIn));
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('over'); importFiles(e.dataTransfer.files); });
  // 2) 최근 가져오기: 화면을 다시 열어도 코드를 다시 볼 수 있게, 휴대폰이 코드를 받았는지(직접 표시·다음 목록 수신)
  const recentBody = h('tbody');
  const loadRecent = wrap(async () => {
    recent = await api('imports/recent?limit=30');
    recentBody.replaceChildren(...(recent.length ? recent.map((x) => h('tr', { 'data-import': x.id }, h('td', { class: 'nowrap' }, kst(x.imported_at)), h('td', {}, IMP_KIND[x.kind] || x.kind, x.channel === 'QR' ? chip('웹캠 QR', 'info') : null),
      h('td', {}, x.crew || h('span', { class: 'muted' }, '(팀 없음)'), h('span', { class: 'muted small block' }, x.device_label || (x.device_id || '').slice(0, 8))),
      h('td', {}, impSummary(x.counts)),
      h('td', {}, x.release_code && x.kind !== 'SOLO' ? [h('small', { class: 'block muted' }, x.kind === 'PHOTO' ? '사진 회수 코드' : '결과 반영 코드'), codeEl(x.kind === 'PHOTO' ? 'P' : 'D', x.release_code)] : h('span', { class: 'muted' }, '—')),
      h('td', {}, x.phone_confirmed ? chip(x.phone_confirmed_by === 'MANUAL' ? '휴대폰 입력함' : '휴대폰 확인됨(다음 목록)', 'ok')
        : x.release_code && x.kind !== 'SOLO' ? [chip('휴대폰 입력 전', 'warn'), ' ', btn('휴대폰에 입력함', wrap(async () => { await post(`imports/${x.id}/confirm`); toast('휴대폰에 코드를 입력했다고 표시했습니다'); loadRecent(); }), 'small')] : '')))
      : [h('tr', {}, h('td', { colspan: 6, class: 'muted' }, '아직 가져온 파일이 없습니다.'))]));
  });
  const recentCard = h('div', { class: 'card', id: 'recent-imports' }, h('div', { class: 'panel-h' }, h('h2', {}, '최근 가져오기'), btn('새로 고침', loadRecent, 'small')),
    h('div', { class: 'tablewrap' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['가져온 시각', '종류', '팀·기기', '건수', '코드', '휴대폰'].map((t) => h('th', { scope: 'col' }, t)))), recentBody)),
    h('p', { class: 'muted small' }, '결과 반영 코드(D-)를 휴대폰에 넣으면 그 접수들의 이름·연락처·주소가 휴대폰에서 지워지고, 사진 회수 코드(P-)를 넣어야 휴대폰에서 사진을 지울 수 있습니다. 코드를 넣지 않아도 휴대폰이 다음 목록을 받으면 저절로 확인됩니다.'));
  // 3) 충돌함: 접수별로 묶고, 접수 하나·가져오기 하나의 기록을 한 사유로 [모두 반영/거부]
  const conf = h('div', { class: 'card', id: 'conflicts' });
  const REQ_KINDS = ['FIELD_INTAKE', 'CORRECTION', 'CANCEL_REQUEST'];
  let confTab = 'ALL';
  const bulk = (what, id, action, n, label) => wrap(async () => {
    const why = await promptDlg(`${label} ${n}건 ${action === 'APPLY' ? '모두 반영' : '모두 거부'}`, '판단 사유', { ok: action === 'APPLY' ? '모두 반영' : '모두 거부', danger: action === 'REJECT',
      lead: [h('p', {}, action === 'APPLY' ? '같은 사유로 기록마다 반영합니다. 반영할 수 없는 기록은 건너뛰고 알려 줍니다.' : '같은 사유로 모두 거부합니다. 원 기록은 남습니다.')] });
    if (!why) return;
    const r = await post('conflicts/bulk', { action, reason: why, [what]: id });
    toast(`${r.resolved}건 ${action === 'APPLY' ? '반영' : '거부'}했습니다`);
    (r.failed || []).forEach((f) => toast(`${EVENT_KIND[f.kind] || f.kind}: ${f.error}`, true));
    loadConf(); refreshBadges(true);
  });
  const loadConf = wrap(async () => {
    const all = await api('conflicts');
    const nReq = all.filter((c) => REQ_KINDS.includes(c.kind)).length;
    const list = all.filter((c) => confTab === 'ALL' || (confTab === 'REQ') === REQ_KINDS.includes(c.kind));
    const tabs = h('div', { class: 'quick conf-tabs' }, ...[['ALL', '전체', all.length], ['REQ', '현장 요청', nReq], ['CONFLICT', '충돌', all.length - nReq]].map(([k, t, n]) =>
      h('button', { type: 'button', class: 'qchip' + (confTab === k ? ' on' : ''), 'aria-pressed': confTab === k ? 'true' : 'false', onclick: () => { confTab = k; loadConf(); } }, t, h('b', {}, n))));
    // 가져오기별 묶음
    const byImp = new Map();
    list.forEach((c) => { if (c.import_id) byImp.set(c.import_id, (byImp.get(c.import_id) || 0) + 1); });
    const impSel = h('select', { 'aria-label': '가져오기 고르기' }, ...[...byImp].map(([id, n]) => { const x = recent.find((y) => y.id === id); return h('option', { value: id }, `${x ? `${kst(x.imported_at)} ${x.crew || x.device_label || ''}` : id.slice(0, 8)} · ${n}건`); }));
    const impBar = byImp.size ? h('div', { class: 'row imp-bulk' }, h('span', { class: 'muted small' }, '가져오기별:'), impSel,
      btn('이 가져오기 모두 반영', (e) => bulk('import_id', impSel.value, 'APPLY', byImp.get(impSel.value), '이 가져오기')(e), 'small'),
      btn('이 가져오기 모두 거부', (e) => bulk('import_id', impSel.value, 'REJECT', byImp.get(impSel.value), '이 가져오기')(e), 'small danger')) : null;
    // 접수별 묶음 (처음 나온 순서 유지)
    const groups = new Map();
    list.forEach((c) => { const k = c.request_id || '-'; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(c); });
    const rowsEl = [];
    for (const [rid, cs] of groups) {
      const c0 = cs[0];
      rowsEl.push(h('tr', { class: 'grp' }, h('td', { colspan: 6 }, h('div', { class: 'row' },
        rid !== '-' ? h('a', { href: `#/request/${rid}` }, c0.receipt_no || rid.slice(0, 8)) : h('span', { class: 'muted' }, '(접수 없음)'), ' ', c0.name || '', c0.request_status ? statusChip(c0.request_status) : null,
        h('span', { class: 'muted small' }, `기록 ${cs.length}건`), h('span', { class: 'grow' }),
        rid !== '-' && cs.length > 1 ? btn('이 접수 기록 모두 반영', (e) => bulk('request_id', rid, 'APPLY', cs.length, c0.receipt_no || '이 접수')(e), 'small') : null,
        rid !== '-' && cs.length > 1 ? btn('이 접수 기록 모두 거부', (e) => bulk('request_id', rid, 'REJECT', cs.length, c0.receipt_no || '이 접수')(e), 'small danger') : null))));
      cs.forEach((c) => {
        const why = h('input', { placeholder: '판단 사유', class: 'w-m', 'aria-label': '판단 사유' });
        rowsEl.push(h('tr', { class: 'ev' }, h('td', { class: 'muted small' }, c.import_id ? teamOf(c.import_id) || '' : ''),
          h('td', {}, EVENT_KIND[c.kind] || c.kind), h('td', {}, c.kind === 'CORRECTION' ? correctionBody(c) : eventBody(c.kind, c.body_json)), h('td', { class: 'nowrap' }, kst(c.device_ts)),
          h('td', {}, c.conflict_reason, c.warning ? h('div', {}, chip(c.warning, 'warn')) : null),
          h('td', {}, h('span', { class: 'row' }, why, btn('반영', wrap(async () => { await post('conflicts/' + c.event_id, { action: 'APPLY', reason: why.value }); toast('반영했습니다'); loadConf(); refreshBadges(true); }), 'small'),
            btn('거부', wrap(async () => { await post('conflicts/' + c.event_id, { action: 'REJECT', reason: why.value }); toast('거부로 기록했습니다'); loadConf(); refreshBadges(true); }), 'small danger')))));
      });
    }
    conf.replaceChildren(h('div', { class: 'panel-h' }, h('h2', {}, `충돌함 ${all.length}건`), h('span', { class: 'muted small' }, '사유를 보고 반영 또는 거부 (거부해도 원 기록은 남음)')), tabs, impBar,
      list.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'grid conflicts' }, h('thead', {}, h('tr', {}, ...['팀', '기록 종류', '기록 내용', '휴대폰 시각', '충돌 사유·경고', '처리'].map((t) => h('th', { scope: 'col' }, t)))), h('tbody', {}, ...rowsEl)))
        : h('p', { class: 'empty' }, confTab === 'REQ' ? '확인할 현장 요청이 없습니다.' : '확인할 충돌이 없습니다.'));
  });
  const field = boot.me.role === 'FIELD';  // 수거팀 계정은 충돌함을 보지 않는다(사무실 판단)
  loadRecent().then(() => { fill(); if (!field) loadConf(); });
  return h('div', {}, pagehead('결과 가져오기', h('a', { href: '#conflicts', class: 'small', onclick: (e) => { e.preventDefault(); conf.scrollIntoView(); } }, '충돌함으로 ↓')),
    h('div', { class: 'card' }, h('h2', {}, '결과·사진 파일 넣기'), drop,
      h('p', { class: 'muted small' }, '결과 파일은 업무 메일 첨부, 사진 파일은 승인된 보안 매체로 받습니다. 같은 파일을 여러 번 넣어도 한 번만 반영됩니다.')),
    runCard,
    recentCard,
    webcamCard((r) => { addRun('휴대폰 화면 QR (웹캠)', 'RESULT', r); loadRecent().then(fill); loadConf(); }),
    soloCard(),
    field ? null : conf, h('p', { class: 'muted' }, '오래된 배정·다른 팀·취소된 접수의 결과는 자동 반영하지 않고 충돌함에 둡니다.'));
}

// 휴대폰 기록 종류·내용을 사람이 읽는 표로 (충돌함)
const EVENT_KIND = { CONTACT: '연락 결과', FIELD_ITEM_ADD: '현장 품목 추가', ITEM_RESULT: '품목 결과', ATTEMPT: '수거 결과', LOCATION_FIX: '위치 보정', FIELD_MEMO: '현장 메모',
  PHOTO_META: '사진', REVISIT_APPT: '재방문 약속', DIAL_INTENT: '전화 걸기', UNLOAD: '하차', ARRIVE: '도착', STICKER_CHECK: '스티커 확인', FEE_NOTICE: '추가 금액 고지', SMS_INTENT: '문자 앱 열기', NAV_INTENT: '길안내 앱 열기',
  FIELD_INTAKE: '현장 접수', CORRECTION: '정정 요청', CANCEL_REQUEST: '취소 요청' };
const BODY_KEY = { result: '결과', outcome: '결과', line_id: '품목 행', qty_confirmed: '현장 수량', qty_collected: '실은 수량', reason: '사유', name: '품목', spec: '규격', qty: '수량', unit_label: '단위',
  date: '날짜', start: '시작', end: '끝', phase: '단계', text: '메모', worker: '작업자', response: '응답', extra: '추가 금액', total: '현장 금액', received: '접수 금액', template: '문구', app: '앱',
  number: '번호', decision: '판단', acc: '정확도(m)', lat: '위도', lon: '경도', volume_m3: '부피(m³)', weight_kg: '중량(kg)', place: '장소', no_photo_reason: '사진 없는 사유', taken_at: '촬영 시각', source: '출처', line_kind: '구분',
  temp_no: '휴대폰 임시번호', fields: '고칠 칸', code: '사유 코드', items: '품목 수', planned_date: '예정일', phone: '연락처', address: '주소', spot_note: '상세 위치' };
const BODY_VAL = { ...STATUS, ...CONTACT, ...REASON, ...STICKER, ...NOTICE, ...SMS_T, ...NO_PHOTO, BEFORE: '수거 전', AFTER: '수거 후', RECEIVED: '처음 접수', FIELD: '현장 추가', LEAVE: '미수거', COLLECT: '그래도 수거' };
function eventBody(kind, raw) {
  let b; try { b = JSON.parse(raw || '{}'); } catch { return h('span', { class: 'muted' }, '(읽을 수 없는 내용)'); }
  const skip = new Set(['photo_id', 'sha256', 'bytes', 'lines', 'pii_purged']);
  const ent = Object.entries(b || {}).filter(([k, v]) => !skip.has(k) && v !== '' && v != null);
  if (!ent.length) return h('span', { class: 'muted' }, '-');
  return h('dl', { class: 'kv mini' }, ...ent.slice(0, 8).flatMap(([k, v]) => [h('dt', {}, BODY_KEY[k] || k),
    h('dd', {}, k === 'line_id' ? String(v).slice(0, 8) : typeof v === 'object' ? JSON.stringify(v).slice(0, 60) : (BODY_VAL[v] || String(v)).slice(0, 80))]));
}


// ------------------------------------------------------------ 시범 운영 비교 (기존 방식 ↔ 이 시스템)
const FIX_LABEL = { name: '이름', phone: '연락처', address: '주소', spot_note: '상세 위치', planned_date: '예정일' };
function correctionBody(c) {
  let b; try { b = JSON.parse(c.body_json || '{}'); } catch { b = {}; }
  const f = b.fields && typeof b.fields === 'object' ? b.fields : {};
  const cur = c.current || {};
  const show = (k, v) => (k === 'phone' ? fmtPhone(v || '') : v || '(없음)');
  return h('dl', { class: 'kv mini' }, ...Object.keys(FIX_LABEL).filter((k) => k in f).flatMap((k) => [h('dt', {}, FIX_LABEL[k]),
    h('dd', {}, h('span', { class: 'muted' }, show(k, cur[k])), ' → ', h('strong', {}, show(k, f[k])))]),
  b.reason ? h('dt', {}, '사유') : null, b.reason ? h('dd', {}, String(b.reason).slice(0, 100)) : null);
}

// 휴대폰 단독 방식 백업(.dgb) → PC 원본으로 옮기기
function soloCard() {
  const f = h('input', { type: 'file', accept: '.dgb' });
  const pw = h('input', { type: 'password', placeholder: '휴대폰 백업 비밀번호', autocomplete: 'off', style: 'width:200px' });
  const crew = h('select', {}, h('option', { value: '' }, '(하차 기록 붙일 팀 없음)'), ...boot.crews.map((c) => h('option', { value: c.id }, c.name)));
  const out = h('div'), msg = h('p', { class: 'msg' });
  const send = async (preview) => {
    const file = f.files[0]; if (!file) throw new Error('파일을 고르세요.');
    const raw = new Uint8Array(await file.arrayBuffer());
    const pass = btoa(String.fromCharCode(...new TextEncoder().encode(pw.value)));
    const r = await fetch(`/api/import/solo?preview=${preview ? 1 : 0}&crew=${crew.value}`, { method: 'POST', credentials: 'same-origin', body: raw,
      headers: { 'X-DG-CSRF': '1', 'X-DG-Pass': pass, 'Content-Type': 'application/octet-stream' } });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || '가져오기 실패');
    return d;
  };
  const prev = wrap(async () => {
    const d = await send(true);
    out.replaceChildren(h('div', { class: 'notice' },
      h('p', {}, `백업 ${d.created_at ? kst(d.created_at) : ''} · 접수 ${d.requests}건 중 새 접수 ${d.new}건 (이미 PC에 있음 ${d.already}건) · 현장 기록 ${d.events}건 · 사진 ${d.photos}장 · 하차 ${d.unloads}회`),
      h('p', { class: 'small' }, '이미 PC에 있는 접수와 그 기록은 덮어쓰지 않습니다. 접수번호가 PC와 겹치면 끝에 S를 붙입니다.'),
      d.invalid ? h('div', { class: 'notice bad small' }, `옮기지 못하는 접수 ${d.invalid}건 (아래 표). 나머지만 옮깁니다.`,
        grid(['접수(앞 8자)', '접수번호', '문제'], (d.errors || []).map((x) => [h('span', { class: 'mono' }, x.request_id), x.receipt_no || '', x.error]))) : null,
      btn('PC로 옮기기', wrap(async () => { const r = await send(false); toast(r.duplicate_file ? '이미 가져온 파일입니다' : `접수 ${r.requests}건 · 기록 ${r.events}건 · 사진 ${r.photos_recovered}장 · 하차 ${r.unloads_applied}회 옮김${r.renamed ? ` · 접수번호 S 표시 ${r.renamed}건` : ''}${r.invalid ? ` · 못 옮김 ${r.invalid}건` : ''}`); out.replaceChildren(); }, msg), 'primary')));
  }, msg);
  return h('div', { class: 'card' }, h('h2', {}, '휴대폰 단독 방식 자료 옮기기 (.dgb)'),
    h('p', { class: 'muted' }, '휴대폰만으로 운영하던 접수·현장 기록·사진을 PC 원본으로 옮깁니다. 휴대폰 [백업·복원]에서 만든 암호화 백업 파일을 승인된 보안 매체로 가져와 넣으세요.'),
    h('div', { class: 'row' }, f, pw, crew, btn('미리보기', prev, 'small')), out, msg);
}

// 역방향 QR: 휴대폰 화면의 결과 QR을 이 PC 웹캠으로 읽는다 (카메라 사용이 허용된 PC에서만)
function webcamCard(onDone) {
  const video = h('video', { playsinline: true, muted: true, style: 'width:100%;max-width:520px;border-radius:10px;background:#000;display:none' });
  const status = h('p', { class: 'muted' }, '휴대폰 [결과 보내기] → [PC 화면 카메라로 보내기]를 누른 뒤 이 PC 웹캠에 비추세요.');
  const msg = h('p', { class: 'msg' });
  let stream = null, stop = false;
  const end = () => { stop = true; if (stream) stream.getTracks().forEach((t) => t.stop()); video.style.display = 'none'; startBtn.disabled = false; };
  const startBtn = btn('웹캠으로 읽기', wrap(async () => {
    await loadJsQR();
    stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
    stop = false; startBtn.disabled = true; video.style.display = ''; video.srcObject = stream; await video.play();
    const col = new Collector(); let current = null;
    const canvas = document.createElement('canvas'), ctx = canvas.getContext('2d', { willReadFrequently: true });
    const loop = async () => {
      if (stop || !video.isConnected) return end();
      if (video.videoWidth) {
        canvas.width = video.videoWidth; canvas.height = video.videoHeight; ctx.drawImage(video, 0, 0);
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = window.jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' });
        if (code && code.data) {
          try {
            const r = col.add(code.data);
            current ||= r.key;
            if (r.key === current) {
              status.textContent = `조각 ${r.got} / ${r.total} 받음` + (col.missing(current).length <= 10 ? ` · 남은 번호 ${col.missing(current).map((x) => x + 1).join(', ')}` : '');
              if (col.complete(current)) {
                end();
                const res = await api('import/result?channel=QR', { method: 'POST', raw: col.assemble(current) });
                status.textContent = '결과를 받았습니다.'; onDone(res); return;
              }
            }
          } catch (e) { if (e.message && !e.code) { msg.textContent = e.message; msg.className = 'err'; return end(); } }
        }
      }
      setTimeout(loop, 80);
    };
    loop();
  }, msg), 'primary');
  return h('div', { class: 'card' }, h('h2', {}, '휴대폰 화면 QR로 받기 (웹캠)'), status, h('div', { class: 'row' }, startBtn, btn('멈추기', end)), video, msg,
    h('p', { class: 'muted' }, 'QR 조각을 모두 받고 서명·암호 검증이 끝나야 반영됩니다. 카메라 영상은 저장하지 않습니다.'));
}

// ------------------------------------------------------------ 마감

export {
  BODY_KEY, BODY_VAL, CODE_NAME, EVENT_KIND, FIX_LABEL, IMP_KIND, IMP_LABEL, codeEl,
  correctionBody, eventBody, impSummary, screenResults, soloCard, webcamCard,
};
