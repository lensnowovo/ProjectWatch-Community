/** Relevance-first, de-duplicated lists for the competition and industry tabs.
 *
 * Long report lists are mostly reposts and items the triage itself judged unrelated
 * ("与已知计划无直接关联"). Those fold away; what the company should look at comes first. */

const IMPORTANCE_RANK: Record<string, number> = { major: 0, notable: 1, routine: 2 };
/** Phrases the triage uses when an item has no bearing on the company. */
export const LOW_RELEVANCE = /无直接关联|没有直接关联|不直接相关|关联(?:度)?有限|关系不大|与本公司无关|无明显关联/;

export interface Rankable {
  title: string;
  importance?: string | null;
  impact?: string | null;
  date?: string | null;
}

export function isLowRelevance(item: Rankable): boolean {
  return item.importance === "routine" || LOW_RELEVANCE.test(item.impact ?? "");
}

const norm = (title: string) => title.toLocaleLowerCase()
  .replace(/[\s|｜·•:：,，。.、!！?？"“”'‘’()（）[\]【】《》<>\-—_/\\]+/g, "");

/** Similar wording is not proof of the same event (especially reversals and later updates). */
export function sameStory(a: string, b: string): boolean {
  const x = norm(a), y = norm(b);
  return Boolean(x && y) && x === y;
}

/** Keep the first (best-ranked) item of each story. */
export function dedupe<T extends Rankable>(items: T[]): T[] {
  const kept: T[] = [];
  for (const item of items) if (!kept.some((k) => (k.date ?? "") === (item.date ?? "") && sameStory(k.title, item.title))) kept.push(item);
  return kept;
}

/** Major → notable → routine; within a level, items with a stated bearing on the company first; then newest. */
export function rank<T extends Rankable>(items: T[]): T[] {
  return [...items].sort((a, b) =>
    (IMPORTANCE_RANK[a.importance ?? "routine"] ?? 2) - (IMPORTANCE_RANK[b.importance ?? "routine"] ?? 2)
    || Number(isLowRelevance(a)) - Number(isLowRelevance(b))
    || (b.date ?? "").localeCompare(a.date ?? ""));
}

/** Ranked, de-duplicated, split into what to read now and the folded rest. */
export function focus<T extends Rankable>(items: T[], limit: number): { top: T[]; rest: T[]; duplicates: number } {
  const unique = dedupe(rank(items));
  const relevant = unique.filter((i) => !isLowRelevance(i));
  const top = (relevant.length ? relevant : unique).slice(0, limit);
  return { top, rest: unique.filter((i) => !top.includes(i)), duplicates: items.length - unique.length };
}
