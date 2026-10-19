/** Bounded, memory-only GET cache. No persistence of project data or failed responses. */
export function createReadResource<T>(read: (key: string, signal: AbortSignal) => Promise<T>, ttl = 60_000, timeout = 30_000) {
  type Entry = { data: T | null; error: unknown; loading: boolean; updated: number; pending?: Promise<void>; listeners: Set<() => void> };
  const entries = new Map<string, Entry>();
  const entry = (key: string): Entry => {
    let e = entries.get(key);
    if (!e) {
      for (const [k, v] of entries) if (entries.size >= 40 && !v.pending && !v.listeners.size) entries.delete(k);
      e = { data: null, error: null, loading: false, updated: 0, listeners: new Set() };
      entries.set(key, e);
    }
    return e;
  };
  const snapshot = (key: string) => { const e = entry(key); return { data: e.data, error: e.error, loading: e.loading || (!e.data && !e.error) }; };
  function load(key: string, force = false): Promise<void> {
    const e = entry(key);
    if (e.pending) return e.pending;
    if (!force && e.data && Date.now() - e.updated < ttl) return Promise.resolve();
    const controller = new AbortController();
    e.loading = true; e.error = null;
    let timer: ReturnType<typeof setTimeout>;
    const expired = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { controller.abort(); reject(new Error('读取超时，请重试')); }, timeout);
    });
    e.pending = Promise.race([Promise.resolve().then(() => read(key, controller.signal)), expired])
      .then(data => { e.data = data; e.updated = Date.now(); })
      .catch(error => { e.error = error; })
      .finally(() => { clearTimeout(timer); e.loading = false; e.pending = undefined; e.listeners.forEach(fn => fn()); });
    e.listeners.forEach(fn => fn());
    return e.pending;
  }
  return { snapshot, load, subscribe(key: string, fn: () => void) { const e = entry(key); e.listeners.add(fn); return () => { e.listeners.delete(fn); }; } };
}
