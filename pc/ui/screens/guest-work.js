import { api, btn, chip, h } from '../core.js';

// 파일을 읽고 미리보는 동안 새 파일을 고르거나 취소할 수 있다. 반영 중에는 같은 선택을 유지한다.
export function guestWorkCard(active, afterApply) {
  const output = h('div'), status = h('p', { role: 'status', class: 'small' });
  const error = h('p', { role: 'alert', class: 'err' });
  const picker = h('input', { type: 'file', accept: '.dgwork', 'aria-label': '현장 업무 결과 파일',
    onchange: () => {
      const file = picker.files[0]; picker.value = '';
      if (file && !picker.disabled) preview(file);
    } });
  const card = h('section', { class: 'card', 'aria-label': '현장 업무 결과 확인' },
    h('h2', {}, '현장 업무 결과 확인'),
    h('p', { class: 'muted' }, '수거팀이 보낸 현장 업무 결과 파일(.dgwork)을 고르세요. 내용을 확인하고 반영하면 PC 업무 기록에 저장됩니다.'),
    h('label', { class: 'btnlike' }, '현장 결과 파일 고르기', picker), status, error, output);
  let generation = 0, request;
  function clear() {
    ++generation;
    request?.abort();
    status.textContent = ''; error.textContent = ''; output.replaceChildren();
  }
  const cancel = () => { clear(); picker.focus(); };
  async function preview(file) {
    clear();
    const mine = generation;
    const current = () => mine === generation && active() && card.isConnected;
    const controller = request = new AbortController();
    const cancelButton = btn('취소', cancel);
    status.textContent = '현장 결과를 확인하는 중…';
    output.append(cancelButton);
    try {
      if (!/\.dgwork$/i.test(file.name))
        throw new Error('휴대폰에서 만든 현장 업무 결과 파일(.dgwork)을 선택하세요.');
      if (file.size > 4 * 1024 * 1024) throw new Error('현장 결과 파일은 4MB까지 열 수 있습니다.');
      const raw = await file.text();
      if (!current()) return;
      let envelope;
      try { envelope = JSON.parse(raw); } catch { /* 파일 일부가 담긴 파서 오류는 표시하지 않는다. */ }
      if (envelope?.schema !== 'dg.guest-return/1' || Object.keys(envelope).length !== 4 ||
          !['ticket', 'iv', 'ciphertext'].every(k => typeof envelope[k] === 'string' && envelope[k]))
        throw new Error('현장 업무 결과 파일 형식이 맞지 않습니다. 휴대폰에서 만든 .dgwork 파일을 선택하세요.');
      const result = await api('guest-work-preview', { method: 'POST', body: { file: envelope },
        signal: controller.signal, retry: false });
      if (!current()) return;
      const fresh = result.rows.filter(row => !row.duplicate).length;
      status.textContent = result.crew + ' · ' + result.work_date + ' · 반영 예정 ' + fresh + '건';
      const list = h('ul', { 'aria-label': '현장 기록별 반영 내용' }, ...result.rows.map(row =>
        h('li', {}, h('strong', {}, row.label), ' ', row.duplicate ? chip('이미 반영', 'neutral') : null,
          h('p', { style: 'white-space:pre-wrap' }, row.detail))));
      const apply = btn('확인한 결과 반영', async () => {
        if (!current() || picker.disabled) return;
        picker.disabled = apply.disabled = cancelButton.disabled = true;
        status.textContent = '확인한 현장 결과를 반영하는 중…';
        try {
          const saved = await api('guest-work-apply', { method: 'POST', retry: false,
            body: { file: envelope, preview_token: result.preview_token } });
          if (!current()) return;
          clear();
          status.textContent = '반영 ' + saved.applied + '건 · 이미 반영 ' + saved.duplicates + '건';
          picker.disabled = false; picker.focus();
          afterApply();
        } catch (e) {
          if (!current()) return;
          picker.disabled = cancelButton.disabled = false;
          status.textContent = '';
          error.textContent = e.message;
          output.replaceChildren(btn('다시 미리보기', () => preview(file), 'primary'), cancelButton);
          output.querySelector('button').focus();
        }
      }, 'primary');
      output.replaceChildren(list, h('div', { class: 'row' }, fresh ? apply : null, cancelButton));
    } catch (e) {
      if (!current()) return;
      status.textContent = '';
      error.textContent = e.message;
      const canRetry = e instanceof TypeError || e.status >= 500;
      output.replaceChildren(...(canRetry ? [btn('다시 미리보기', () => preview(file), 'primary')] : []), cancelButton);
    }
  }
  return card;
}
