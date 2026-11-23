import { btn, h, handoff, msgBox, say, stepFlush, toast } from '../core.js';
import { collectEntry, createWorkFile, intakeEntry, outcome, unloadEntry, workFileRanges } from '../guest-work.js';

const workList = list => list?.schema === 'dg.guest-work/1';
const newItem = (addition) => addition ? {name:'', qty:'1', collected:'1'} : {name:'', qty:'1'};

// 한 폼으로 수거·접수·하차를 기록한다. 입력 도중 잠긴 경우에도 같은 초안 ID로 다시 연다.
function editWork({body, group, draft, update, active, finish, confirm}) {
  const form = h('form', {class:'guest-work-form'}), content = h('div'), message = msgBox();
  const kind = draft.kind, values = structuredClone(draft.values);
  const title = {collection:'수거 결과',intake:'현장 접수',unload:'하차 기록'}[kind];
  const heading = h('h2', {tabindex:'-1'}, kind === 'collection' ? draft.seq+'번 수거 결과' : title);
  const connected = () => active() && form.isConnected;
  // 연속 입력을 합치고, 완료·취소는 이미 시작한 초안 쓰기 뒤에 처리한다.
  let pending = Promise.resolve(), generation = 0, closing = false;
  const flushKey = Symbol('guest-work-draft');
  const saveDraft = () => {
    if (closing) return;
    const generationAtInput = ++generation, snapshot = structuredClone(values);
    pending = pending.catch(() => {}).then(async () => {
      if (generationAtInput !== generation) return;
      await update(group.ticket, current => {
        if (current.draft && current.draft.id !== draft.id) throw new Error('다른 기록을 입력 중입니다. 배차 화면을 다시 여세요.');
        current.draft = {...draft, values:snapshot};
      });
    });
    const operation = pending;
    const clear = () => { if (pending === operation) stepFlush.delete(flushKey); };
    stepFlush.set(flushKey, () => pending);
    pending.then(clear, clear);
    pending.catch(e => { if (connected()) say(message, e.message, true); });
  };
  function input(label, model, prop, options = {}) {
    const field = h('input', {type:'text', autocomplete:'off', ...options});
    field.value = model[prop];
    field.addEventListener('input', () => { model[prop] = field.value; saveDraft(); });
    return h('label', {}, label, field);
  }
  function itemList(rows, addition) {
    const area = h('div'), cards = h('div');
    const paint = () => {
      cards.replaceChildren(...rows.map((row,i) => h('fieldset', {},
        h('legend', {}, (addition ? '추가 품목 ' : '품목 ') + (i+1)),
        input('품목명',row,'name',{maxlength:60}),
        input('품목 수량',row,'qty',{inputmode:'numeric'}),
        addition ? input('추가 품목 수거 수량',row,'collected',{inputmode:'numeric'}) : null,
        rows.length > 1 || addition ? btn('품목 삭제', () => { rows.splice(i,1); paint(); saveDraft(); }, 'ghost') : null)));
    };
    const add = btn(addition ? '현장 품목 추가' : '품목 추가', () => {
      if (rows.length >= 100) { say(message,'품목은 100개까지 추가할 수 있습니다.',true); return; }
      rows.push(newItem(addition)); paint(); saveDraft(); cards.lastElementChild.querySelector('input').focus();
    }, 'big');
    paint(); area.append(cards, add); return area;
  }
  if (kind === 'collection') {
    const lines = h('div');
    const paint = () => lines.replaceChildren(...draft.stop.items.map((it,i) => {
      const name = [it.name,it.spec].filter(Boolean).join(' · ');
      return h('fieldset', {}, h('legend', {}, name+' · 배차 '+it.qty+it.unit),
        input(name+' 현장 확인 수량',values.confirmed,i,{inputmode:'numeric'}),
        input(name+' 수거 수량',values.qty,i,{inputmode:'numeric'}));
    }));
    paint();
    content.append(h('div', {class:'actions'}, btn('모두 수거', () => { values.qty=[...values.confirmed]; paint(); saveDraft(); }),
      btn('모두 미수거', () => { values.qty=values.qty.map(()=>'0'); paint(); saveDraft(); })),
      lines, itemList(values.additions,true), input('변동·미수거 사유',values,'reason',{maxlength:200}));
  } else if (kind === 'intake') {
    content.append(input('신청인',values,'name',{maxlength:60}), input('연락처',values,'phone',{type:'tel',maxlength:20}),
      input('배출 주소',values,'address',{maxlength:300}), input('행정동',values,'dong',{maxlength:20}), itemList(values.items,false));
  } else {
    content.append(input('하차지',values,'site',{maxlength:120}),input('하차 중량(kg)',values,'weight_kg',{inputmode:'decimal'}));
  }
  const submit = h('button', {type:'submit',class:'btn primary big'}, title+' 저장');
  const back = btn('배차로 돌아가기', async () => {
    if (closing) return;
    closing = true;
    try { await pending; if (connected()) finish(); }
    catch (e) { if (connected()) say(message,e.message,true); }
    finally { closing = false; }
  }, 'ghost big');
  const discard = btn('초안 버리기', async () => {
    if (closing || !await confirm('초안을 버릴까요?',title+' 입력 내용을 지웁니다. 저장을 마친 기록은 보관됩니다.','초안 버리기','danger')) return;
    closing = true;
    try {
      await pending.catch(() => {});
      await update(group.ticket, current => { if (current.draft?.id === draft.id) current.draft = null; });
      if (connected()) finish();
    } catch (e) { if (connected()) say(message,e.message,true); } finally { closing = false; }
  }, 'ghost big');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (closing) return;
    closing = true; submit.disabled = true;
    try {
      const entry = kind === 'collection' ? collectEntry(draft.id,draft.stop,values) :
        kind === 'intake' ? intakeEntry(draft.id,values) : unloadEntry(draft.id,values);
      await pending.catch(() => {});
      await update(group.ticket, current => {
        if (current.draft && current.draft.id !== draft.id) throw new Error('다른 초안이 열려 있습니다. 목록을 다시 여세요.');
        const index = current.entries.findIndex(e => e.id === entry.id || (kind==='collection' && e.kind===kind && e.seq===entry.seq));
        if (index < 0) current.entries.push(entry);
        else {
          if (index < current.exported || current.entries[index].id !== entry.id) throw new Error('이 방문의 결과가 이미 저장되었습니다. 목록을 다시 여세요.');
          current.entries[index] = entry;
        }
        current.draft = null;
      });
      if (connected()) { toast(title+' 저장 완료'); finish(); }
    } catch (e) { if (connected()) say(message,e.message,true); }
    finally { closing=false; submit.disabled=false; }
  });
  form.append(heading, content, message, submit, back, discard);
  body.replaceChildren(form); heading.focus();
}

