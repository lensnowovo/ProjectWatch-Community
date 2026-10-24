import type { ProjectDetail } from "./api";

// Exact placeholders only; short or unfamiliar real names are not rejected.
const PLACEHOLDERS = new Set(["未知", "暂无", "无", "不详", "未披露", "待补充", "待确认", "未知公司",
  "unknown", "none", "null", "n/a", "na", "tbd", "-", "—", "–", "暂无竞对", "暂无竞品"]);
export function companyName(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const name = value.normalize("NFKC").toLocaleLowerCase().replace(/\s/g, "");
  return !!name && !PLACEHOLDERS.has(name);
}

// A bilingual display suffix is not a separate company. Do not strip Chinese
// regions, legal suffixes or use fuzzy matches: subsidiaries can be distinct.
export function peerDisplayKey(name: string): string {
  return name.normalize("NFKC").replace(/\([A-Za-z][A-Za-z0-9 .&_-]*\)/g, "")
    .trim().toLocaleLowerCase().replace(/\s+/g, " ");
}

export function competitionIdentity(detail: ProjectDetail) {
  const owners = new Map<string, Set<string>>();
  const claim = (name: string, id?: string) => {
    if (!id || !name) return;
    const key = peerDisplayKey(name);
    owners.set(key, new Set([...(owners.get(key) ?? []), id]));
  };
  for (const row of detail.competition_research?.candidates ?? []) claim(row.name, row.id);
  for (const row of [...(detail.relationships?.items ?? []), ...(detail.relationships?.exclusions ?? [])])
    for (const name of [row.peer_name, ...(row.peer_names ?? [])]) claim(name, row.peer_id);
  return (name: string, id?: string): string => {
    if (id) return `id:${id}`;
    const normalized = peerDisplayKey(name);
    const ids = owners.get(normalized);
    return ids?.size === 1 ? `id:${[...ids][0]}` : `name:${normalized}`;
  };
}
