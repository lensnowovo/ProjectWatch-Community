import { useState } from 'react';
import { api, type CollectionSourceDecision, type SourceSelection } from './api';
import { ErrorNote, useAsync } from './ui';
import './collectionSources.css';

const NAMES: Record<string, string> = {
  search: '公开搜索', site: '官网页面', cninfo: '公开公告',
  openalex_affiliation: '论文 · 机构', openalex_author: '论文 · 作者', openalex_topic: '论文 · 主题',
  patent_search: '专利线索', clinical_sponsor: '临床 · 申办方', clinical_term: '临床 · 产品与干预',
  cn_regulatory: '国内注册线索',
};
const SELECTIONS: Record<SourceSelection, string> = {
  enabled: '纳入采集', disabled: '暂不采集', not_applicable: '不适用',
};

export function SourceChoiceRow({ adapter, decision, save }: {
  adapter: string; decision?: CollectionSourceDecision;
  save: (selection: SourceSelection, reason: string, version: number) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [selection, setSelection] = useState<SourceSelection>(decision?.selection ?? 'enabled');
  const [reason, setReason] = useState(decision?.reason ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const label = NAMES[adapter] ?? adapter;
  return <li className="collection-source-row">
    <div className="collection-source-summary"><strong>{label}</strong>
      <span>{SELECTIONS[decision?.selection ?? 'enabled']}</span>
      <button type="button" className="text-btn" disabled={busy} aria-expanded={editing}
        aria-label={`调整${label}`} onClick={() => { setEditing(!editing); setError(null); }}>调整</button>
    </div>
    {decision?.reason && !editing && <p className="muted small">{decision.reason}</p>}
    {editing && <form className="collection-source-editor" onSubmit={async event => {
      event.preventDefault();
      if (busy || !reason.trim()) return;
      setBusy(true); setError(null);
      try { await save(selection, reason.trim(), decision?.version ?? 0); setEditing(false); }
      catch (e) { setError(e); }
      finally { setBusy(false); }
    }}>
      <label>采集选择<select value={selection} disabled={busy}
        onChange={event => setSelection(event.target.value as SourceSelection)}>
        {Object.entries(SELECTIONS).map(([value, title]) => <option key={value} value={value}>{title}</option>)}
      </select></label>
      <label>选择理由<input value={reason} onChange={event => setReason(event.target.value)}
        disabled={busy} required maxLength={600} placeholder="说明这个来源为何纳入或排除" /></label>
      <div><button type="submit" disabled={busy || !reason.trim()}>{busy ? '保存中…' : '保存选择'}</button>
        <button type="button" className="text-btn" disabled={busy} onClick={() => setEditing(false)}>取消</button></div>
      {error != null && <ErrorNote error={error} />}
    </form>}
  </li>;
}

export function CollectionSources({ projectId }: { projectId: string }) {
  const { data, error, loading, reload, setData } = useAsync(() => api.collectionSources(projectId), [projectId]);
  if (error) return <div><ErrorNote error={error} /><button type="button" onClick={reload}>重新读取来源选择</button></div>;
  if (!data) return <p className="muted small">{loading ? '正在读取来源选择…' : '暂无来源选择'}</p>;
  return <section className="collection-source-choices" aria-label="来源选择">
    <p className="muted small">按项目选择采集来源。保存不会立即运行任务；正在采集时调整范围，会停止旧范围的后续请求。纳入采集仍需来源可用。</p>
    <button type="button" className="text-btn" disabled={loading} onClick={reload}>刷新来源选择</button>
    <ul>{data.adapters.map(adapter => <SourceChoiceRow key={`${projectId}:${adapter}:${data.decisions[adapter]?.version ?? 0}`}
      adapter={adapter} decision={data.decisions[adapter]} save={async (selection, reason, version) => {
        const decision = await api.chooseCollectionSource(projectId, adapter, selection, reason, version);
        setData(current => current && ({ ...current, decisions: { ...current.decisions, [adapter]: decision } }));
      }} />)}</ul>
  </section>;
}
