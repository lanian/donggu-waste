import { deriveStop, needsResult } from './fees.js';
import { hasCoord } from './nav.js';

export const TOLL_GUIDANCE = '지도 앱에서 유료도로 제외 또는 무료도로 우선 경로를 선택하세요.';

function beforeUnload(events, today, pendingSeq) {
  const lastUnload = [...events].reverse().find((event) => event.kind === 'UNLOAD' && event.body?.work_date === today && event.seq !== pendingSeq);
  return lastUnload ? events.filter((event) => event.seq <= lastUnload.seq) : [];
}

function loadedSince(stop, derived, before) {
  const previous = new Map(deriveStop(stop, before).lines.map((line) => [line.line_id, line.qty_collected]));
  return derived.lines.some((line) => line.qty_collected > (previous.get(line.line_id) || 0));
}

function carriedLoadDue(list, events, pendingSeq) {
  const since = list.carry_since_seq;
  return !!(list.carry_load && Number.isSafeInteger(since) && !events.some((event) =>
    event.kind === 'UNLOAD' && event.assign_ver === list.assign_ver && event.body?.work_date === list.work_date
      && event.seq > since && event.seq !== pendingSeq));
}

export function intermediateUnloadState(state, today, orderedStops, pendingSeq) {
  const list = state.daylist;
  if (state.mode !== 'PC' || list?.work_date !== today) return null;
  if (!list.stops.some((stop) => stop.unload_after && stop.unload_after.final !== true)) return null;
  const active = orderedStops.filter((s) => !s.removed && !s.pii_purged && s.status !== 'CANCELLED'
    && (!s.field || s.planned_date === today));
  const lastIntermediate = list.printed_since_seq == null ? active.length - 1 : active.length;
  const events = state.events || [];
  const before = beforeUnload(events, today, pendingSeq);
  let hasLoad = carriedLoadDue(list, events, pendingSeq);
  for (const stop of active.slice(0, lastIntermediate)) {
    const derived = deriveStop(stop, events);
    if (needsResult(stop, derived)) return null;
    hasLoad ||= loadedSince(stop, derived, before);
    if (hasLoad && stop.unload_after && stop.unload_after.final !== true) {
      return { key: `${today}/${list.assign_ver}/${stop.request_id}`, stop, site: stop.unload_after };
    }
  }
  return null;
}

export function finalUnloadState(state, today, records = null) {
  const list = state?.daylist;
  if (state?.mode !== 'PC' || list?.work_date !== today) return null;
  const active = list.stops.filter((stop) => !stop.removed && stop.status !== 'CANCELLED');
  const last = active.at(-1);
  const site = list.final_unload || last?.unload_after;
  if (site?.final !== true || !hasCoord(site.depot) || !Number.isSafeInteger(site.stop_count) || site.stop_count < (list.final_unload ? 0 : 1)) return null;

  const key = `${list.work_date}/${list.assign_ver}/${last?.request_id || 'carry'}`;
  const missingStops = list.printed_since_seq == null ? 0 : Math.max(0, site.stop_count - active.length);
  const events = state.events || [];
  if (!records) {
    const assigned = new Set(list.stops.map((stop) => stop.request_id));
    const field = (state.fieldStops || []).filter((stop) => stop.planned_date === today && !assigned.has(stop.request_id));
    records = [...active, ...field].map((s) => ({ s, d: deriveStop(s, events) }));
  }
  const ready = !missingStops && !records.some(({ s, d }) => needsResult(s, d));
  const before = ready ? beforeUnload(events, today) : [];
  const hasLoad = ready && (carriedLoadDue(list, events) || records.some(({ s, d }) =>
    !s.removed && s.status !== 'CANCELLED' && loadedSince(s, d, before)));
  const recorded = state.finalUnloadReceipt === key && !hasLoad;
  return { key, site, depot: site.depot, ready, recorded, hasLoad, missingStops };
}
