// 휴대폰 화면: 결과 보내기
import * as B from '../backup.js';
import * as F from '../field.js';
import * as P from '../proto.js';
import * as S from '../store.js';
import { deriveStop } from '../fees.js';
import { addrRoute, ask, btn, commit, emptyState, errMsg, flowSteps, footBar, go, h, render, setSt, smsTemplates, st, toast, todayKST, ui, unsent } from '../core.js';
import { APP_VERSION } from '../app.js';

async function screenExport() {
  const pc = await S.getRaw('pc'), dev = await S.getRaw('device');
  const pending = unsent();
  const photoMetas = st.events.filter((e) => e.kind === 'PHOTO_META');
  const msg = h('p', { class: 'msg' }, ui.exportMsg || '');
  ui.exportMsg = '';
  const say = (t) => { ui.exportMsg = t; msg.textContent = t; };
  const saveResultFile = (file, name, message) => {
    const u = URL.createObjectURL(file);
    const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(u), 30000);
    say(message);
  };
  // 결과 파일: 이름·연락처·주소·메모 없이 이벤트만, PC 공개키로 암호화 + 기기 서명
  const buildResult = async () => {
    const from = pending[0].seq, to = pending[pending.length - 1].seq;
    const payload = {
      schema: 'dg.result/1', work_date: st.daylist ? st.daylist.work_date : null, app_version: APP_VERSION,
      // gen: 기록 세대(백업 복원마다 바뀜). gens: 보내는 기록의 세대별 첫 번호 — PC가 세대마다 '빠짐없이 받은 번호'를 센다(rel.g)
      events: pending.map(({ event_id, seq, gen, request_id, assign_ver, base_req_ver, kind, body, ts }) => ({ event_id, seq, ...(gen ? { gen } : {}), request_id, assign_ver, base_req_ver, kind, body, device_ts: ts })),
      gens: F.gensOf(st, pending),
      photos: pending.filter((e) => e.kind === 'PHOTO_META').map((e) => ({ photo_id: e.body.photo_id, request_id: e.request_id, phase: e.body.phase, sha256: e.body.sha256, bytes: e.body.bytes })),
    };
    const signRaw = await P.exportRaw(dev.sign.publicKey);
    const header = { btype: P.TYPE_RESULT, bundleId: P.randomBytes(16), deviceId: dev.id, assignVer: st.daylist ? st.daylist.assign_ver : 0, dataVer: 1, signerKeyId: await P.keyId(signRaw) };
    const sealed = await P.seal(payload, header, await P.importEcdhPub(pc.pcKexPub), dev.sign.privateKey);
    const name = `DG_R_${P.uuidStr(dev.id).slice(0, 8)}_${from}-${to}.dgr`;
    st.exports.push({ from, to, code: sealed.receipt, name, at: new Date().toISOString(), confirmed: false });
    await commit();
    return { sealed, name, bundleId: header.bundleId };
  };
  const showQr = btn('PC 화면 카메라로 보내기 (QR)', async () => {
    showQr.disabled = true;
    try {
      const { sealed, name, bundleId } = await buildResult();
      ui.resultQr = { frames: P.splitFrames(sealed.bytes, bundleId, 600), name };
      go('#/resultqr');
    } catch (e) { say('QR을 만들지 못했습니다: ' + errMsg(e)); showQr.disabled = false; }
  }, 'big', { disabled: !pending.length });
  const make = btn(pending.length ? `결과 파일 만들어 보내기 (${pending.length}건)` : '보낼 결과 없음', async () => {
    make.disabled = true; msg.textContent = '암호화하는 중…';
    try {
      const { sealed, name } = await buildResult();
      const file = new File([sealed.bytes], name, { type: 'application/octet-stream' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: '수거 결과 파일' }); say('업무 메일로 보냈다면, PC 담당자가 알려 주는 결과 반영 코드(D-)를 아래에 입력하세요.'); }
        catch (e) {
          if (e.name === 'AbortError') say('보내기를 취소했습니다. 결과 반영 코드를 받기 전에는 다시 만들어 보낼 수 있습니다.');
          else saveResultFile(file, name, '공유창을 열 수 없어 파일을 저장했습니다. 업무 메일에 첨부해 보내 주세요.');
        }
      } else {
        saveResultFile(file, name, '파일을 저장했습니다. 업무 메일에 첨부해 보내 주세요.');
      }
    } catch (e) { say('파일을 만들지 못했습니다: ' + errMsg(e)); }
    make.disabled = false; render();
  }, 'primary huge', { disabled: !pending.length });

  // PC → 휴대폰 코드 두 가지(결과 반영 코드 D-, 사진 회수 코드 P-)는 칸이 따로다. 다른 칸 코드나 수신 코드(R-)를 넣으면 알려 준다.
  const knownCodes = () => ({ RESULT: st.exports.map((x) => x.code), PHOTO: (st.photoExports || []).map((x) => x.code), RECEIPT: st.lastReceipt ? [st.lastReceipt.code] : [] });
  const codeBox = (kind, input) => h('div', { class: 'code-entry k-' + kind }, h('span', { class: 'code-kind', 'aria-hidden': 'true' }, F.CODE_KINDS[kind].prefix + '-'), input);
  const codeIn = h('input', { type: 'text', placeholder: '결과 반영 코드 8자리', 'aria-label': '결과 반영 코드 (D-)', class: 'code-in', autocapitalize: 'characters', autocomplete: 'off' });
  const codeMsg = h('p', { class: 'msg err', role: 'alert' });
  const confirm = btn('결과 반영 코드 입력', async () => {
    const r = F.checkCode(codeIn.value, 'RESULT', knownCodes());
    codeMsg.textContent = r.error || '';
    if (r.error) return toast(r.error, 'bad');
    const ex = st.exports.find((x) => x.code === r.code);
    ex.confirmed = true; st.releasedSeq = Math.max(st.releasedSeq, ex.to);
    // 공유를 취소했거나 다시 만든 예전 결과 파일은 이 확인으로 보낸 범위에 들어가므로 '대체됨'으로 끝낸다
    for (const x of st.exports) if (!x.confirmed && x.to <= st.releasedSeq) { x.confirmed = true; x.superseded = true; }
    const n = purgeReleased();
    await commit(); toast('PC 반영이 확인되었습니다' + (n ? ` · 끝난 ${n}곳의 개인정보를 이 휴대폰에서 지웠습니다` : '')); render();
  }, 'primary huge');

  // 사진: 결과 파일과 따로, PC 공개키로 암호화한 사진 파일(.dgp)을 승인된 보안 매체로 옮긴다.
  // PC가 해시를 대조하고 알려 주는 "사진 회수 코드"를 넣은 사진만 이 휴대폰에서 지울 수 있다.
  const recovered = new Set(st.recoveredPhotos || []);
  const allPhotos = [...photoMetas.map((e) => ({ ...e.body, request_id: e.request_id })), ...(st.keptPhotos || [])];
  const notRecovered = allPhotos.filter((p) => !recovered.has(p.photo_id));
  const pmsg = h('p', { class: 'msg' });
  const makePhotos = btn(`사진 파일 만들기 (${notRecovered.length}장)`, async () => {
    makePhotos.disabled = true; pmsg.textContent = '암호화하는 중…';
    try {
      const photos = [];
      for (const p of notRecovered) {
        const b = await S.getSecureBytes('ph:' + p.photo_id);
        if (b) photos.push({ photo_id: p.photo_id, request_id: p.request_id, phase: p.phase, sha256: p.sha256, data: B.b64(b) });
      }
      const signRaw = await P.exportRaw(dev.sign.publicKey);
      const header = { btype: 4, bundleId: P.randomBytes(16), deviceId: dev.id, assignVer: 0, dataVer: 1, signerKeyId: await P.keyId(signRaw) };
      const sealed = await P.seal({ schema: 'dg.photos/1', photos }, header, await P.importEcdhPub(pc.pcKexPub), dev.sign.privateKey);
      const name = `DG_P_${P.uuidStr(dev.id).slice(0, 8)}_${new Date().toISOString().slice(0, 16).replace(/\D/g, '')}.dgp`;
      st.photoExports = [...(st.photoExports || []), { code: sealed.receipt, ids: photos.map((p) => p.photo_id), name, at: new Date().toISOString() }];
      await commit();
      const u = URL.createObjectURL(new File([sealed.bytes], name, { type: 'application/octet-stream' }));
      const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 30000);
      pmsg.textContent = `${name} (${(sealed.bytes.length / 1024).toFixed(0)}KB)을 저장했습니다. 승인된 보안 매체로 PC에 옮긴 뒤 사진 회수 코드(P-)를 받으세요.`;
    } catch (e) { pmsg.textContent = '사진 파일을 만들지 못했습니다: ' + errMsg(e); }
    makePhotos.disabled = false;
  }, 'big', { disabled: !notRecovered.length });
  const pcode = h('input', { type: 'text', placeholder: '사진 회수 코드 8자리', 'aria-label': '사진 회수 코드 (P-)', class: 'code-in', autocapitalize: 'characters', autocomplete: 'off' });
  const pcodeMsg = h('p', { class: 'msg err', role: 'alert' });
  const pconfirm = btn('사진 회수 코드 입력', async () => {
    const r = F.checkCode(pcode.value, 'PHOTO', knownCodes());
    pcodeMsg.textContent = r.error || '';
    if (r.error) return toast(r.error, 'bad');
    const ex = (st.photoExports || []).find((x) => x.code === r.code);
    st.recoveredPhotos = [...new Set([...(st.recoveredPhotos || []), ...ex.ids])];
    await commit(); toast(`사진 ${ex.ids.length}장 회수가 확인되었습니다`); render();
  }, 'big');

  const allConfirmed = !unsent().length && st.exports.every((x) => x.confirmed);
  const cleanup = btn('오늘 자료 정리', async () => {
    if (!(await ask('반영이 확인된 목록과 기록을 이 휴대폰에서 지웁니다. 회수가 확인된 사진만 지우고, 나머지 사진은 남깁니다.', '정리하기'))) return;
    for (const p of allPhotos) if (recovered.has(p.photo_id)) await S.delSecureBytes('ph:' + p.photo_id);
    const keepCfg = Object.fromEntries(['mapEnabled', 'textSize', 'lockMin', 'workers', 'worker', 'smsTemplates', 'navApp', 'jusoKey', 'jusoCoordKey', 'addrRoute', 'unloadSite', 'unsentWarnH', 'retentionDays', 'geoEvidence', 'feeFavs', 'feeUsage', 'deviceCheck', 'navAppSet', 'mapPrefetchedAt', 'setupHidden', 'storagePersisted', 'persistHintOff'].map((k) => [k, st[k]]));
    setSt({ ...emptyState(), ...keepCfg, mode: st.mode, seq: st.seq, releasedSeq: st.seq, gen: st.gen, gens: st.gens, genRestored: st.genRestored, keptPhotos: notRecovered, photoExports: st.photoExports, recoveredPhotos: [] });
    await commit(); toast('정리했습니다'); go('#/home');
  }, 'ghost', { disabled: !allConfirmed || !st.daylist });

  const waitingCode = st.exports.some((x) => !x.confirmed);
  const sentPhotos = new Set((st.photoExports || []).flatMap((x) => x.ids));
  const cur = (pending.length && !waitingCode) || (!st.exports.length && !allPhotos.length) ? 0 : waitingCode ? 1 : notRecovered.some((p) => !sentPhotos.has(p.photo_id)) ? 2 : notRecovered.length ? 3 : 4;
  // 결과 반영 코드 입력: 보낸 뒤 기다리는 코드가 있으면 맨 위에 크게
  const codeCard = h('section', { class: 'card code-card' + (waitingCode ? ' ask' : '') },
    h('h2', {}, h('span', { class: 'stepno' }, '2'), '결과 반영 코드 입력 (PC → 휴대폰)'),
    h('p', { class: 'small' }, 'PC 담당자가 결과를 가져온 뒤 알려 주는 결과 반영 코드(D-로 시작, 8자리)를 넣으세요. 넣어야 "보낸 것"으로 처리되고, 끝난 곳의 개인정보가 이 휴대폰에서 지워집니다. PC가 결과를 가져온 뒤 만든 새 목록을 받으면 코드 없이도 저절로 확인됩니다.'),
    codeBox('RESULT', codeIn), codeMsg, confirm,
    h('ul', { class: 'small list' }, ...st.exports.slice(-5).reverse().map((x) => h('li', {}, `${x.name} · ${x.superseded ? '뒤 파일로 반영됨' : x.via === 'list' ? '반영 확인됨(새 목록)' : x.confirmed ? '반영 확인됨' : '확인 대기'}`))));
  const sendCard = h('section', { class: 'card' },
    h('h2', {}, h('span', { class: 'stepno' }, '1'), pending.length ? `결과 보내기 · 미전송 ${pending.length}건` : '결과 보내기'),
    h('p', {}, '결과 파일에는 이름·연락처·주소·메모가 들어가지 않으며, PC에서만 열 수 있게 암호화됩니다. 사진은 넣지 않습니다. 하루 중간에 보내도 되고(중간 결과), 그 뒤 기록은 다음 파일에 이어서 들어갑니다.'),
    pending.some((e) => e.kind === 'FIELD_INTAKE' || e.kind === 'CORRECTION') ? h('p', { class: 'notice' },
      `이번 파일에는 현장 접수·정정 요청 ${pending.filter((e) => e.kind === 'FIELD_INTAKE' || e.kind === 'CORRECTION').length}건의 이름·연락처·주소가 들어갑니다(PC가 원본을 만들거나 고치려면 필요). PC 전용 암호화는 같으며, 업무 메일이나 PC 화면 카메라로만 보내세요.`) : null,
    showQr, h('p', { class: 'small' }, '업무 메일을 쓸 수 없으면 [PC 화면 카메라로 보내기]로 PC 웹캠에 QR을 보여 주세요.'), msg);
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '결과 보내기')),
    flowSteps(['결과 파일', '결과 반영 코드', '사진 파일', '사진 회수 코드', '정리'], cur),
    waitingCode ? codeCard : null, sendCard, waitingCode ? null : codeCard,
    h('section', { class: 'card' }, h('h2', {}, h('span', { class: 'stepno' }, '3'), `사진 ${allPhotos.length}장 · 미회수 ${notRecovered.length}장`),
      h('p', { class: 'small' }, '사진 파일은 PC에서만 열 수 있게 암호화됩니다. 업무 메일이 아니라 승인된 보안 매체로 옮기세요. 회수가 확인되기 전에는 휴대폰에서 지우지 않습니다. PC가 알려 주는 사진 회수 코드(P-로 시작)를 넣거나, PC가 사진을 가져온 뒤 만든 새 목록을 받으면 회수가 확인됩니다.'),
      makePhotos, pmsg, codeBox('PHOTO', pcode), pcodeMsg, pconfirm),
    h('section', { class: 'card' }, h('h2', {}, h('span', { class: 'stepno' }, '5'), '정리'), cleanup,
      allConfirmed ? null : h('p', { class: 'small' }, '모든 결과의 PC 반영이 확인되어야 정리할 수 있습니다.')),
    footBar(make));
}
// 결과 반영 코드를 넣은 뒤(또는 새 목록의 반영 정보): 결과가 끝났고(완료·일부·미수거) 그 기록이 모두 PC에 반영된 곳의 개인정보를 지운다.
// 수량·결과 같은 기록은 사진 회수까지 남긴다. 보류·처리 전인 곳은 그대로 둔다.
function purgeReleased() {
  const at = new Date().toISOString();
  let n = 0;
  for (const s of [...(st.daylist ? st.daylist.stops : []), ...(st.fieldStops || [])]) {
    if (s.pii_purged) continue;
    const mine = st.events.filter((e) => e.request_id === s.request_id);
    const d = deriveStop(s, st.events);
    if (!['DONE', 'PARTIAL', 'NOT_COLLECTED'].includes(d.outcome) || mine.some((e) => e.seq > st.releasedSeq)) continue;
    // 오늘 다시 가기로 약속했거나 재연락 예정이면 연락처·주소가 아직 필요하다
    if (d.outcome !== 'DONE' && (d.contact === 'CALLBACK' || (d.appt && (!d.appt.date || d.appt.date === todayKST())))) continue;
    F.purgeStopPII(s, at);
    for (const e of mine) F.purgeEventPII(e);
    n++;
  }
  return n;
}

// ------------------------------------------------------------ 사용 방식 선택

export {
  purgeReleased, screenExport,
};
