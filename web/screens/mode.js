// 휴대폰 화면: 사용 방식 선택
import { appLogo, ask, btn, commit, go, h, render, st } from '../core.js';

function screenMode() {
  const pick = async (mode) => {
    if (mode === 'SOLO' && !(await ask('이 휴대폰만으로 쓰면 접수 원본이 이 휴대폰에만 저장됩니다. 분실·고장·데이터 삭제 시 원본까지 사라지므로 매일 백업 파일을 만들어 안전한 곳에 보관해야 합니다.', '이 휴대폰만으로 시작', { title: '이 휴대폰만으로 사용' }))) return;
    st.mode = mode; await commit(); go('#/home'); render();
  };
  return h('main', {},
    h('header', { class: 'bar' }, appLogo(), h('h1', { class: 'bar-title' }, '사용 방식 선택')),
    h('section', { class: 'card mode-card' },
      h('h2', {}, 'PC와 연결해서 사용 (권장)'),
      h('p', {}, 'PC가 접수 원본을 갖고, 휴대폰은 QR로 오늘 목록을 받아 현장 기록 후 결과 파일을 보냅니다.'),
      btn('PC와 연결해서 사용', () => pick('PC'), 'primary big')),
    h('section', { class: 'card mode-card' },
      h('h2', {}, '이 휴대폰만으로 사용'),
      h('p', {}, '전화 접수 등록부터 연락·수거 기록까지 이 휴대폰에서 모두 합니다. PC가 필요 없습니다.'),
      h('p', { class: 'small' }, '원본이 이 휴대폰에만 있으므로 매일 암호화 백업이 필요합니다. 여러 휴대폰이 같은 접수를 나눠 처리할 수는 없습니다.'),
      btn('이 휴대폰만으로 사용', () => pick('SOLO'), 'big')),
    h('p', { class: 'small' }, '사용 방식은 자료가 없을 때만 설정 화면에서 바꿀 수 있습니다.'));
}

// ------------------------------------------------------------ 단독 모드 홈

export {
  screenMode,
};
