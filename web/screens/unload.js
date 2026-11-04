// 휴대폰 화면: 하차 기록
import * as NAV from '../nav.js';
import { addCrewEvent, btn, go, h, isSolo, st, toast, todayKST } from '../core.js';
import { NO_LOC, navBtn, unloadLoc } from '../widgets.js';

function screenUnload() {
  const list = st.events.filter((e) => e.kind === 'UNLOAD');
  const plannedLabel = st.daylist && (st.daylist.stops.find((x) => x.unload_after) || {}).unload_after;
  const uLoc = unloadLoc(plannedLabel);
  const now = new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Seoul', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date());
  const place = h('input', { type: 'text', class: 'search', value: (list.length ? list[list.length - 1].body.place : (plannedLabel && plannedLabel.label)) || '' , placeholder: '반입지 이름' });
  const time = h('input', { type: 'time', value: now });
  const vol = h('input', { type: 'number', inputmode: 'decimal', step: '0.1', min: 0, placeholder: '예: 5.5' });
  const kg = h('input', { type: 'number', inputmode: 'decimal', step: '1', min: 0, placeholder: '계근표 중량' });
  const note = h('input', { type: 'text', placeholder: '메모 (선택, 개인정보 적지 않기)' });
  const msg = h('p', { class: 'msg' });
  const fld = (t, el) => h('label', { class: 'fld' }, h('span', {}, t), el);
  return h('main', { class: 'with-dock' },
    h('header', { class: 'bar' }, btn('← 목록', () => go('#/home'), 'ghost'), h('h1', { class: 'bar-title' }, '하차 기록')),
    h('section', { class: 'card' }, fld('반입지', place), h('div', { class: 'grid2' }, fld('시각', time), fld('부피 (m³, 추정)', vol)), fld('중량 (kg)', kg), fld('메모', note),
      h('p', { class: 'small' }, isSolo() ? '이 휴대폰에 저장되고 백업 파일에 함께 들어갑니다. 부피나 중량 중 하나는 적어 주세요.' : '결과 보내기 때 PC로 함께 갑니다. 부피나 중량 중 하나는 적어 주세요.'), msg),
    h('section', { class: 'card' }, h('h2', {}, '반입지 길안내'),
      h('div', { class: 'grid2' }, navBtn('🧭 길안내', null, uLoc, NAV.navLabel('UNLOAD'), 'big'),
        btn(uLoc ? '반입지 위치 고치기' : '반입지 위치 지정', () => go('#/pick/unload'), 'opt', { disabled: !st.mapEnabled })),
      uLoc ? h('p', { class: 'small' }, (plannedLabel && plannedLabel.lat != null ? 'PC 목록의 반입지 위치' : '이 휴대폰에 지정한 반입지 위치') + '로 안내합니다. 지도 앱에는 좌표와 "반입지"만 넘깁니다.')
        : h('p', { class: 'small nav-none' }, `길안내: ${NO_LOC}`)),
    list.length ? h('section', { class: 'card' }, h('h2', {}, `오늘 하차 ${list.length}회`),
      h('ul', { class: 'list' }, ...list.map((e) => h('li', {}, `${e.body.at_time} ${e.body.place} · ${e.body.volume_m3 ? e.body.volume_m3 + 'm³' : ''} ${e.body.weight_kg ? e.body.weight_kg + 'kg' : ''} ${e.seq > st.releasedSeq ? '(미전송)' : ''}`)))) : null,
    h('nav', { class: 'dock' }, h('span', {}), h('span', {}), btn('하차 기록', async () => {
      if (!vol.value && !kg.value) return (msg.textContent = '부피나 중량 중 하나는 적어 주세요.');
      if (!place.value.trim()) return (msg.textContent = '반입지 이름을 적어 주세요.');
      await addCrewEvent('UNLOAD', { place: place.value.trim(), at_time: time.value, volume_m3: vol.value ? +vol.value : null, weight_kg: kg.value ? +kg.value : null, note: note.value.trim(), work_date: st.daylist ? st.daylist.work_date : todayKST() });
      toast('하차를 기록했습니다'); go('#/home');
    }, 'primary dock-next')));
}

// ------------------------------------------------------------ 카메라 + QR

export {
  screenUnload,
};
