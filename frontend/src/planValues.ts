/** Missing input is not a measured zero. Commas may group thousands, not replace digits. */
export function parseMetricValue(value: string): number | null {
  const raw = value.trim();
  if (!/^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$/.test(raw)) return null;
  const number = Number(raw.replace(/,/g, ""));
  return Number.isFinite(number) ? number : null;
}
