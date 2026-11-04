// PC 화면: 로그인
import { api, btn, h, post, render, setBoot, wrap } from '../core.js';

async function screenLogin() {
  const st = await api('state');
  const user = h('input', { placeholder: '아이디', autocomplete: 'username', value: st.setup_needed ? 'admin' : '' });
  const dname = st.setup_needed ? h('input', { placeholder: '이름 (예: 홍길동 주무관)' }) : null;
  const pw = h('input', { type: 'password', placeholder: '비밀번호', autocomplete: st.setup_needed ? 'new-password' : 'current-password' });
  const pw2 = st.setup_needed ? h('input', { type: 'password', placeholder: '비밀번호 한 번 더', autocomplete: 'new-password' }) : null;
  const msg = h('p', { class: 'msg' });
  const go1 = wrap(async () => {
    if (st.setup_needed) {
      if (pw.value !== pw2.value) throw new Error('두 번 입력한 비밀번호가 다릅니다.');
      await post('setup', { username: user.value, display_name: dname.value, password: pw.value });
    } else await post('login', { username: user.value, password: pw.value });
    setBoot(null); render();
  }, msg);
  (pw2 || pw).addEventListener('keydown', (e) => { if (e.key === 'Enter') go1(); });
  user.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); (dname || pw).focus(); } });
  pw.addEventListener('keydown', (e) => { if (e.key === 'Enter' && pw2) { e.preventDefault(); pw2.focus(); } });
  setTimeout(() => (st.setup_needed ? dname : user).focus(), 30);
  return h('main', { class: 'login-wrap' }, h('div', { class: 'login card' }, h('h1', { class: 'sr-only' }, st.setup_needed ? '관리자 계정 만들기' : '로그인'),
    h('div', { class: 'brand' }, h('span', { class: 'logo' }, '동'), h('div', {}, h('strong', {}, '동구 대형폐기물 관리시스템'), h('small', {}, '접수·배차·수거 업무 (PC)'))),
    h('p', { class: 'muted' }, st.setup_needed ? '처음 사용합니다. 운영 관리자 계정을 만드세요. 비밀번호는 10자 이상입니다.' : '아이디와 비밀번호를 입력하세요.'),
    h('label', { class: 'f' }, '아이디', user), dname && h('label', { class: 'f' }, '이름', dname), h('label', { class: 'f' }, '비밀번호', pw), pw2 && h('label', { class: 'f' }, '비밀번호 확인', pw2),
    btn(st.setup_needed ? '관리자 계정 만들기' : '로그인', go1, 'primary block'), msg,
    h('p', { class: 'muted small' }, '이 화면은 이 PC 안에서만 열립니다. 자료는 암호화된 DB에 저장되고, 모든 변경은 작업자와 함께 기록됩니다.')));
}

// ------------------------------------------------------------ 내 계정 (비밀번호 변경)

export {
  screenLogin,
};
