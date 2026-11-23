// 휴대폰 화면: 결과 보내기
import * as B from '../backup.js';
import * as F from '../field.js';
import * as P from '../proto.js';
import * as S from '../store.js';
import { deriveStop } from '../fees.js';
import { addrRoute, allStops, ask, backBtn, btn, commit, emptyState, errMsg, flowSteps, flushSteps, footBar, go, h, msgBox, outbox, render, say, setSt, smsTemplates, st, toast, todayKST, ui, unsent, writesPending } from '../core.js';
import { APP_VERSION } from '../app.js';
import { closeCheckBox } from './stoplist.js';

async function buildPhotoBatch(candidates, checkView, seal) {
  const photos = [];
  let size = F.PHOTO_EMPTY_PAYLOAD_BYTES, unavailable = 0, tooLarge = 0;
  for (const photo of candidates) {
    checkView();
    let bytes;
    try {
      bytes = await S.getSecureBytes('ph:' + photo.photo_id);
    } catch (error) {
      checkView();
      if (error?.name !== 'OperationError') throw error;
    }
    if (!bytes?.length) { unavailable++; continue; }
    const length = F.photoRecordBytes(photo, bytes.length);
    if (photos.length && size + length > F.PHOTO_BATCH_BUDGET) break;
    const packed = F.photoFileEntry(photo, B.b64(bytes));
    // 큰 사진 한 장은 압축·암호화한 실제 크기로 판단한다.
    if (size + length > F.PHOTO_BATCH_BUDGET) {
      const file = await seal([packed]);
      if (file.bytes.length <= F.PHOTO_UPLOAD_LIMIT) return { file, ids: [photo.photo_id], unavailable, tooLarge };
      tooLarge++;
      continue;
    }
    photos.push(packed);
    size += length;
  }
  return { file: photos.length ? await seal(photos) : null, ids: photos.map((p) => p.photo_id), unavailable, tooLarge };
}

