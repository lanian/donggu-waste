// 한국 날짜 기준의 조회 도우미. 예정일/배정 등 업무 자료는 변경하지 않는다.
export const kstDay = () => new Date(Date.now() + 9 * 3600000).toISOString().slice(0, 10);
export function moveDay(day, offset) {
  const value = new Date((day || kstDay()) + 'T12:00:00Z');
  value.setUTCDate(value.getUTCDate() + offset);
  return value.toISOString().slice(0, 10);
}
export function presetRange(preset) {
  const today = kstDay();
  if (preset === 'all') return ['', ''];
  if (preset === 'month') return [today.slice(0, 8) + '01', today];
  const day = preset === 'yesterday' ? moveDay(today, -1) : today;
  return [day, day];
}
