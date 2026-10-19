import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, type Job } from './api';
import { ErrorNote, Spinner, useAsync } from './ui';

const REQUEST_LABEL: Record<string, string> = {
  search: '检索', model: '模型分析', open_data: '公开数据', page: '原文读取', site: '官网页面',
};

/** An explicit owner action, never triggered by reading the failed update. */
export function RecoveryDialog({ projectId, job, onClose, onQueued }: {
  projectId: string; job: Job; onClose: () => void; onQueued: () => void;
}) {
  const preview = useAsync(() => api.recoveryPreview(projectId, job.id), [projectId, job.id]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const actionKey = useRef(crypto.randomUUID());
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  const state = preview.data;
  const knownFailure = state?.reason === '没有结果不明的请求';
  const submit = async () => {
    if (!state || busy || (!state.can_recover && !knownFailure)) return;
    setBusy(true); setError(null);
    try {
      if (state.can_recover) await api.recover(projectId, job.id, state.version, actionKey.current);
      else await api.refresh(projectId, job.kind);
      onQueued(); onClose();
    } catch (e) { setError(e); }
    finally { setBusy(false); }
  };
  return createPortal(<div className="modal-backdrop" onClick={() => !busy && onClose()}>
    <div ref={panel} className="modal recovery-modal" tabIndex={-1} role="dialog" aria-modal="true"
      aria-labelledby="recovery-title" aria-describedby="recovery-description" onClick={e => e.stopPropagation()}
      onKeyDown={e => {
        if (e.key === 'Escape' && !busy) onClose();
        if (e.key !== 'Tab') return;
        const buttons = Array.from(panel.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? []);
        if (!buttons.length) { e.preventDefault(); return; }
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
          e.preventDefault(); last.focus();
        } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }}>
      <header className="modal-head"><h3 id="recovery-title">继续这次更新</h3>
        <button className="icon-btn" aria-label="关闭" disabled={busy} onClick={onClose}>×</button></header>
      {preview.loading && !state && <Spinner label="查看更新状态" />}
      <ErrorNote error={preview.error || error} />
      {state?.can_recover && <>
        <p id="recovery-description">已完成内容不重复请求。以下请求没有返回结果，重新请求可能再次计费。</p>
        <ul className="recovery-requests">{Object.entries(state.requests).map(([kind, count]) =>
          <li key={kind}><span>{REQUEST_LABEL[kind] ?? '其他请求'}</span><strong>{count} 次</strong></li>)}</ul>
      </>}
      {state && !state.can_recover && <p id="recovery-description">{knownFailure
        ? '上次更新遇到错误，可以重新处理。已有内容保留。' : state.reason}</p>}
      <footer className="modal-foot"><button className="btn" disabled={busy} onClick={onClose}>取消</button>
        {(state?.can_recover || knownFailure) && <button className="btn btn-primary" disabled={busy} onClick={submit}>
          {busy ? '正在安排…' : state?.can_recover ? '确认重新请求' : '重新处理'}</button>}</footer>
    </div>
  </div>, document.body);
}