export function guestWorkControls({list, groups, update, body, active, finish, confirm}) {
  const current = workList(list) ? (groups.find(g => g.ticket === list.ticket) || {
    ticket:list.ticket, return_key:list.return_key, crew:list.crew, work_date:list.work_date, issue_no:list.issue_no,
    entries:[], exported:0, draft:null, revision:0,
  }) : null;
  const start = (kind, stop, previous, group = current) => {
    if (group.draft) { resume(group); return; }
    const draft = {id:previous?.id ?? crypto.randomUUID(),kind};
    if (kind === 'collection') {
      draft.seq = stop.seq;
      draft.stop = {seq:stop.seq,items:stop.items};
      draft.values = {confirmed:previous?.confirmed.map(String) ?? stop.items.map(it=>String(it.qty)),
        qty:previous?.qty.map(String) ?? stop.items.map(()=>'0'), additions:structuredClone(previous?.additions || []), reason:previous?.reason || ''};
    } else if (kind === 'intake') draft.values = previous ?
      {name:previous.name,phone:previous.phone,address:previous.address,dong:previous.dong,items:structuredClone(previous.items)} :
      {name:'',phone:'',address:'',dong:'',items:[newItem(false)]};
    else draft.values = {site:previous?.site || '',weight_kg:previous?.weight_kg ?? ''};
    editWork({body,group,draft,update,active,finish,confirm});
  };
  function resume(group) { editWork({body,group,draft:group.draft,update,active,finish,confirm}); }
  function stopAction(stop) {
    if (!current || current.closed) return null;
    const index = current.entries.findIndex(e => e.kind==='collection' && e.seq===stop.seq);
    const previous = current.entries[index];
    return h('div', {}, previous ? h('strong',{},outcome(previous)) : null,
      previous && index < current.exported ? h('p',{class:'small'},'결과 파일에 담았습니다.') :
      btn(stop.seq+'번 수거 결과 '+(previous ? '수정':'기록'), () => start('collection',stop,previous),'big',{disabled:!!(current.draft || current.delivery)}));
  }
  const actions = current && !current.closed ? h('div', {class:'guest-work-actions'},
    btn('현장 접수 추가', () => start('intake'),'big',{disabled:!!(current.draft || current.delivery)}),
    btn('하차 기록 추가', () => start('unload'),'big',{disabled:!!(current.draft || current.delivery)})) : null;
  function history(group) {
    const msg = msgBox(), files = [];
    const ranges = workFileRanges(group.entries);
    for (const [from,end] of ranges) {
      const suffix=ranges.length>1 ? ' ('+(from+1)+'~'+end+'건)' : '';
      const earlierPending = from > group.exported;
      async function deliver(share, button) {
        if (button.disabled) return;
        button.disabled=true;
        const attempt=crypto.randomUUID();
        let reserved=false, delivered=false, failure='';
        try {
          const envelope=await createWorkFile({...group,entries:group.entries.slice(from,end)});
          if (!active() || !button.isConnected) return;
          const file=new File([JSON.stringify(envelope)],'수거결과_'+group.work_date+'_'+group.issue_no+'_'+(from+1)+'-'+end+'.dgwork',{type:'application/octet-stream'});
          if (share && !navigator.canShare?.({files:[file]})) throw new DOMException('파일 공유 불가','NotAllowedError');
          await update(group.ticket, target => {
            if (target.exported < from || JSON.stringify(target.entries.slice(from,end)) !== JSON.stringify(group.entries.slice(from,end)))
              throw new Error('기록이 바뀌었습니다. 배차 화면을 다시 여세요.');
            if (target.exported >= end) return false;
            // 실제 전달 전에는 원본만 고정한다. 브라우저가 종료돼도 같은 파일로 이어갈 수 있다.
            const reservation=target.delivery ?? {from,end,attempts:[]};
            if (reservation.from!==from || reservation.end!==end) throw new Error('먼저 전달 중인 결과 파일을 저장하세요.');
            reservation.attempts.push(attempt);
            target.delivery=reservation;
            reserved=true;
          },'prepare');
          if (!active() || !button.isConnected) throw new DOMException('화면 전환','AbortError');
          handoff();
          if (share) {
            await navigator.share({files:[file],title:'수거 결과 파일'});
          } else {
            const url=URL.createObjectURL(file), link=h('a',{href:url,download:file.name});
            document.body.append(link); link.click(); link.remove(); setTimeout(()=>URL.revokeObjectURL(url),30000);
          }
          delivered=true;
        } catch (e) {
          failure=e.name==='AbortError' ? '공유를 취소했습니다. 파일은 다시 보낼 수 있습니다.' :
            e.name==='NotAllowedError' ? '이 환경에서는 파일을 공유할 수 없습니다. [결과 파일 저장]을 사용하세요.' : e.message;
        } finally {
          try {
            if (reserved) await update(group.ticket, target => {
              const reservation=target.delivery;
              if (!reservation?.attempts.includes(attempt)) return false;
              if (delivered) {
                target.exported=Math.max(target.exported,end);
                delete target.delivery;
              } else {
                reservation.attempts=reservation.attempts.filter(id=>id!==attempt);
                if (!reservation.attempts.length) delete target.delivery;
              }
            },'settle');
            if (active() && button.isConnected) {
              if (delivered) { toast('PC에서 결과 파일을 열어 확인·반영하세요'); finish(); }
              else if (failure) say(msg,failure,true);
            }
          } catch (e) {
            if (active() && button.isConnected) say(msg,'전달 상태를 저장하지 못했습니다. 같은 파일을 다시 저장하세요. '+e.message,true);
          } finally { button.disabled=false; }
        }
      }
      files.push(btn('결과 파일 저장'+suffix,e=>deliver(false,e.currentTarget),'primary big',{disabled:earlierPending}));
      if (navigator.canShare) files.push(btn('결과 파일 공유'+suffix,e=>deliver(true,e.currentTarget),'big',{disabled:earlierPending}));
    }
    const past = group.ticket !== current?.ticket;
    return h('section',{class:'card guest-work-log'},h('h3',{},past ? group.crew+' · '+group.work_date+' 현장 기록' : '이 배차의 현장 기록'),
      group.draft ? btn('입력 중인 '+({collection:'수거 결과',intake:'현장 접수',unload:'하차 기록'}[group.draft.kind])+' 이어쓰기',()=>resume(group),'primary big') : null,
      group.entries.length ? h('p',{},'저장 '+group.entries.length+'건 · 파일에 담은 기록 '+group.exported+'건') : null,
      group.delivery ? h('p',{class:'small',role:'status'},'전달을 마치려면 같은 결과 파일을 다시 저장하거나 공유하세요.') : null,
      group.entries.length ? h('ul',{},...group.entries.map((entry,i) => {
        if (entry.kind === 'collection') return h('li',{},entry.seq+'번 · '+outcome(entry));
        const isIntake = entry.kind === 'intake', label = isIntake ? '현장 접수' : '하차 기록';
        const detail = isIntake ? entry.address+' · '+entry.items.map(it=>it.name+' '+it.qty+'개').join(', ') :
          entry.site+' · '+entry.weight_kg+'kg';
        return h('li',{},label+' · '+detail, i>=group.exported && !group.draft && !group.delivery ?
          btn(label+' 수정',()=>start(entry.kind,null,entry,group),'ghost') : null);
      })) : null,
      ...files, group.entries.length ? h('p',{class:'small'},'파일을 PC 담당자에게 전달하세요. PC의 [결과 가져오기 → 현장 업무 결과 확인]에서 반영합니다.') : null,
      group.entries.length && group.exported===group.entries.length && !group.draft ? btn('반영한 기록 정리', async () => {
        if (!await confirm('PC 반영을 확인했나요?',group.crew+'의 현장 기록 '+group.entries.length+'건을 이 휴대폰에서 지웁니다.','반영 확인 후 정리','danger')) return;
        try {
          await update(group.ticket, target => {
            if (target.entries.length!==group.entries.length || target.draft || target.exported!==target.entries.length)
              throw new Error('기록이 바뀌었습니다. 다시 확인하세요.');
            target.closed=true; target.entries=[]; target.exported=0; delete target.return_key;
          });
          if (active()) finish();
        } catch (e) { if (active()) say(msg,e.message,true); }
      },'ghost big') : null, msg);
  }
  return {actions, stopAction, logs:groups.filter(g=>!g.closed && (g.entries.length || g.draft)).map(history)};
}