async function screenExport() {
  await flushSteps();
  const pc = await S.getRaw('pc'), dev = await S.getRaw('device');
  if (!pc || !dev) return h('main', {},
    h('header', { class: 'bar' }, backBtn(), h('h1', { class: 'bar-title' }, '결과 보내기')),
    h('section', { class: 'card' }, h('p', {}, '현장 기록을 저장한 배차에서 결과 파일을 만들어 PC 담당자에게 전달하세요.'),
      btn('배차와 현장 기록', () => go('#/guest-list'), 'primary big'),
      st.events.length || st.intakeDraft ? btn('기존 기록 암호화 백업', () => go('#/backup'), 'big') : null));
  const targetState = st, targetList = st.daylist, targetHash = location.hash;
  let view, cleaning = false, makingPhotos = false, confirmingPhotos = false;
  const pending = unsent();  // 결과 파일에 담는 기록(D-코드 전 전부) — 전송 형식 그대로
  const ob = outbox();        // 화면 표시용: 보낼 기록·확인 대기
  const unconfirmedResults = () => allStops().filter((s) => !s.removed && !s.pii_purged && s.status !== 'CANCELLED' && deriveStop(s, st.events).resultNeedsConfirmation);
  const unresolved = unconfirmedResults();
  const checkResultReady = () => {
    if (st !== targetState || !S.isUnlocked() || !view?.isConnected) throw new Error('현재 화면에서 다시 시도해 주세요.');
    if (unconfirmedResults().length) throw new Error('수량을 바꾼 접수의 수거 결과를 먼저 재확인해 주세요. 기록 보호가 필요하면 암호화 백업을 사용하세요.');
  };
  const msg = msgBox(ui.exportMsg || '');
  ui.exportMsg = '';
  const note = (t, bad = false) => { ui.exportMsg = t; say(msg, t, bad); };
  const saveResultFile = (file, name, message) => {
    const u = URL.createObjectURL(file);
    const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(u), 30000);
    note(message);
  };
  // 결과 파일: 기록(이벤트)을 그대로, PC 공개키로 암호화 + 기기 서명. 주민 이름·연락처·주소는 현장 접수·정정 요청에만 들어가고,
  // 직원이 적은 글(현장 메모·취소 요청 내용·하차 메모)도 그대로 들어간다 — 화면 안내(sendCard)와 입력칸 안내가 이를 알린다.
  const buildResult = async () => {
    checkResultReady();
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
    checkResultReady();
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
    } catch (e) { note('QR을 만들지 못했습니다: ' + errMsg(e), true); showQr.disabled = false; }
  }, 'big', { disabled: !pending.length || !!unresolved.length });
  // 파일에는 D-코드 전 기록 전부(보낼 기록 + 확인 대기)가 들어간다 — 전송 형식 그대로
  const make = btn(pending.length ? `결과 파일 만들어 보내기 (${pending.length}건)` : '보낼 결과 없음', async () => {
    make.disabled = true; note('암호화하는 중…');
    try {
      const { sealed, name } = await buildResult();
      const file = new File([sealed.bytes], name, { type: 'application/octet-stream' });
      if (navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: '수거 결과 파일' }); note('업무 메일로 보냈다면, PC 담당자가 알려 주는 결과 반영 코드(D-)를 아래에 입력하세요.'); }
        catch (e) {
          if (e.name === 'AbortError') note('보내기를 취소했습니다. 결과 반영 코드를 받기 전에는 다시 만들어 보낼 수 있습니다.');
          else saveResultFile(file, name, '공유창을 열 수 없어 파일을 저장했습니다. 업무 메일에 첨부해 보내 주세요.');
        }
      } else {
        saveResultFile(file, name, '파일을 저장했습니다. 업무 메일에 첨부해 보내 주세요.');
      }
    } catch (e) { note('파일을 만들지 못했습니다: ' + errMsg(e), true); }
    make.disabled = false; render();
  }, 'primary huge', { disabled: !pending.length || !!unresolved.length });

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
  const { allPhotos, recovered, notRecovered, unprepared } = F.photoRecovery(st);
  const pmsg = msgBox();
  const photoViewCurrent = () => view?.isConnected && location.hash === targetHash && st === targetState && S.isUnlocked();
  function checkPhotoView() {
    if (!photoViewCurrent()) throw new Error('현재 화면에서 다시 시도해 주세요.');
  }
  const makePhotoBatch = async (candidates, button) => {
    if (makingPhotos || confirmingPhotos || cleaning || !photoViewCurrent()) return;
    makingPhotos = true;
    button.disabled = true;
    syncCleanup();
    say(pmsg, '암호화하는 중…');
    try {
      const signerKeyId = await P.keyId(await P.exportRaw(dev.sign.publicKey));
      const recipient = await P.importEcdhPub(pc.pcKexPub);
      const seal = async (photos) => {
        checkPhotoView();
        const header = { btype: 4, bundleId: P.randomBytes(16), deviceId: dev.id, assignVer: 0, dataVer: 1, signerKeyId };
        const sealed = await P.seal({ schema: 'dg.photos/1', photos }, header, recipient, dev.sign.privateKey);
        return { ...sealed, bundleId: header.bundleId };
      };
      const { file, ids, unavailable, tooLarge } = await buildPhotoBatch(candidates, checkPhotoView, seal);
      const issues = [
        unavailable ? `원본을 읽을 수 없는 사진 ${unavailable}장` : '',
        tooLarge ? `한도를 넘은 사진 ${tooLarge}장` : '',
      ].filter(Boolean);
      const issueNote = issues.length ? ` ${issues.join(', ')}은 미회수로 남겨 두었습니다.` : '';
      if (!file) throw new Error('이 파일에 담을 사진이 없습니다.' + issueNote);
      checkPhotoView();
      if (file.bytes.length > F.PHOTO_UPLOAD_LIMIT) throw new Error('PC의 사진 반입 한도를 넘었습니다.');
      if (knownCodes().PHOTO.includes(file.receipt)) throw new Error('사진 회수 코드가 겹쳤습니다. 파일을 다시 만들어 주세요.');
      const name = `DG_P_${P.uuidStr(dev.id).slice(0, 8)}_${P.uuidStr(file.bundleId)}.dgp`;
      const entry = { code: file.receipt, ids, name, at: new Date().toISOString(), bytes: file.bytes.length };
      st.photoExports = [...(st.photoExports || []), entry];
      if (!await commit()) {
        if (st === targetState && S.isUnlocked()) {
          st.photoExports = st.photoExports.filter((old) => old !== entry);
          await commit(); // 실패한 파일의 확인 정보가 복구 대기 상태에 남지 않게 한다.
        }
        throw new Error('사진 회수 정보를 저장하지 못했습니다. 다시 시도해 주세요.');
      }
      checkPhotoView();
      const u = URL.createObjectURL(new File([file.bytes], name, { type: 'application/octet-stream' }));
      const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 30000);
      const remaining = F.photoRecovery(st).unprepared.length;
      const next = remaining ? ` 파일을 만들지 않은 사진 ${remaining}장은 [사진 파일 만들기]로 이어서 받으세요.`
        : ' 승인된 보안 매체로 PC에 옮긴 뒤 파일마다 사진 회수 코드(P-)를 받으세요.';
      note(`${name}에 사진 ${ids.length}장을 담았습니다.${issueNote}${next}`);
      await render();
    } catch (e) {
      if (photoViewCurrent()) say(pmsg, '사진 파일을 만들지 못했습니다. ' + errMsg(e), true);
    } finally {
      makingPhotos = false;
      if (photoViewCurrent()) { button.disabled = false; syncCleanup(); }
    }
  };
  const makePhotos = btn(unprepared.length ? `사진 파일 만들기 (${unprepared.length}장)` : '새로 만들 사진 파일 없음',
    (e) => makePhotoBatch(unprepared, e.currentTarget), 'big', { disabled: !unprepared.length });
  const retries = (st.photoExports || []).slice().reverse().map((entry) => ({
    name: entry.name || '이전 사진 파일', photos: notRecovered.filter((photo) => entry.ids.includes(photo.photo_id)),
  })).filter(({ photos }) => photos.length);
  const pcode = h('input', { type: 'text', placeholder: '사진 회수 코드 8자리', 'aria-label': '사진 회수 코드 (P-)', class: 'code-in', autocapitalize: 'characters', autocomplete: 'off' });
  const pcodeMsg = h('p', { class: 'msg err', role: 'alert' });
  const pconfirm = btn('사진 회수 코드 입력', async () => {
    if (makingPhotos || confirmingPhotos || cleaning || !photoViewCurrent()) return;
    const r = F.checkCode(pcode.value, 'PHOTO', knownCodes());
    pcodeMsg.textContent = r.error || '';
    if (r.error) return toast(r.error, 'bad');
    const ex = (st.photoExports || []).find((x) => x.code === r.code);
    confirmingPhotos = true;
    pconfirm.disabled = true;
    syncCleanup();
    try {
      st.recoveredPhotos = [...new Set([...(st.recoveredPhotos || []), ...ex.ids])];
      const saved = await commit();
      if (!photoViewCurrent()) return;
      if (!saved) return say(pcodeMsg, '사진 회수 확인을 저장하지 못했습니다. 같은 코드를 다시 입력해 주세요.', true);
      toast(`사진 ${ex.ids.length}장 회수가 확인되었습니다`);
      await render();
    } finally {
      confirmingPhotos = false;
      if (photoViewCurrent()) { pconfirm.disabled = false; syncCleanup(); }
    }
  }, 'big');

  const allConfirmed = !unsent().length && st.exports.every((x) => x.confirmed);
  const cleanupCurrent = () => view?.isConnected && location.hash === targetHash && st === targetState && st.daylist === targetList;
  const cleanupReady = () => st === targetState && st.daylist === targetList && !!targetList && S.isUnlocked()
    && !makingPhotos && !confirmingPhotos && !unconfirmedResults().length && !writesPending()
    && !S.hasPendingSecure('state') && !unsent().length && st.exports.every((x) => x.confirmed);
  const cleanupBlocked = () => { toast('저장 중인 사진이나 새 기록이 있습니다. PC 반영을 확인한 뒤 다시 정리해 주세요.', 'bad'); render(); };
  const cleanup = btn('오늘 자료 정리', async () => {
    if (cleaning || !cleanupCurrent()) return;
    if (!cleanupReady()) return cleanupBlocked();
    cleaning = true; cleanup.disabled = true;
    const seq = st.seq, draft = st.intakeDraft;
    // 확인창·사진 삭제를 기다리는 동안 생긴 기록이나 새 업무(새로 생긴 접수 초안 포함)를 이전 화면의 확인으로 지우지 않는다.
    const unchanged = () => {
      if (!cleanupCurrent()) return false;
      if (st.seq !== seq || !cleanupReady() || (st.intakeDraft && st.intakeDraft !== draft)) { cleanupBlocked(); return false; }
      return true;
    };
    try {
      const text = '반영이 확인된 목록과 기록을 이 휴대폰에서 지웁니다. 회수가 확인된 사진만 지우고, 나머지 사진은 남깁니다.';
      // 작성 중인 접수 초안은 조용히 지우지 않는다: 알리고, 초안까지 지운다는 단추를 따로 눌러야 정리한다.
      if (!(await (draft
        ? ask(`${text} 작성 중인 접수 초안이 있습니다. 정리하면 이 초안도 지워집니다. 초안을 살리려면 [취소]를 누르고 목록의 [+ 접수 등록]에서 접수를 마치세요.`, '초안까지 지우고 정리', { danger: true })
        : ask(text, '정리하기')))) return;
      for (const p of allPhotos) if (recovered.has(p.photo_id)) {
        if (!unchanged()) return;
        await S.delSecureBytes('ph:' + p.photo_id);
      }
      if (!unchanged()) return;
      const keepCfg = Object.fromEntries(['mapEnabled', 'textSize', 'lockMin', 'workers', 'worker', 'smsTemplates', 'navApp', 'jusoKey', 'jusoCoordKey', 'addrRoute', 'unloadSite', 'unsentWarnH', 'retentionDays', 'geoEvidence', 'feeFavs', 'feeUsage', 'deviceCheck', 'navAppSet', 'mapPrefetchedAt', 'setupHidden', 'storagePersisted', 'persistHintOff',
        'receiptCounter'].map((k) => [k, st[k]]));  // receiptCounter: 같은 날 다시 접수해도 현장 접수 임시번호가 1부터 다시 시작하지 않게
      const cleared = { ...emptyState(), ...keepCfg, mode: st.mode, seq: st.seq, releasedSeq: st.seq, gen: st.gen, gens: st.gens, genRestored: st.genRestored, keptPhotos: notRecovered, photoExports: st.photoExports, recoveredPhotos: [] };
      setSt(cleared);
      const saved = await commit();
      if (!view.isConnected || location.hash !== targetHash || st !== cleared || st.seq !== seq || !S.isUnlocked()) return;
      if (!saved) { toast('정리 상태를 저장하지 못했습니다. 앱을 닫지 말고 저장 실패 안내를 확인해 주세요.', 'bad'); render(); return; }
      toast('정리했습니다'); go('#/home');
    } finally {
      cleaning = false;
      if (cleanupCurrent()) syncCleanup();
    }
  }, 'ghost', { disabled: !cleanupReady() });
  const cleanupHint = h('p', { class: 'small' });
  const syncCleanup = () => {
    cleanup.disabled = cleaning || !cleanupReady();
    // 정리할 조건을 채우면 이 화면의 주 단추
    cleanup.classList.toggle('primary', !cleanup.disabled); cleanup.classList.toggle('big', !cleanup.disabled); cleanup.classList.toggle('ghost', cleanup.disabled);
    cleanupHint.textContent = unconfirmedResults().length ? '수량을 바꾼 접수의 수거 결과를 먼저 재확인해 주세요.' : unsent().length || st.exports.some((x) => !x.confirmed) ? '모든 결과의 PC 반영이 확인되어야 정리할 수 있습니다.'
      : writesPending() || S.hasPendingSecure('state') ? '사진·기록 저장이 끝나야 정리할 수 있습니다. 대기 중인 사진은 목록에서 [사진 다시 저장]으로 저장을 마쳐 주세요.'
      : st.intakeDraft ? '작성 중인 접수 초안이 있습니다. 정리하면 초안도 지워지니, 먼저 목록의 [+ 접수 등록]에서 접수를 마치거나 확인창에서 초안까지 지울지 고르세요.' : '';
    cleanupHint.hidden = !cleanupHint.textContent;
  };
  const watchCleanupRecovery = () => {
    if (!cleanupCurrent()) return;
    syncCleanup();
    // 저장 문제로 대기 중인 화면에서만 재확인한다. 복구되거나 화면을 떠나면 멈춘다.
    if (writesPending() || S.hasPendingSecure('state')) setTimeout(watchCleanupRecovery, 250);
  };
  syncCleanup();

  const waitingCode = st.exports.some((x) => !x.confirmed);
  const cur = (pending.length && !waitingCode) || (!st.exports.length && !allPhotos.length) ? 0 : waitingCode ? 1 : unprepared.length ? 2 : notRecovered.length ? 3 : 4;
  // 단계 카드 ①~⑤는 위 단계 표시와 1:1. 지금 단계만 펼치고 나머지는 한 줄 요약(지금 / ✓ 완료 / 대기)으로 접는다(눌러 펼칠 수 있음).
  const doneStep = [
    !ob.toSend && st.exports.length > 0,
    st.exports.length > 0 && !waitingCode,
    allPhotos.length ? !unprepared.length : cur > 2,
    allPhotos.length ? !notRecovered.length : cur > 3,
    false,
  ];
  if (!ui.exportOpen || ui.exportOpen.cur !== cur) ui.exportOpen = { cur, open: [] };
  const opened = ui.exportOpen;
  // ⑤ 정리는 조건을 채웠으면(미회수 사진은 남겨 둔 채 정리 가능) 지금 단계가 아니어도 펼쳐 둔다
  const ready = cleanupReady();
  const stepCard = (i, title, cls, ...kids) => h('details', { class: `card step-card ${cls}`, open: i === cur || (i === 4 && ready) || opened.open.includes(i),
    ontoggle: (e) => { if (i === cur) return; opened.open = opened.open.filter((x) => x !== i).concat(e.target.open ? [i] : []); } },
  h('summary', {}, h('h2', {}, h('span', { class: 'stepno' }, String(i + 1)), title),
    h('span', { class: 'step-state' + (i === cur || (i === 4 && ready) ? ' now' : doneStep[i] ? ' done' : '') },
      i === cur ? '지금' : i === 4 && ready ? '정리 가능' : doneStep[i] ? '✓ 완료' : '대기')),
  ...kids);
  // ② 결과 반영 코드(D-) 넣기
  const codeCard = stepCard(1, 'D-코드 넣기 (결과 반영 코드)', 'code-card' + (waitingCode ? ' ask' : ''),
    h('p', { class: 'small' }, 'PC 담당자가 결과를 가져온 뒤 알려 주는 결과 반영 코드(D-로 시작, 8자리)를 넣으세요. 넣어야 "보낸 것"으로 처리되고, 끝난 곳의 개인정보가 이 휴대폰에서 지워집니다. PC가 결과를 가져온 뒤 만든 새 목록을 받으면 코드 없이도 저절로 확인됩니다.'),
    codeBox('RESULT', codeIn), codeMsg, confirm,
    h('ul', { class: 'small list' }, ...st.exports.slice(-5).reverse().map((x) => h('li', {}, `${x.name} · ${x.superseded ? '뒤 파일로 반영됨' : x.via === 'list' ? '반영 확인됨(새 목록)' : x.confirmed ? '반영 확인됨' : '확인 대기'}`))));
  // 직원이 직접 적은 글이 실리는 기록: 현장 메모, 내용을 적은 취소 요청, 메모를 적은 하차 기록
  const freeText = pending.filter((e) => (e.kind === 'FIELD_MEMO' && e.body?.text) || (e.kind === 'CANCEL_REQUEST' && e.body?.text) || (e.kind === 'UNLOAD' && e.body?.note));
  // ① 결과 보내기: 첫 설명은 한 줄 + 작은 개인정보 안내, 나머지는 [자세히] 안. 이번 파일에 들어가는 직접 쓴 글·주민 정보 건수는 그대로 보인다.
  const sendCard = stepCard(0, ['결과 보내기', ob.toSend ? `보낼 기록 ${ob.toSend}건` : '', ob.waiting ? `확인 대기 ${ob.waiting}건` : ''].filter(Boolean).join(' · '), 'send-card',
    unresolved.length ? h('section', { class: 'callout warn', 'aria-label': '수거 결과 재확인 필요' },
      h('strong', {}, `수거 결과 재확인 ${unresolved.length}건`), h('p', {}, '결과 저장 뒤 수량이 바뀐 접수가 있습니다. 현재 수량에 맞는 결과를 다시 저장한 뒤 보내세요.'),
      ...unresolved.map((s) => btn(`${s.seq}번 결과 재확인`, () => go(`#/stop/${s.request_id}`), 'big')),
      btn('기록 보호용 백업', () => go('#/backup'), 'big')) : null,
    h('p', { class: 'send-lead' }, 'PC만 열 수 있게 암호화해 업무 메일로 보냅니다.'),
    h('p', { class: 'small' }, '메모에 이름·연락처 등 개인정보를 적지 마세요.'),
    freeText.length ? h('p', { class: 'callout warn free-text' }, `이번 파일에는 직접 적은 글(현장 메모·취소 요청 내용·하차 메모) ${freeText.length}건이 들어갑니다. 개인정보가 적혀 있지 않은지 확인하세요.`) : null,
    pending.some((e) => e.kind === 'FIELD_INTAKE' || e.kind === 'CORRECTION') ? h('p', { class: 'callout warn' },
      `이번 파일에는 현장 접수·정정 요청 ${pending.filter((e) => e.kind === 'FIELD_INTAKE' || e.kind === 'CORRECTION').length}건의 이름·연락처·주소가 들어갑니다(PC가 원본을 만들거나 고치려면 필요). PC 전용 암호화는 같으며, 업무 메일이나 PC 화면 카메라로만 보내세요.`) : null,
    h('details', { class: 'send-more' }, h('summary', {}, '자세히'),
      h('p', { class: 'small' }, '결과 파일은 PC에서만 열 수 있게 암호화됩니다. 주민 이름·연락처·주소는 넣지 않지만(현장 접수·정정 요청은 예외), 현장 메모·취소 요청 내용·하차 메모에 적은 글은 그대로 PC로 갑니다. 그런 칸에는 이름·연락처 등 개인정보를 적지 마세요. 사진은 넣지 않습니다. 하루 중간에 보내도 되고(지금까지 결과), 그 뒤 기록은 다음 파일에 이어서 들어갑니다. 결과 반영 코드(D-)를 넣기 전까지는 보낸 기록도 "확인 대기"로 남아 다음 파일에 다시 들어갑니다.')),
    showQr, h('p', { class: 'small' }, '업무 메일을 쓸 수 없으면 [PC 화면 카메라로 보내기]로 PC 웹캠에 QR을 보여 주세요.'));
  // ③ 사진 보내기 · ④ 사진 회수 코드(P-) 넣기 · ⑤ 정리
  const photoCard = stepCard(2, `사진 보내기 · 사진 ${allPhotos.length}장 · 미회수 ${notRecovered.length}장`, 'photo-card',
    h('p', { class: 'small' }, '사진 파일은 PC에서만 열 수 있게 암호화됩니다. 승인된 보안 매체로 옮기세요. 사진이 많으면 여러 파일로 나뉘므로 남은 사진이 있을 때 [사진 파일 만들기]를 다시 누르세요.'),
    makePhotos, pmsg,
    retries.length ? h('details', { class: 'send-more' }, h('summary', {}, '사진 파일 다시 만들기'),
      h('p', { class: 'small' }, 'PC에 도착하지 않은 파일을 고르면 아직 회수되지 않은 사진을 새 파일로 만듭니다. 크기에 따라 여러 번 필요할 수 있습니다.'),
      h('ul', { class: 'small list' }, ...retries.map(({ name, photos }) => h('li', {},
        btn(`${name} 다시 만들기`, (e) => makePhotoBatch(photos, e.currentTarget), 'ghost'))))) : null);
  const photoCodeCard = stepCard(3, 'P-코드 넣기 (사진 회수 코드)', 'photo-code-card',
    h('p', { class: 'small' }, '파일마다 PC가 알려 주는 사진 회수 코드(P-로 시작)를 넣으세요. PC가 사진을 가져온 뒤 만든 새 팀 목록으로도 회수를 확인할 수 있습니다.'),
    codeBox('PHOTO', pcode), pcodeMsg, pconfirm);
  const cleanCard = stepCard(4, '정리', 'cleanup-card', cleanup, cleanupHint);
  // 결과 파일 단추(하단 고정)는 ① 단계일 때만 주 단추
  if (cur !== 0) { make.classList.remove('primary', 'huge'); make.classList.add('big'); }
  // 결과 반영 코드 입력도 ② 단계일 때만 주 단추(하단 바의 주 단추와 한 화면에 둘이 보이지 않게)
  if (cur !== 1) { confirm.classList.remove('primary', 'huge'); confirm.classList.add('big'); }
  view = h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, backBtn(), h('h1', { class: 'bar-title' }, '결과 보내기')),
    closeCheckBox(),
    flowSteps(['결과 파일', '결과 반영 코드', '사진 파일', '사진 회수 코드', '정리'], cur),
    msg,  // 결과 파일을 만든 뒤 다음 단계(②)로 넘어가 ① 카드가 접혀도 안내가 보이게 카드 밖에 둔다
    sendCard, codeCard, photoCard, photoCodeCard, cleanCard,
    footBar(make));
  setTimeout(watchCleanupRecovery, 0);
  return view;
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
    if (d.resultNeedsConfirmation || !['DONE', 'PARTIAL', 'NOT_COLLECTED'].includes(d.outcome) || mine.some((e) => e.seq > st.releasedSeq)) continue;
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
