import { AnimatePresence, motion } from "motion/react";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import ShinyText from "./bits/ShinyText";
import { api, ApiError, type Category, type Fragment, type Importance, type Ref } from "./api";

export const CATEGORY_LABEL: Record<Category, string> = {
  company: "公司",
  competitor: "竞品",
  industry: "赛道",
  policy: "政策",
  paper: "论文",
  trial: "临床",
  patent: "专利",
};
export const IMPORTANCE_LABEL: Record<Importance, string> = {
  major: "重大",
  notable: "关注",
  routine: "一般",
};
export const BASIS_LABEL = { actual: "实际", forecast: "预测", budget: "预算", unknown: "口径未明" };
export const TONE_LABEL = { positive: "向好", neutral: "中性", watch: "需关注", negative: "承压" };

export function relTime(iso?: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  const diff = (Date.now() - date.getTime()) / 1000;
  // A date ahead of today (a journal's scheduled issue, a planned milestone) is labelled, not shown as recent.
  if (diff < -86400) return `预定 ${date.toLocaleDateString("zh-CN")}`;
  if (diff < 0) return date.toLocaleDateString("zh-CN");
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.round(diff / 86400)} 天前`;
  return date.toLocaleDateString("zh-CN", { year: "numeric", month: "short", day: "numeric" });
}

/** When an item can count as news: its date, but never later than when it was collected (a paper's
 * scheduled issue date or a planned milestone is not "the latest"). */
export function seenAt(item: { published_at?: string | null; collected_at: string }): string {
  const published = item.published_at ?? item.collected_at;
  return published > item.collected_at ? item.collected_at : published;
}
export const byNewest = <T extends { published_at?: string | null; collected_at: string }>(a: T, b: T) => seenAt(b).localeCompare(seenAt(a));

export function dayLabel(iso?: string | null): string {
  if (!iso) return "日期未知";
  const date = new Date(iso);
  const today = new Date();
  const start = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((start(today) - start(date)) / 86400000);
  if (days < -1) return `预定 ${date.toLocaleDateString("zh-CN", { month: "long", day: "numeric" })}`;
  if (days === 0) return "今天";
  if (days === 1) return "昨天";
  return date.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "short" });
}

export function formatNumber(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

export function ImportanceDot({ level }: { level: Importance }) {
  return <span className={`dot dot-${level}`} title={IMPORTANCE_LABEL[level]} />;
}

export function Tag({ children, tone = "plain" }: { children: ReactNode; tone?: string }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" />
      {label && <span>{label}</span>}
    </span>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      {children && <div className="empty-body">{children}</div>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}

export function errorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error ?? "");
  if (/validation errors? for|errors\.pydantic\.dev|\[type=(?:literal_error|string_pattern_mismatch|model_type|missing)/i.test(message)) {
    return "部分字段未能正确识别，已有结果保留。请重试。";
  }
  if (message.startsWith("内部错误")) return "处理失败，已有结果保留。请重试。";
  if (/^interrupted\b/i.test(message.trim())) return "任务中断：有请求没有返回明确结果（或重试已达上限），为避免重复调用已停止，已有结果保留。";
  const code = message.trim().split(/[\s:：]/)[0];
  if (/^[a-z]+(?:_[a-z]+)+$/.test(code)) return CODE_MESSAGE[code] ?? (code.startsWith("commercial_") ? CODE_MESSAGE.commercial : "处理失败，已有结果保留。请稍后重试。");
  return message;
}

/** Backend error codes shown in plain language; the raw code never reaches the page. */
const CODE_MESSAGE: Record<string, string> = {
  request_outcome_unknown: "上次请求没有返回明确结果，更新已暂停。",
  request_budget: "本次更新的请求次数已达上限，已有内容保留。",
  provider_backoff: "服务方暂时限流，稍后会自动继续。",
  connection_failed: "无法连接服务，请检查网络后重试。",
  http_failure: "外部服务返回错误，已有结果保留。",
  response_too_large: "返回内容过大，已跳过。",
  invalid_json: "服务返回的内容无法识别，已有结果保留。",
  disallowed_url: "该地址不允许读取。",
  search_key_missing: "检索服务尚未配置密钥，请到设置中填写。",
  search_key_invalid: "检索服务密钥无效，请到设置中检查。",
  search_access_denied: "检索服务拒绝访问，请检查账户权限。",
  search_quota_exhausted: "检索服务额度已用完。",
  registry_error: "工商数据服务暂时不可用。",
  model_timeout: "模型响应超时，请重试。",
  model_output_truncated: "模型输出不完整，请在设置中提高单次回复上限后重试。",
  model_http_failure: "模型服务返回错误，请稍后重试。",
  model_access_denied: "模型服务拒绝访问，请检查密钥。",
  commercial: "专业来源暂时无法读取，已保留上次结果。",
};

export function ErrorNote({ error }: { error: unknown }) {
  const [retryState, setRetryState] = useState("");
  useEffect(() => setRetryState(""), [error]);
  if (!error) return null;
  const held = error instanceof ApiError && error.operationId
    && (error.operationStatus === "unknown" || error.operationStatus === "failed") ? error : null;
  const approve = async () => {
    if (!held?.operationId || !window.confirm("上次请求可能已经计费。确认允许再次执行？本次只授权，不立即调用服务。")) return;
    setRetryState("正在确认…");
    try {
      await api.retryDirectOperation(held.operationId);
      setRetryState("已允许重试，请再次点击原来的操作按钮。");
    } catch (e) { setRetryState(errorMessage(e)); }
  };
  return <div className="note note-error">{errorMessage(error)}
    {held && <div>{retryState ? <span role="status">{retryState}</span>
      : <button className="btn btn-sm" onClick={approve}>确认后允许重试</button>}</div>}
  </div>;
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    return fn()
      .then((value) => {
        setData(value);
        setError(null);
      })
      .catch(setError)
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    load();
  }, [load]);
  return { data, error, loading, reload: load, setData };
}

/* ---------- source drawer: click a reference, read the original passage ---------- */

interface DrawerState {
  ref: Ref;
}
const DrawerContext = createContext<(ref: Ref) => void>(() => undefined);
export const useOpenSource = () => useContext(DrawerContext);

export function SourceDrawerProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DrawerState | null>(null);
  const [fragments, setFragments] = useState<Fragment[] | null>(null);
  const [error, setError] = useState<unknown>(null);

  const open = useCallback((ref: Ref) => {
    setState({ ref });
    setFragments(null);
    setError(null);
    api.fragments(ref.source_id).then(setFragments).catch(setError);
  }, []);

  useEffect(() => {
    if (!fragments || !state) return;
    const timer = window.setTimeout(() => {
      document.querySelector(".frag-active")?.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 280);
    return () => window.clearTimeout(timer);
  }, [fragments, state]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setState(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <DrawerContext.Provider value={open}>
      {children}
      <Drawer open={!!state} onClose={() => setState(null)} eyebrow="原文出处"
        title={state?.ref.source_name ?? ""} subtitle={state ? locatorLabel(state.ref.locator) : undefined}>
        <ErrorNote error={error} />
        {!fragments && !error && <Spinner label="正在打开原文" />}
        {state && fragments?.map((f) => (
          <p key={f.id} className={`frag ${f.locator === state.ref.locator ? "frag-active" : ""}`}>
            <span className="frag-loc">{locatorLabel(f.locator)}</span>
            {f.text}
          </p>
        ))}
      </Drawer>
    </DrawerContext.Provider>
  );
}

/** Reader-facing locator: "page 5" becomes "第 5 页"; other locators stay as given. */
export function locatorLabel(locator: string): string {
  return locator.replace(/^(?:page|p\.?)\s*(\d+)(\s*OCR)?$/i, (_m, n, ocr) => `第 ${n} 页${ocr ? "（OCR）" : ""}`);
}

/** Short file label without extension; long names keep their recognisable start. */
export function sourceLabel(name: string): string {
  const bare = name.replace(/\.[a-z0-9]+$/i, "");
  return bare.length > 14 ? `${bare.slice(0, 12)}…` : bare;
}

export function RefChips({ refs, compact = false }: { refs: Ref[]; compact?: boolean }) {
  const open = useOpenSource();
  if (!refs?.length) return null;
  const shown = refs.slice(0, 3);
  return (
    <span className={`refs${compact ? " refs-compact" : ""}`}>
      {shown.map((r, i) => (
        r.url ? <a key={i} className="ref" href={r.url} target="_blank" rel="noreferrer noopener" aria-label={compact ? `查看来源：${r.source_name}` : undefined} title={`${r.source_name} · ${r.read_scope === "summary" ? "搜索摘录" : "Agent 读取"}`}>{compact ? `[${i + 1}]` : <>{sourceLabel(r.source_name)} ↗</>}</a>
        : <button key={i} className="ref" onClick={() => open(r)} aria-label={compact ? `查看来源：${r.source_name} · ${locatorLabel(r.locator)}` : undefined} title={`${r.source_name} · ${locatorLabel(r.locator)}`}>
          {compact ? `[${i + 1}]` : i > 0 && shown[i - 1].source_name === r.source_name ? locatorLabel(r.locator)
            : <>{sourceLabel(r.source_name)} · {locatorLabel(r.locator)}</>}
        </button>
      ))}
    </span>
  );
}

export function Drawer({ open, onClose, eyebrow, title, subtitle, children }: {
  open: boolean; onClose: () => void; eyebrow: string; title: string; subtitle?: string; children: ReactNode;
}) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="drawer-backdrop" onClick={onClose}
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}>
          <motion.aside className="drawer" onClick={(e) => e.stopPropagation()} aria-label={title}
            initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }}
            transition={{ type: "spring", stiffness: 380, damping: 40 }}>
            <header className="drawer-head">
              <div>
                <div className="eyebrow">{eyebrow}</div>
                <div className="drawer-title">{title}</div>
                {subtitle && <div className="muted small">{subtitle}</div>}
              </div>
              <button className="icon-btn" onClick={onClose} aria-label="关闭">×</button>
            </header>
            <div className="drawer-body">{children}</div>
          </motion.aside>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/** Status line for background work: a quiet shimmer instead of a spinner. */
export function Working({ text }: { text: string }) {
  return <ShinyText text={text} speed={2.4} className="working" color="var(--muted)" shineColor="var(--ink)" spread={110} />;
}

/** Amounts in 万元 switch to 亿元 above 1 亿 so a laptop-width tile stays readable. */
export function formatAmount(value: number | null | undefined, unit: string): { value: string; unit: string } {
  if (value === null || value === undefined) return { value: "—", unit: "" };
  if (unit === "万元" && Math.abs(value) >= 10000) {
    return { value: (value / 10000).toLocaleString("zh-CN", { maximumFractionDigits: 2 }), unit: "亿元" };
  }
  return { value: value.toLocaleString("zh-CN", { maximumFractionDigits: unit === "%" ? 1 : 0 }), unit };
}

/** Links to public evidence (news, search results); internal-material citations show as plain text. */
export function EvidenceLinks({ refs }: { refs?: { id: string; title: string; url: string; source: string; date?: string; quote?: string; source_id?: string; source_name?: string; locator?: string }[] }) {
  const open = useOpenSource();
  if (!refs?.length) return null;
  return (
    <span className="evidence-links">
      {refs.slice(0, 3).map((r) => r.url
        ? <a key={r.id} href={r.url} target="_blank" rel="noreferrer noopener" title={`${r.title}${r.date ? ` · ${r.date}` : ""}\n${r.quote || ""}`}>{r.source || "来源"} ↗</a>
        : r.source_id ? <button className="ref" key={r.id} title={r.quote} onClick={() => open({source_id:r.source_id!,source_name:r.source_name || "内部资料",locator:r.locator || ""})}>内部资料 ↗</button>
        : <span key={r.id} className="evidence-internal">{r.source || "内部资料"}</span>)}
    </span>
  );
}
