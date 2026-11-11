/** UI availability only. Saved documents, sources and backend receipts remain intact. */
export const INTERNAL_INFORMATION_VISIBLE = false;
export const INTERNAL_TABS = new Set(['ops', 'business', 'numbers', 'fin', 'risks', 'documents']);
export function visibleProjectTab<T extends string>(tab: T, fallback: T): T {
  return !INTERNAL_INFORMATION_VISIBLE && INTERNAL_TABS.has(tab) ? fallback : tab;
}
