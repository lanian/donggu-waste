// 인쇄 버튼 (CSP 때문에 인라인 스크립트 대신 파일로)
document.getElementById('print')?.addEventListener('click', () => window.print());

const batches = document.getElementById('label-batches');
if (batches) {
  const size = Number(batches.dataset.size), total = Number(batches.dataset.count);
  const previous = document.getElementById('label-prev'), next = document.getElementById('label-next');
  const sheets = document.getElementById('label-sheets');
  const templates = document.querySelectorAll('template[data-count]');
  let start = 0;
  function showBatch(offset) {
    const end = Math.min(offset + size, total);
    const pages = document.createDocumentFragment();
    let itemStart = 0, sheet;
    for (const template of templates) {
      const itemEnd = itemStart + Number(template.dataset.count);
      for (let position = Math.max(offset, itemStart); position < Math.min(end, itemEnd); position++) {
        if ((position - offset) % 6 === 0) {
          sheet = document.createElement('div');
          sheet.className = 'sheet';
          pages.append(sheet);
        }
        const label = template.content.firstElementChild.cloneNode(true);
        const number = Number(template.dataset.first) + position - itemStart;
        label.querySelector('.lh span').textContent = `${number}/${template.dataset.total}`;
        sheet.append(label);
      }
      itemStart = itemEnd;
      if (itemStart >= end) break;
    }
    sheets.replaceChildren(pages);
    start = offset;
    previous.disabled = start === 0;
    next.disabled = end === total;
    document.getElementById('label-range').textContent = `전체 ${total.toLocaleString('ko-KR')}장 중 ${(start + 1).toLocaleString('ko-KR')}~${end.toLocaleString('ko-KR')}장`;
  }
  previous.addEventListener('click', () => showBatch(start - size));
  next.addEventListener('click', () => showBatch(start + size));
}
