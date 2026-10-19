import { AnimatePresence, motion, useReducedMotion, type Variants } from "motion/react";
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode,
} from "react";
import { useNavigate } from "react-router-dom";
import type { Project } from "./api";

/* ---------------------------------------------------------------- easing */

export const EASE = [0.22, 1, 0.36, 1] as const;

export const listVariants: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.035, delayChildren: 0.04 } },
};
export const itemVariants: Variants = {
  hidden: { opacity: 0, y: 8 },
  show: { opacity: 1, y: 0, transition: { duration: 0.42, ease: EASE } },
};

export function Page({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <motion.div
      className={`page ${className}`}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: 0.36, ease: EASE }}
    >
      {children}
    </motion.div>
  );
}

export function Reveal({ children, delay = 0, className = "", as = "div" }: {
  children: ReactNode; delay?: number; className?: string; as?: "div" | "section";
}) {
  const Tag = as === "section" ? motion.section : motion.div;
  return (
    <Tag
      className={className}
      initial={{ opacity: 0, y: 8 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "0px 0px -24px 0px" }}
      transition={{ duration: 0.42, ease: EASE, delay }}
    >
      {children}
    </Tag>
  );
}

/* ---------------------------------------------------------------- theme */

type ThemeChoice = "light" | "dark" | "system";
const THEME_KEY = "pw-theme";

export function readTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(choice: ThemeChoice) {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

const THEME_EVENT = "pw:theme";

/** Several toggles (sidebar, settings) stay in step through one window event. */
export function ThemeToggle({ pill = "theme-pill" }: { pill?: string }) {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme);
  useEffect(() => {
    const sync = (e: Event) => setChoice((e as CustomEvent<ThemeChoice>).detail);
    window.addEventListener(THEME_EVENT, sync);
    return () => window.removeEventListener(THEME_EVENT, sync);
  }, []);
  useEffect(() => {
    applyTheme(choice);
    try {
      if (choice === "system") localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, choice);
    } catch {
      /* storage unavailable: theme still applies for this session */
    }
  }, [choice]);
  const pick = (next: ThemeChoice) => {
    setChoice(next);
    window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: next }));
  };
  const options: { id: ThemeChoice; label: string }[] = [
    { id: "light", label: "浅色" },
    { id: "dark", label: "深色" },
    { id: "system", label: "跟随系统" },
  ];
  return (
    <div className="theme-toggle" role="radiogroup" aria-label="外观">
      {options.map((o) => (
        <button key={o.id} role="radio" aria-checked={choice === o.id}
          className={choice === o.id ? "on" : ""} onClick={() => pick(o.id)}>
          {choice === o.id && <motion.span layoutId={pill} className="theme-pill" transition={{ type: "spring", stiffness: 500, damping: 38 }} />}
          <span className="theme-label">{o.label}</span>
        </button>
      ))}
    </div>
  );
}

/* ---------------------------------------------------------------- toasts */

interface Toast { id: number; text: string; tone: "ok" | "error" | "info" }
const ToastContext = createContext<(text: string, tone?: Toast["tone"]) => void>(() => undefined);
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);
  const push = useCallback((text: string, tone: Toast["tone"] = "ok") => {
    const id = ++seq.current;
    setToasts((t) => [...t, { id, text, tone }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3200);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        <AnimatePresence>
          {toasts.map((t) => (
            <motion.div key={t.id} className={`toast toast-${t.tone}`} layout
              initial={{ opacity: 0, y: 16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, transition: { duration: 0.2 } }} transition={{ duration: 0.3, ease: EASE }}>
              {t.text}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastContext.Provider>
  );
}

/* ---------------------------------------------------------------- command palette */

interface Command { id: string; label: string; hint?: string; run: () => void }

export function CommandPalette({ projects }: { projects: Project[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const navigate = useNavigate();
  const reduce = useReducedMotion();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
      } else if (e.key === "Escape") setOpen(false);
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener("pw:command", onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pw:command", onOpen);
    };
  }, []);
  useEffect(() => { if (open) { setQuery(""); setActive(0); } }, [open]);

  const commands = useMemo<Command[]>(() => {
    const go = (path: string) => () => { navigate(path); setOpen(false); };
    const base: Command[] = [
      { id: "new", label: "添加项目", hint: "输入公司名或上传 BP", run: go("/new") },
      { id: "home", label: "项目监控首页", run: go("/") },
      { id: "feed", label: "查看动态", run: go("/feed") },
      { id: "projects", label: "全部项目", run: go("/projects") },
      { id: "settings", label: "设置", hint: "模型、搜索服务、代理", run: go("/settings") },
    ];
    const items = projects.map((p) => ({
      id: p.id, label: p.name, hint: [p.legal_name !== p.name ? p.legal_name : "", p.industry].filter(Boolean).join(" · "),
      run: go(`/projects/${p.id}`),
    }));
    const q = query.trim().toLowerCase();
    const all = [...items, ...base];
    return q ? all.filter((c) => `${c.label} ${c.hint ?? ""}`.toLowerCase().includes(q)) : [...base.slice(0, 1), ...items, ...base.slice(1)];
  }, [projects, query, navigate]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div className="palette-backdrop" onClick={() => setOpen(false)}
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.18 }}>
          <motion.div className="palette" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="快速跳转"
            initial={reduce ? false : { opacity: 0, y: -12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.98 }} transition={{ duration: 0.22, ease: EASE }}>
            <input autoFocus value={query} placeholder="搜索项目或操作…"
              onChange={(e) => { setQuery(e.target.value); setActive(0); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, commands.length - 1)); }
                if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
                if (e.key === "Enter") commands[active]?.run();
              }} />
            <ul>
              {commands.map((c, i) => (
                <li key={c.id} className={i === active ? "on" : ""} onMouseEnter={() => setActive(i)} onClick={c.run}>
                  {i === active && <motion.span layoutId="palette-active" className="palette-active" transition={{ type: "spring", stiffness: 600, damping: 40 }} />}
                  <span className="palette-label">{c.label}</span>
                  {c.hint && <span className="palette-hint">{c.hint}</span>}
                </li>
              ))}
              {commands.length === 0 && <li className="palette-empty">没有匹配的项目</li>}
            </ul>
            <div className="palette-foot"><kbd>↑</kbd><kbd>↓</kbd> 选择 <kbd>Enter</kbd> 打开 <kbd>Esc</kbd> 关闭</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
