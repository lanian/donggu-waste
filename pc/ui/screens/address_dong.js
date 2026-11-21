// 행정동을 주소로 판별한다. 담당자가 선택한 값은 명시적으로 다시 조회할 때까지 유지한다.
export function addressDongSelection({ input, select, message, lookup, isCurrent, manual = false,
  defaultValue = '', valueOf, onUpdate, onResult }) {
  let timer, generation = 0, pending, applied, needsChoice = false, locked = manual;
  const address = () => input.value.trim();
  const notify = (text, error = false) => {
    message.className = error ? 'err small' : 'muted small';
    message.textContent = text;
  };
  const assign = (value) => {
    if (select.value !== value) { select.value = value; onUpdate?.(); }
  };
  function invalidate() {
    clearTimeout(timer);
    generation++;
    applied = pending = undefined;
    needsChoice = false;
  }
  function apply(result, text) {
    const value = result && valueOf(result);
    const option = value && [...select.options].find((o) => !o.disabled && o.value === value);
    onResult?.(result);
    needsChoice = !!(result?.outside_scope || result?.ambiguous || (result?.candidates?.length > 1 && !value));
    if (locked) {
      notify('직접 정한 행정동을 유지합니다.');
    } else if (needsChoice) {
      assign('');
      const choices = (result.candidates || []).join(', ');
      notify(result.outside_scope ? '담당 범위 밖의 주소입니다. 주소와 담당 행정동을 확인하세요.'
        : `행정동 후보가 여러 곳입니다. 직접 선택하세요.${choices ? ' 후보 ' + choices : ''}`, true);
    } else if (option) {
      assign(option.value);
      notify(`주소로 자동 선택한 행정동 · ${option.textContent.trim()}`);
    } else {
      assign(value ? '' : defaultValue);
      notify(!text ? '주소를 입력하면 행정동을 찾습니다.' : defaultValue && !value
        ? '주소에서 행정동을 찾지 못했습니다. 현재 선택한 담당 동을 확인하세요.'
        : '주소에서 행정동을 확인할 수 없습니다. 직접 선택하세요.', !!text);
    }
    applied = text;
    return locked || !needsChoice;
  }
  function detect() {
    clearTimeout(timer);
    if (!isCurrent()) return Promise.resolve(false);
    const text = address(), seq = generation;
    if (applied === text) return Promise.resolve(locked || !needsChoice);
    if (!text) return Promise.resolve(apply(null, text));
    if (pending?.text === text) return pending.task;
    const current = () => seq === generation && text === address() && isCurrent();
    const task = Promise.resolve().then(() => lookup(text)).then((result) => current() && apply(result, text))
      .catch(() => {
        if (!current()) return false;
        if (!locked) assign('');
        notify(locked ? '직접 정한 행정동을 유지합니다.' : '행정동 조회에 실패했습니다. 직접 선택하거나 주소로 다시 조회하세요.', !locked);
        return locked;
      }).finally(() => { if (pending?.task === task) pending = undefined; });
    pending = { text, task };
    return task;
  }
  input.addEventListener('input', () => {
    invalidate();
    if (!isCurrent()) return;
    onResult?.(null);
    if (!locked) assign('');
    if (!address()) { apply(null, ''); return; }
    notify(locked ? '직접 정한 행정동을 유지합니다.' : '주소에서 행정동을 확인하는 중입니다.');
    timer = setTimeout(detect, 250);
  });
  input.addEventListener('blur', detect);
  select.addEventListener('change', () => {
    if (!isCurrent()) return;
    locked = true;
    notify('직접 정한 행정동을 유지합니다.');
    onUpdate?.();
  });
  return {
    settle: () => locked ? Promise.resolve(isCurrent()) : detect(),
    isManual: () => locked,
    reset() {
      invalidate(); locked = false;
      if (select.value) assign(''); else onUpdate?.();
      return detect();
    },
    choose(result) {
      invalidate();
      if (!isCurrent()) return Promise.resolve(false);
      if (!locked) assign('');
      return result ? Promise.resolve(apply(result, address())) : detect();
    },
  };
}
