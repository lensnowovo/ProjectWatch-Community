import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Candidate, type ResolveResult, type SettingsView } from "../api";
import { EASE, Page } from "../motion";
import { ErrorNote, Spinner } from "../ui";

interface Draft {
  draft_id: string;
  filename: string;
  readable: boolean;
}

const EMPTY: Candidate = {
  legal_name: "", short_name: "", credit_code: "", website: "", founded: "", location: "",
  industry: "", description: "", stage: "", aliases: [], confidence: 0, reason: "", evidence: [],
};

export default function NewProjectPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState<1 | 2>(1);
  const [name, setName] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [uploading, setUploading] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [result, setResult] = useState<ResolveResult | null>(null);
  const [picked, setPicked] = useState<number>(0);
  const [entity, setEntity] = useState<Candidate>(EMPTY);
  const [focus, setFocus] = useState("");
  const [consent, setConsent] = useState(false);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [settings, setSettings] = useState<SettingsView | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.settings().then(setSettings).catch(() => undefined);
  }, []);

  const addFiles = async (files: FileList | File[]) => {
    setError(null);
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const draft = await api.draft(file);
        setDrafts((d) => (d.some((x) => x.draft_id === draft.draft_id) ? d : [...d, draft]));
      }
    } catch (e) {
      setError(e);
    } finally {
      setUploading(false);
    }
  };

  const resolve = async () => {
    setError(null);
    setResolving(true);
    try {
      const readable = drafts.find((d) => d.readable);
      const res = await api.resolve(name, readable?.draft_id);
      setResult(res);
      const first = res.candidates[0] ?? { ...EMPTY, legal_name: res.query };
      setPicked(res.candidates.length ? 0 : -1);
      setEntity(first);
      setStep(2);
    } catch (e) {
      setError(e);
    } finally {
      setResolving(false);
    }
  };

  const choose = (index: number) => {
    setPicked(index);
    setEntity(index >= 0 && result ? result.candidates[index] : { ...EMPTY, legal_name: result?.query ?? name });
  };

  const create = async () => {
    setError(null);
    setCreating(true);
    try {
      const source = picked < 0 ? "manual" : result?.method === "registry" ? "registry" : result?.method === "ai_search" ? "ai_search" : "manual";
      const payload = {
        legal_name: entity.legal_name.trim(),
        short_name: entity.short_name.trim(),
        credit_code: entity.credit_code.trim(),
        website: entity.website.trim(),
        founded: entity.founded.trim(),
        location: entity.location.trim(),
        industry: entity.industry.trim(),
        description: entity.description.trim(),
        stage: entity.stage.trim(),
        aliases: entity.aliases.filter(Boolean),
        entity_source: source,
      };
      const points = focus.split(/\n+/).map((t) => t.trim()).filter((t) => t.length >= 2).slice(0, 3);
      if (points.some((t) => t.length > 200)) throw new Error("每条关注点请控制在 200 字以内");
      const companyNames = payload.short_name ? [payload.short_name] : [];
      const project = await api.createProject(payload, drafts.map((d) => d.draft_id), points, companyNames);
      navigate(`/projects/${project.id}${points.length ? "?tab=external" : ""}`);
    } catch (e) {
      setError(e);
      setCreating(false);
    }
  };

  const field = (key: keyof Candidate, label: string, placeholder = "") => (
    <label className="field">
      <span>{label}</span>
      <input value={(entity[key] as string) ?? ""} placeholder={placeholder}
        onChange={(e) => setEntity({ ...entity, [key]: e.target.value })} />
    </label>
  );

  const modelName = settings?.model.ready ? settings.model.model : null;

  return (
    <Page className="page-narrow">
      <div className="steps">
        <span className={step === 1 ? "on" : "done"}><i>1</i>输入公司</span>
        <span className="sep"><motion.span className="sep-fill" animate={{ scaleX: step === 2 ? 1 : 0 }} transition={{ duration: 0.5, ease: EASE }} /></span>
        <span className={step === 2 ? "on" : ""}><i>2</i>确认主体并开始监控</span>
      </div>
      <AnimatePresence mode="wait" initial={false}>

      {step === 1 && (
        <motion.section key="s1" className="card card-pad" initial={{ opacity: 0, x: -16 }} animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: -24 }} transition={{ duration: 0.32, ease: EASE }}>
          <h1 className="display-sm">添加项目</h1>
          <p className="muted">输入公司名，或拖入 BP。</p>
          <label className="field field-lg">
            <span>公司名称</span>
            <input autoFocus value={name} placeholder="例如：宁德时代 / 某某生物科技有限公司"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && (name.trim() || drafts.length) && resolve()} />
          </label>
          <div
            className={`dropzone ${dragging ? "drag" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files); }}
            onClick={() => fileInput.current?.click()}
            role="button" tabIndex={0}
          >
            <input ref={fileInput} type="file" multiple hidden accept=".pdf,.docx,.pptx,.xlsx,.xls,.txt"
              onChange={(e) => e.target.files && addFiles(e.target.files)} />
            {uploading ? <Spinner label="正在读取文件" /> : (
              <>
                <strong>拖入 BP 或其他资料</strong>
                <span className="muted">PDF / Word / PPT / Excel / TXT · 可选</span>
              </>
            )}
          </div>
          {drafts.length > 0 && (
            <ul className="file-list">
              {drafts.map((d) => (
                <li key={d.draft_id}>
                  <span>{d.filename}</span>
                  {d.readable ? <span className="tag tag-ok">可读取</span> : <span className="tag tag-warn">未读到文字 · 可能需 OCR</span>}
                  <button className="link" onClick={() => setDrafts(drafts.filter((x) => x !== d))}>移除</button>
                </li>
              ))}
            </ul>
          )}
          <ErrorNote error={error} />
          <div className="actions">
            <button className="btn btn-primary btn-lg" disabled={resolving || uploading || (!name.trim() && !drafts.length)}
              onClick={resolve}>
              {resolving ? <Spinner label="正在比对公开信息…" /> : "识别公司"}
            </button>
          </div>
        </motion.section>
      )}

      {step === 2 && result && (
        <motion.div key="s2" initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 16 }} transition={{ duration: 0.36, ease: EASE }}>
          <section className="card card-pad">
            <h2>是哪一家？</h2>
            <p className="muted">
              {result.method === "registry" ? "根据工商登记数据" : result.method === "ai_search" ? "根据公开搜索结果由 AI 判断" : "未配置 AI 模型，按输入名称建立"}
              。请核对公司主体。
            </p>
            {result.notes && <div className="note">{result.notes}</div>}
            <div className="candidates">
              {result.candidates.map((c, i) => (
                <motion.label key={i} className={`candidate ${picked === i ? "on" : ""}`}
                  initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.08 + i * 0.06, duration: 0.35, ease: EASE }}>
                  <input type="radio" checked={picked === i} onChange={() => choose(i)} />
                  <div>
                    <div className="candidate-name">
                      {c.legal_name}
                      {c.confidence > 0 && <span className={`conf ${c.confidence >= 70 ? "hi" : c.confidence >= 40 ? "mid" : "lo"}`}>匹配度 {c.confidence}</span>}
                    </div>
                    <div className="muted small">
                      {[c.short_name, c.location, c.founded && `成立于 ${c.founded}`, c.credit_code].filter(Boolean).join(" · ")}
                    </div>
                    {c.description && <div className="candidate-desc">{c.description}</div>}
                    {c.reason && <div className="muted small">{c.reason}</div>}
                  </div>
                </motion.label>
              ))}
              <label className={`candidate ${picked === -1 ? "on" : ""}`}>
                <input type="radio" checked={picked === -1} onChange={() => choose(-1)} />
                <div>
                  <div className="candidate-name">都不是，我来填写</div>
                </div>
              </label>
            </div>
          </section>

          <section className="card card-pad">
            <h2>核对公司信息</h2>
            <div className="grid-2">
              {field("legal_name", "工商全称 *")}
              {field("short_name", "公司简称", "仅填写这家公司的简称；品牌和产品填在下方")}
              {field("industry", "行业 / 赛道")}
              {field("stage", "融资阶段")}
              {field("location", "所在地")}
              {field("website", "官网")}
            </div>
            <label className="field">
              <span>品牌、产品与其他检索词（逗号分隔）</span>
              <input value={entity.aliases.join("，")}
                onChange={(e) => setEntity({ ...entity, aliases: e.target.value.split(/[,，]/).map((s) => s.trim()) })} />
            </label>
          </section>

          <section className="card card-pad">
            <h2>最关心什么？<span className="tag">可选</span></h2>
            <p className="muted small">每行一条，最多 3 条；命中时标记，可开启推送。</p>
            <textarea rows={4} value={focus} maxLength={600} onChange={(e) => setFocus(e.target.value)}
              placeholder={"例如：\n定点车型什么时候量产\n下一轮融资和估值\n核心技术人员是否变动"} />
          </section>

          <section className="card card-pad consent">
            <h2>开始监控</h2>
            <ul className="checklist">
              <li>{drafts.length ? `${drafts.length} 份资料将发送至模型（${modelName ?? "尚未配置"}）分析，原件本机保存。` : "后续资料将发送至配置的模型分析，原件本机保存。"}</li>
              <li>公司、产品、核心人员与竞品名称用于公开新闻、论文、临床与专利检索。</li>
              <li>默认每天更新，频率随动态调整（设置可修改）。AI 整理的内容都附原文出处。</li>
            </ul>
            <label className="check">
              <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
              我已确认公司主体，同意按以上方式持续分析这个项目
            </label>
            <ErrorNote error={error} />
            <div className="actions">
              <button className="btn btn-ghost" onClick={() => setStep(1)}>返回</button>
              <button className="btn btn-primary btn-lg" disabled={!consent || !entity.legal_name.trim() || creating} onClick={create}>
                {creating ? <Spinner label="正在建立…" /> : "开始监控"}
              </button>
            </div>
            {!settings?.model.ready && (
              <p className="muted small">未配置模型，仅收集公开新闻。<Link to="/settings">去设置</Link></p>
            )}
          </section>
        </motion.div>
      )}
      </AnimatePresence>
    </Page>
  );
}
