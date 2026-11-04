// 휴대폰 화면: 단독 모드 백업·복원
import * as B from '../backup.js';
import * as F from '../field.js';
import * as S from '../store.js';
import { ERR, ago, ask, btn, commit, emptyState, errMsg, flowSteps, footBar, go, h, isSolo, pcStops, setSt, settleWrites, st, toast, todayKST, ui } from '../core.js';
import { APP_VERSION } from '../app.js';

async function screenBackup() {
  const BSTEPS = ['비밀번호 정하기', '파일 만들기', '안전한 곳에 보관'];
  let flow = flowSteps(BSTEPS, ui.backupDone ? 2 : 0);
  ui.backupDone = false;
  const photoIds = st.events.filter((e) => e.kind === 'PHOTO_META').map((e) => e.body.photo_id);
  const msg = h('p', { class: 'msg' });
  const pw1 = h('input', { type: 'password', placeholder: '백업 비밀번호 (12자 이상)', class: 'search', autocomplete: 'new-password' });
  const pw2 = h('input', { type: 'password', placeholder: '한 번 더', class: 'search', autocomplete: 'new-password' });
  const withPhotos = h('input', { type: 'checkbox', checked: true });
  const make = btn('암호화 백업 파일 만들기', async () => {
    if (pw1.value.length < 12) return (msg.textContent = '백업 비밀번호는 12자 이상으로 정해 주세요(PC 백업과 같은 기준).');
    if (pw1.value !== pw2.value) return (msg.textContent = '두 번 입력한 비밀번호가 다릅니다.');
    make.disabled = true; msg.textContent = '암호화하는 중…';
    try {
      const photos = {};
      if (withPhotos.checked) for (const pid of photoIds) { const b = await S.getSecureBytes('ph:' + pid); if (b) photos[pid] = B.b64(b); }
      const at = new Date().toISOString();
      const { intakeDraft, ...state } = st;
      const bytes = await B.makeBackup({ format: 'dg.backup/1', created_at: at, app_version: APP_VERSION, state, photos }, pw1.value);
      const name = `DG_B_${todayKST().replace(/-/g, '')}_${at.slice(11, 16).replace(':', '')}.dgb`;
      const file = new File([bytes], name, { type: 'application/octet-stream' });
      let sent = false;
      // 공유 창은 개인 메신저·클라우드로 바로 보낼 수 있어 단독 방식에서만 쓴다(PC 연결 방식은 내려받기만)
      if (isSolo() && navigator.canShare && navigator.canShare({ files: [file] })) {
        try { await navigator.share({ files: [file], title: '수거 업무 백업' }); sent = true; }
        catch (e) { if (e.name === 'AbortError') { msg.textContent = '백업을 취소했습니다.'; make.disabled = false; return; } }
      }
      if (!sent) { const u = URL.createObjectURL(file); const a = h('a', { href: u, download: name }); document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 30000); }
      st.lastBackupAt = at; await commit();
      pw1.value = pw2.value = '';
      ui.backupDone = true; flow.replaceWith(flow = flowSteps(BSTEPS, 2));
      msg.textContent = `백업 파일 ${name} (${(bytes.length / 1024).toFixed(0)}KB)을 만들었습니다. 업무 메일이나 승인된 저장 매체에 보관하세요.`;
    } catch (e) { msg.textContent = '백업 실패: ' + errMsg(e); }
    make.disabled = false;
  }, 'primary big');

  const fileIn = h('input', { type: 'file', accept: '.dgb,application/octet-stream' });
  const rpw = h('input', { type: 'password', placeholder: '백업 비밀번호', class: 'search', autocomplete: 'off' });
  const rmsg = h('p', { class: 'msg' });
  const restore = btn('백업에서 복원', async () => {
    const file = fileIn.files && fileIn.files[0];
    if (!file) return (rmsg.textContent = '백업 파일을 골라 주세요.');
    rmsg.textContent = '확인하는 중…';
    try {
      const obj = await B.openBackup(new Uint8Array(await file.arrayBuffer()), rpw.value);
      const n = (obj.state.requests || []).length;
      if ((obj.state.mode || 'SOLO') !== st.mode) { rmsg.textContent = '다른 사용 방식(PC 연결/단독)의 백업입니다. 같은 방식에서만 복원할 수 있습니다.'; return; }
      const when = obj.created_at.slice(0, 16).replace('T', ' ');
      const text = isSolo() ? `${when} 백업 (접수 ${n}건)으로 이 휴대폰 자료를 바꿉니다. 지금 휴대폰에만 있는 기록은 사라집니다.`
        : `${when} 백업 (기록 ${(obj.state.events || []).length}건)을 이 휴대폰 자료와 합칩니다. 지금 휴대폰의 기록·PC 반영 확인은 그대로 남고, 기록 번호는 이어서 씁니다(PC에 이미 보낸 번호를 다시 쓰지 않음).`;
      if (!(await ask(text, '복원하기', { title: '백업에서 복원' }))) { rmsg.textContent = ''; return; }
      // 저장 공간 부족 등으로 사진을 다 넣지 못하면 상태는 바꾸지 않는다(반쯤 복원된 상태로 두지 않음)
      for (const [pid, b] of Object.entries(obj.photos || {})) await S.putSecureBytes('ph:' + pid, B.unb64(b));
      await settleWrites();
      const merged = F.mergeRestore(st, { ...emptyState(), ...obj.state, mode: obj.state.mode || st.mode });
      setSt({ ...emptyState(), ...merged, lastBackupAt: obj.created_at });
      const ok = await commit();
      if (!ok) { rmsg.textContent = ERR.STORAGE_LOW; return; }
      toast(isSolo() ? `복원했습니다 (접수 ${n}건)` : `복원했습니다 (기록 ${st.events.length}건 · 다음 기록 번호 ${st.seq + 1})`); go('#/home');
    } catch (e) { rmsg.textContent = errMsg(e); }
  }, 'big');

  pw1.addEventListener('input', () => { if (pw1.value.length >= 8) flow.replaceWith(flow = flowSteps(BSTEPS, 1)); });
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '백업·복원')),
    flow,
    isSolo() ? null : h('section', { class: 'card notice' }, h('h2', {}, 'PC 연결 방식의 안전용 백업'),
      h('p', { class: 'small' }, 'PC 연결 방식에서는 [결과 보내기]가 기본입니다. 이 백업은 휴대폰 고장·초기화에 대비한 예비본이며, PC는 이 파일이 아니라 결과 파일(.dgr)로 반영합니다. 받은 목록(주민 정보)이 함께 들어가므로 승인된 보안 매체에만 보관하세요.')),
    h('section', { class: 'card' }, h('h2', {}, h('span', { class: 'stepno' }, '1'), '백업 만들기'),
      h('p', { class: 'small' }, `마지막 백업: ${ago(st.lastBackupAt)} · ` + (isSolo() ? `접수 ${st.requests.length}건` : `목록 ${pcStops().length}곳 · 기록 ${st.events.length}건`) + ` · 사진 ${photoIds.length}장`),
      h('p', { class: 'small' }, '백업 파일은 백업 비밀번호로 암호화됩니다. 비밀번호를 잊으면 복원할 수 없으니 기관 기준에 따라 따로 보관하세요. 개인 메일·메신저·공개 클라우드에는 올리지 마세요.'),
      pw1, pw2, h('label', { class: 'check' }, withPhotos, ' 사진 포함'), msg),
    isSolo() ? h('section', { class: 'card' }, h('h2', {}, 'PC로 옮기기'), h('p', { class: 'small' }, '나중에 PC 프로그램을 쓰게 되면, 이 백업 파일을 승인된 보안 매체로 PC에 옮긴 뒤 PC [결과 가져오기 → 휴대폰 단독 방식 자료 옮기기]에서 백업 비밀번호로 열면 접수·현장 기록·사진·하차 기록이 PC 원본으로 들어갑니다. 이미 PC에 있는 접수는 덮어쓰지 않습니다.')) : null,
    h('section', { class: 'card' }, h('h2', {}, isSolo() ? '복원 (새 휴대폰으로 옮길 때)' : '복원 (이 휴대폰 자료가 지워졌을 때)'), fileIn, rpw, restore, rmsg),
    footBar(make));
}

// ------------------------------------------------------------ 지도 (온라인 바탕 지도, 개인정보는 보내지 않음)

export {
  screenBackup,
};
