import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, type Job } from './api';
import { ErrorNote, Spinner, useAsync } from './ui';

const REQUEST_LABEL: Record<string, string> = {
  search: '检索', model: '模型分析', open_data: '公开数据', page: '原文读取', site: '官网页面',
};

/** An explicit owner action, never triggered by reading the failed update. */
export function RecoveryDialog({ projectId, job, onClose, onQueued }: {
  projectId: string; job?: Job; onClose: () => void; onQueued: () => void;
}) {
  const index = useAsync(() => api.recoveryProject(projectId), [projectId]);
  const [chosen, setChosen] = useState('');
  const target = index.data?.items.find(p => p.job_id === chosen)?.job_id
    ?? index.data?.items[0]?.job_id ?? (index.data ? job?.id : undefined);
  const preview = useAsync(() => target ? api.recoveryPreview(projectId, target) : Promise.resolve(null), [projectId, target]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const actionKey = useRef(crypto.randomUUID());
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  const state = preview.data?.job_id === target ? preview.data : null;
  const knownFailure = state?.reason === '没有结果不明的请求';
  const submit = async () => {
    if (!state || busy || (!state.can_recover && !knownFailure)) return;
    setBusy(true); setError(null);
    try {
      if (state.can_recover) await api.recover(projectId, state.job_id, state.version, actionKey.current);
      else if (job) await api.refresh(projectId, job.kind);
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
      <header className="modal-head"><h3 id="recovery-title">处理待确认请求</h3>
        <button className="icon-btn" aria-label="关闭" disabled={busy} onClick={onClose}>×</button></header>
      {(index.loading || preview.loading) && !state && <Spinner label="定位实际阻塞请求" />}
      <ErrorNote error={index.error || preview.error || error} />
      {!!index.data?.items.length && <ul className="recovery-requests" aria-label="待处理请求">{index.data.items.map((item, i) =>
        <li key={item.job_id}><button className="link" disabled={busy} aria-pressed={target === item.job_id}
          onClick={() => { setChosen(item.job_id); actionKey.current = crypto.randomUUID(); setError(null); }}>
          {i + 1}. {Object.entries(item.requests).map(([kind, count]) => `${REQUEST_LABEL[kind] ?? '其他请求'} ${count} 次`).join('、')}
          {item.job_status === 'done' ? ' · 历史任务已完成，仍有待处理请求' : item.can_recover ? ' · 可确认恢复' : ' · 暂不可恢复'}
        </button></li>)}</ul>}
      {index.data && !target && <p id="recovery-description">当前没有待确认的请求。</p>}
      {state?.can_recover && <>
        <p id="recovery-description">{state.mode === 'checkpoint_continuation'
          ? '旧任务和回执保留。将新建续跑任务，基于已保存资料重新取得模型回答；这不是原 HTTP 请求的逐字重放，可能再次计费。'
          : state.mode === 'model_continuation'
            ? '进度已保存，可继续。将从已有资料继续未完成的研究，不重发旧请求；需要新的模型调用，可能产生费用。'
          : '已有内容保留。以下请求结果不明或未取得可用回答，恢复可能再次计费。'}</p>
        {state.notice && <p>{state.notice}</p>}
        <ul className="recovery-requests">{Object.entries(state.requests).map(([kind, count]) =>
          <li key={kind}><span>{REQUEST_LABEL[kind] ?? '其他请求'}</span><strong>{count} 次</strong></li>)}</ul>
      </>}
      {state && !state.can_recover && <p id="recovery-description">{knownFailure
        ? '上次更新遇到错误，可以重新处理。已有内容保留。' : state.reason}</p>}
      <footer className="modal-foot"><button className="btn" disabled={busy} onClick={onClose}>取消</button>
        {(state?.can_recover || knownFailure) && <button className="btn btn-primary" disabled={busy} onClick={submit}>
          {busy ? '正在安排…' : state?.can_recover
            ? state.mode === 'model_continuation' || state.mode === 'checkpoint_continuation' ? '从已保存进度继续' : '确认重新请求'
            : '重新处理'}</button>}</footer>
    </div>
  </div>, document.body);
}
