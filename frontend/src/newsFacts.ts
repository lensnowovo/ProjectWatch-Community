import type { Item } from './api';

/** Collection time is never an occurrence/publication date. */
export function newsDate(item: Pick<Item, 'published_at'>): number | null {
  const value = item.published_at;
  if (!value || !/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(value)) return null;
  const day = new Date(value.slice(0, 10) + 'T00:00:00Z');
  if (!Number.isFinite(day.getTime()) || day.toISOString().slice(0, 10) !== value.slice(0, 10)) return null;
  const stamp = Date.parse(value);
  return Number.isFinite(stamp) ? stamp : null;
}

export function inNewsWindow(item: Pick<Item, 'published_at'>, days: number, now = Date.now()) {
  const stamp = newsDate(item);
  return stamp !== null && stamp <= now && stamp >= now - days * 86400_000;
}

export function newsImportance(item: Pick<Item, 'importance' | 'importance_status'>) {
  return item.importance_status === 'pending' ? 'pending' : item.importance;
}

export function newsSummary(items: Item[], now = Date.now()) {
  const recent = items.filter(item => inNewsWindow(item, 30, now));
  const pending = items.filter(item => newsImportance(item) === 'pending').length;
  const undated = items.filter(item => newsDate(item) === null).length;
  return {recent, pending, undated,
    major: recent.filter(item => newsImportance(item) === 'major').length,
    notable: recent.filter(item => newsImportance(item) === 'notable').length,
    recentPending: recent.filter(item => newsImportance(item) === 'pending').length};
}
