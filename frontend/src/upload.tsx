import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "./api";
import { EASE } from "./motion";

const ACCEPT = ".pdf,.docx,.pptx,.xlsx,.xls,.txt";
type Entry = { id: number; name: string; kind: "file" | "link"; state: "working" | "done" | "failed"; note?: string };

/** One place to add material: drop files, choose files, paste files, or paste a web/PDF link. */
export function UploadDialog({ projectId, open, onClose, onAdded }: {
  projectId: string; open: boolean; onClose: () => void; onAdded: () => void;
}) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [dragging, setDragging] = useState(false);
  const [url, setUrl] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const seq = useRef(0);

  const track = (name: string, kind: Entry["kind"]) => {
    const id = ++seq.current;
    setEntries((list) => [{ id, name, kind, state: "working" }, ...list]);
    return (state: Entry["state"], note?: string) =>
      setEntries((list) => list.map((e) => (e.id === id ? { ...e, state, note } : e)));
  };

  const addFiles = async (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      const done = track(file.name, "file");
      try { await api.uploadFile(projectId, file); done("done", "已加入，正在识别"); onAdded(); }
      catch (e) { done("failed", e instanceof Error ? e.message : "上传失败"); }
    }
  };
  const addLink = async (value = url) => {
    const link = value.trim();
    if (!link) return;
    setUrl("");
    const done = track(link.replace(/^https?:\/\//, ""), "link");
    try {
      const r = await api.addLink(projectId, link);
      done("done", `已保存为“${r.name}”，正在识别`); onAdded();
    } catch (e) { done("failed", e instanceof Error ? e.message : "读取失败"); }
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    // Paste anywhere while the dialog is open: files are uploaded, a link goes into the box.
    const onPaste = (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData?.files ?? []);
      if (files.length) { e.preventDefault(); addFiles(files); return; }
      const text = e.clipboardData?.getData("text")?.trim() ?? "";
      if (/^https?:\/\/\S+$/i.test(text) && (e.target as HTMLElement)?.tagName !== "INPUT") { e.preventDefault(); setUrl(text); }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("paste", onPaste);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("paste", onPaste); };
  });

  // Portal: the header sits in its own stacking context; the dialog must cover the whole page.
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div className="modal-backdrop" onClick={onClose} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}>
          <motion.div className="modal upload-modal" role="dialog" aria-modal="true" aria-label="添加资料" onClick={(e) => e.stopPropagation()}
            initial={{ opacity: 0, y: 12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.24, ease: EASE }}>
            <header className="modal-head">
              <div>
                <h3>添加资料</h3>
                <p className="muted small">上传后自动分析，并更新项目信息。</p>
              </div>
              <button className="icon-btn" onClick={onClose} aria-label="关闭">×</button>
            </header>
            <div className={`dropzone upload-drop ${dragging ? "drag" : ""}`} role="button" tabIndex={0}
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files); }}
              onClick={() => input.current?.click()}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}>
              <input ref={input} type="file" multiple hidden accept={ACCEPT} onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
              <strong>拖入文件，或点击选择</strong>
              <span className="muted small">PDF / Word / PPT / Excel / TXT · Ctrl+V 粘贴</span>
            </div>
            <div className="upload-or"><span>或者</span></div>
            <form className="upload-link" onSubmit={(e) => { e.preventDefault(); addLink(); }}>
              <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="粘贴网页或 PDF 链接" aria-label="网页或 PDF 链接"
                inputMode="url" autoComplete="off" />
              <button className="btn btn-primary" disabled={!url.trim()}>读取链接</button>
            </form>
            <p className="muted small upload-hint">仅支持公开链接 · 原件本机保存</p>
            {entries.length > 0 && (
              <ul className="upload-list">
                {entries.map((e) => (
                  <li key={e.id} className={`ul-${e.state}`}>
                    <span className="ul-kind">{e.kind === "link" ? "链接" : "文件"}</span>
                    <span className="ul-name" title={e.name}>{e.name}</span>
                    <span className="ul-state">{e.state === "working" ? "处理中…" : e.note}</span>
                  </li>
                ))}
              </ul>
            )}
            <footer className="modal-foot"><button className="btn" onClick={onClose}>完成</button></footer>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
