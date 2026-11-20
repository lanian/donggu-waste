import { deriveStop, needsResult } from './fees.js';
import { hasCoord } from './nav.js';

export const TOLL_GUIDANCE = '지도 앱에서 유료도로 제외 또는 무료도로 우선 경로를 선택하세요.';

export function finalUnloadState(state, today, records = null) {
  const list = state?.daylist;
  if (state?.mode !== 'PC' || list?.work_date !== today) return null;
  const active = list.stops.filter((stop) => !stop.removed && stop.status !== 'CANCELLED');
  const last = active.at(-1);
  const site = last?.unload_after;
  if (site?.final !== true || !hasCoord(site.depot) || !Number.isSafeInteger(site.stop_count) || site.stop_count < 1) return null;

  const key = `${list.work_date}/${list.assign_ver}/${last.request_id}`;
  const missingStops = list.printed_since_seq == null ? 0 : Math.max(0, site.stop_count - active.length);
  const events = state.events || [];
  if (!records) {
    const assigned = new Set(list.stops.map((stop) => stop.request_id));
    const field = (state.fieldStops || []).filter((stop) => stop.planned_date === today && !assigned.has(stop.request_id));
    records = [...active, ...field].map((s) => ({ s, d: deriveStop(s, events) }));
  }
  const ready = !missingStops && !records.some(({ s, d }) => needsResult(s, d));
  let hasLoad = false;
  if (ready) {
    const lastUnload = [...events].reverse().find((event) => event.kind === 'UNLOAD' && event.body?.work_date === today);
    const before = lastUnload ? events.filter((event) => event.seq <= lastUnload.seq) : [];
    hasLoad = records.some(({ s, d }) => {
      if (s.removed || s.status === 'CANCELLED') return false;
      const previous = new Map(deriveStop(s, before).lines.map((line) => [line.line_id, line.qty_collected]));
      return d.lines.some((line) => line.qty_collected > (previous.get(line.line_id) || 0));
    });
  }
  const recorded = state.finalUnloadReceipt === key && !hasLoad;
  return { key, site, depot: site.depot, ready, recorded, hasLoad, missingStops };
}
