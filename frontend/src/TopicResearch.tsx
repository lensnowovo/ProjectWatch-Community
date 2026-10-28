import { useEffect, useState } from 'react';
import { api, type PlanItem, type TopicResearchView } from './api';
import { publicLink } from './relationScope';
import { ErrorNote, relTime, useAsync } from './ui';

export function TopicResult({ view }: { view: TopicResearchView }) {
  const { result, job } = view;
  if (!job) return <p className="muted small">尚未单独研究这个问题。</p>;
  const label = { queued: '已排队', running: '正在研究', done: '本轮已结束', failed: '本轮未完成' }[job.status];
  return <div>
    <p className="small" role="status">{label} · {relTime(job.created_at)}</p>
    {view.scope_changed && <p className="muted small">问题范围已修改，以下为上次研究：{view.question?.text}</p>}
    {result && <>
      <strong>{result.answer_status === 'answered' ? '本题研究结果' : '暂未形成回答'}</strong>
      {result.text && <p>{result.text}</p>}
      {result.status === 'partial' && <p className="muted small">本轮部分完成</p>}
      {!!result.gaps.length && <details><summary>查看未完成项（{result.gaps.length}）</summary>
        <ul>{result.gaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul></details>}
      {!!result.sources.length && <details><summary>查看依据（{result.sources.length}）</summary>
        <ul>{result.sources.map(source => <li key={source.ref}>
          {publicLink(source.url) ? <a href={source.url} target="_blank" rel="noreferrer">{source.title || source.ref} ↗</a>
            : <span>{source.title || source.ref}</span>}
          {source.date && <span className="muted small"> · {source.date}</span>}
          <p className="small">{source.context_kind}：{source.context}</p>
        </li>)}</ul></details>}
    </>}
    {job.status === 'failed' && <a href="/tasks">查看任务详情</a>}
  </div>;
}

export function TopicResearch({ item }: { item: PlanItem }) {
  const { data, error, loading, reload, setData } = useAsync(
    () => api.topicResearch(item.project_id, item.id), [item.project_id, item.id, item.lifecycle_version]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const active = data?.job?.status === 'queued' || data?.job?.status === 'running';
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(reload, 5000);
    return () => window.clearInterval(timer);
  }, [active, reload]);
  const start = async () => {
    if (submitting || active || loading || !item.lifecycle_version) return;
    setSubmitting(true); setSubmitError(null);
    try {
      const job = await api.startTopicResearch(item.project_id, item.id, item.lifecycle_version);
      setData({ job, question: { text: item.text, exit_rule: item.exit_rule, lifecycle_version: item.lifecycle_version },
        scope_changed: false, result: null });
      await reload();
    } catch (e) { setSubmitError(e); }
    finally { setSubmitting(false); }
  };
  return <section aria-label="单独研究此问题">
    {data && <TopicResult view={data} />}
    {error != null && <ErrorNote error={error} />}
    {submitError != null && <ErrorNote error={submitError} />}
    <div className="plan-actions">
      {item.status !== 'retired' && <button type="button" className="btn btn-sm"
        disabled={submitting || active || loading || !data || !!error || !item.lifecycle_version} onClick={start}>
        {submitting ? '正在提交…' : active ? '本题研究进行中' : '启动本题研究'}</button>}
      <button type="button" className="btn btn-sm btn-ghost" disabled={loading || submitting} onClick={reload}>刷新结果</button>
    </div>
    <p className="muted small">使用当前模型与搜索配置，仅研究本题，按专题预算运行。</p>
  </section>;
}
