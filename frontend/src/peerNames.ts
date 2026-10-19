// Exact placeholders only; short or unfamiliar real names are not rejected.
const PLACEHOLDERS = new Set(["未知", "暂无", "无", "不详", "未披露", "待补充", "待确认", "未知公司",
  "unknown", "none", "null", "n/a", "na", "tbd", "-", "—", "–", "暂无竞对", "暂无竞品"]);
export function companyName(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const name = value.normalize("NFKC").toLocaleLowerCase().replace(/\s/g, "");
  return !!name && !PLACEHOLDERS.has(name);
}
