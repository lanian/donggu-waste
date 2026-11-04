// PC 화면: QR 실측
import * as M from '/web/map.js';
import { api, boot, btn, chip, grid, h, pagehead, post, toast, wrap } from '../core.js';

async function screenQrTest() {
  const devs = boot.devices.filter((d) => d.status === 'ACTIVE');
  const sel = (opts, def) => h('select', {}, ...opts.map(([v, t]) => h('option', { value: v, selected: v === def }, t)));
  const F = { device: sel(devs.map((d) => [d.id, `${d.id.slice(0, 8)} ${d.label || ''}`]), devs[0] && devs[0].id), count: sel([['20', '20건'], ['50', '50건'], ['100', '100건']], '20'),
    addr: sel([['mixed', '섞음'], ['short', '짧은 주소'], ['medium', '보통(동·호)'], ['long', '긴 주소(설명 포함)']], 'mixed'),
    chunk: sel([['400', '400B'], ['600', '600B'], ['800', '800B'], ['1200', '1,200B']], '800'), ec: sel([['L', 'L (용량 큼)'], ['M', 'M'], ['Q', 'Q (복원력 큼)']], 'M'),
    fps: sel([['2', '초당 2장'], ['3', '초당 3장'], ['4', '초당 4장'], ['6', '초당 6장']], '3') };
  const msg = h('p', { class: 'msg' });
  const tbody = h('tbody');
  const load = wrap(async () => {
    const list = await api('qrtests');
    tbody.replaceChildren(...list.map((t) => {
      const el = h('input', { type: 'number', step: '0.1', placeholder: '초', style: 'width:80px', value: t.elapsed_s ?? '' });
      const rd = h('input', { type: 'number', placeholder: '인식 횟수', style: 'width:90px', value: t.reads ?? '' });
      const ck = h('input', { placeholder: '확인 4자리', style: 'width:100px' });
      return h('tr', {}, h('td', {}, t.created_at.replace('T', ' ').slice(5, 16)), h('td', {}, `${t.params.count}건 · ${t.params.addr}`), h('td', {}, `${t.params.chunk}B · ${t.params.ec} · ${t.params.fps}장/초`),
        h('td', {}, `${t.bytes.toLocaleString()}B · ${t.frames}장`), h('td', {}, t.verified == null ? '' : t.verified ? chip(`${t.elapsed_s}초 · 인식 ${t.reads}회 · 복원 일치`, 'ok') : chip('확인 코드 불일치', 'bad')),
        h('td', {}, btn('QR 다시 표시', () => window.open('/qrtest/' + t.id.slice(0, 8), 'dg-qrtest', 'width=760,height=900'), 'small'), ' ', el, rd, ck,
          btn('기록', wrap(async () => { const r = await post(`qrtest/${t.id}/result`, { elapsed_s: el.value, reads: rd.value, check: ck.value }); toast(r.verified ? '기록했습니다 (복원 일치)' : '확인 코드가 맞지 않습니다', !r.verified); load(); }), 'small primary')));
    }));
  });
  load();
  const run = wrap(async () => {
    const r = await post('qrtest', { device_id: F.device.value, count: +F.count.value, addr: F.addr.value, chunk: +F.chunk.value, ec: F.ec.value, fps: +F.fps.value });
    window.open('/qrtest/' + r.test_id.slice(0, 8), 'dg-qrtest', 'width=760,height=900');
    toast(`실측 QR ${r.frames}장 (${r.bytes.toLocaleString()}B) 표시`); load();
  }, msg);
  const lab = (t, el) => h('label', { class: 'f' }, t, el);
  return h('div', {}, pagehead('QR 실측'),
    h('div', { class: 'card' }, h('p', { class: 'muted' }, '기관 휴대폰으로 QR 전송 시간과 인식률을 잽니다. 가상 자료만 쓰며 업무 자료에 섞이지 않습니다. 휴대폰은 [오늘 목록 받기]로 읽으면 "실측 모드"로 결과만 보여 주고 저장하지 않습니다.'),
      devs.length ? h('div', { class: 'row' }, lab('휴대폰', F.device), lab('건수', F.count), lab('주소 길이', F.addr), lab('조각 크기', F.chunk), lab('오류 복원', F.ec), lab('표시 속도', F.fps), btn('실측 QR 표시', run, 'primary'))
        : h('p', { class: 'err' }, '설정에서 휴대폰을 먼저 등록하세요.'), msg),
    h('div', { class: 'card tablecard' }, h('table', { class: 'grid' }, h('thead', {}, h('tr', {}, ...['시각', '자료', '조건', '크기', '결과', '휴대폰 결과 입력'].map((x) => h('th', {}, x)))), tbody)),
    h('p', { class: 'muted' }, '휴대폰에 나온 시간·인식 횟수·확인 4자리를 입력하세요. 확인 4자리가 맞아야 전체 복원이 정확했다는 뜻입니다. 같은 조건을 10회 반복해 평균을 비교하세요.'));
}

// ------------------------------------------------------------ 설정 (탭: 기본·수거팀·기기·행정동·한도·휴무일·수수료/감면·주소 자료·백업·개인정보)

export {
  screenQrTest,
};
