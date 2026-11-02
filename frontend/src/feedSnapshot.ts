import type { Item } from './api';

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const row = value as Record<string, unknown>;
    return `{${Object.keys(row).sort().map(key => `${JSON.stringify(key)}:${stable(row[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

/** Same-ID corrections and removals matter; response property ordering does not. */
export function feedChanged(current: Item[], next: Item[]): boolean {
  if (current.length !== next.length) return true;
  const previous = new Map(current.map(item => [item.id, stable(item)]));
  return next.some(item => previous.get(item.id) !== stable(item));
}
