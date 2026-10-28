import { useState } from 'react';
import { api, type DiscoveredSourcePage } from './api';
import { dayLabel, ErrorNote, useAsync } from './ui';
import './discoveredSources.css';

const quality = (item: DiscoveredSourcePage['items'][number]) =>
  item.source_type === 'structured_record' ? '结构化记录' :
    ({ summary: '摘要', partial: '部分正文', full: '完整正文', fragmented: '正文片段' }[item.read_scope] ?? '获取范围未知');

export function DiscoveredSourceList({ data }: { data: DiscoveredSourcePage }) {
  return <ul className="discovered-source-list">{data.items.map(item => <li key={item.id}>
    <div className="discovered-source-meta"><span>{item.source || '来源未标注'}</span>
      <span>{item.published_at ? dayLabel(item.published_at) : '发布日期未知'}</span>
      <span>{item.source_type === 'search_summary' ? '搜索摘要' : quality(item)}</span>
      {item.read_status && !['article_body','partial_body','filing_text','search_summary','paper_abstract'].includes(item.read_status)
        && <span>{item.reading_basis === 'donor_text' ? '参考其他来源正文' : '正文暂不可用'}</span>}
      <span>{item.processing_status === 'pending' ? '待整理 · 归属待确认' : '已整理'}</span></div>
    <a href={/^https?:\/\//i.test(item.url) ? item.url : undefined} target="_blank" rel="noopener noreferrer">{item.title}</a>
  </li>)}</ul>;
}

export function DiscoveredSources({ projectId, revision }: { projectId: string; revision: string }) {
  const [offset, setOffset] = useState(0);
  const { data, error } = useAsync(() => api.discoveredSources(projectId, offset), [projectId, offset, revision]);
  if (error) return <ErrorNote error={error} />;
  if (!data || !data.total) return null;
  return <details className="discovered-sources">
    <summary>已发现来源 {data.total} <span className="muted">{data.pending ? `· ${data.pending} 条待整理` : '· 已整理'}</span></summary>
    <DiscoveredSourceList data={data} />
    {(offset > 0 || data.next_offset !== null) && <div className="discovered-source-pages">
      <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</button>
      <span>{offset + 1}–{offset + data.items.length} / {data.total}</span>
      <button type="button" disabled={data.next_offset === null} onClick={() => data.next_offset !== null && setOffset(data.next_offset)}>下一页</button>
    </div>}
  </details>;
}
