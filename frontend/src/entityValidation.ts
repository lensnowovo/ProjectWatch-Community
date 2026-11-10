export const entityFields = {
  legal_name: ["工商全称", 200], short_name: ["公司简称", 100],
  credit_code: ["统一社会信用代码", 40], website: ["官网", 300],
  founded: ["成立时间", 20], location: ["所在地", 100],
  industry: ["行业 / 赛道", 200], description: ["公司简介", 500],
  stage: ["融资阶段", 60],
} as const;

export function entityIssue(entity: Record<string, unknown>): string | null {
  if (!String(entity.legal_name ?? "").trim()) return "请填写工商全称。";
  for (const [key, [label, limit]] of Object.entries(entityFields)) {
    if (Array.from(String(entity[key] ?? "").trim()).length > limit)
      return `${label}请控制在 ${limit} 字以内。${key === 'stage' ? '只填写融资轮次，融资经过放在公司资料中。' : ''}`;
  }
  return null;
}

export function validationMessage(detail: unknown[]): string {
  for (const value of detail) {
    if (!value || typeof value !== 'object') continue;
    const row = value as {loc?: unknown[]; type?: string};
    const key = row.loc?.find(x => typeof x === 'string' && Object.prototype.hasOwnProperty.call(entityFields, x));
    if (typeof key !== 'string') continue;
    const [label, limit] = entityFields[key as keyof typeof entityFields];
    if (row.type === 'string_too_long') return `${label}请控制在 ${limit} 字以内。`;
    if (row.type === 'string_too_short' || row.type === 'missing') return `请填写${label}。`;
    return `请检查${label}的填写内容。`;
  }
  return '请检查填写的信息，修改后再提交。';
}
