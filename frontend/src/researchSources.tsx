import { useState } from 'react';
import { api, ApiError, type ResearchCategory, type ResearchSource, type ResearchSourcePage } from './api';
import { useVisiblePoll } from './Operational';
import { CATEGORY_LABEL, relTime, useAsync } from './ui';

type Reader = typeof api.researchSources;

/** A loaded prefix always belongs to one source revision. Retry a read conflict
 * once from page zero, never combine pages from different snapshots. */
export async function loadResearchSources(read: Reader, project: string, category: ResearchCategory, wanted: number): Promise<ResearchSourcePage> {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      let page = await read(project, category);
      const first = page;
      const items = [...page.items];
      while (items.length < wanted && page.next_offset !== null) {
        const offset = page.next_offset;
        page = await read(project, category, offset, first.revision);
        if (page.project_id !== project || page.category !== category || page.revision !== first.revision || page.offset !== offset || !page.items.length)
          throw new Error('来源分页已变化，请重新读取');
        items.push(...page.items);
      }
      if (first.project_id !== project || first.category !== category || new Set(items.map(i => i.id)).size !== items.length)
        throw new Error('来源分页不一致，请重新读取');
      return {...first, items, next_offset: page.next_offset};
    } catch (error) {
      if (!(error instanceof ApiError && error.code === 'research_sources_changed' && attempt === 0)) throw error;
    }
  }
  throw new Error('来源持续更新，请稍后重新读取');
}

export function useResearchSources(project: string, category: ResearchCategory) {
  const scope = `${project}:${category}`;
  const [window, setWindow] = useState({scope, count: 40});
  const count = window.scope === scope ? window.count : 40;
  const result = useAsync(() => loadResearchSources(api.researchSources, project, category, count), [project, category, count]);
  // Sources arrive independently of first_result_at / the analysis job.
  useVisiblePoll(result.reload, 15000);
  const page = result.data?.project_id === project && result.data?.category === category ? result.data : undefined;
  return {...result, data: page?.items, page,
    more: () => setWindow({scope, count: (page?.items.length ?? count) + 40}),
    note: page && page.items.length < page.total ? `已显示 ${page.items.length} / ${page.total} 条；图表统计当前已显示来源` : '按已收录来源统计'};
}

export function ResearchMore({page, loading, more}: {page?: ResearchSourcePage; loading: boolean; more: () => void}) {
  return page?.next_offset != null ? <button className="btn small" disabled={loading} onClick={more}>
    {loading ? '读取中…' : `继续显示 · 已显示 ${page.items.length} / ${page.total} 条`}
  </button> : null;
}

const strings = (value: unknown) => Array.isArray(value) ? value.filter(x => typeof x === 'string').join('、') : '';
const text = (value: unknown) => typeof value === 'string' ? value : '';

/** Source IDs must never be sent to event read/feedback endpoints. */
export function ResearchSourceList({items}: {items: ResearchSource[]}) {
  const groups = new Map<string, ResearchSource[]>();
  items.forEach(item => {
    const key = item.category === 'patent' && item.family ? item.family.key : item.id;
    groups.set(key, [...(groups.get(key) || []), item]);
  });
  const renderItem = (item: ResearchSource) => {
    const x = item.extra;
    const href = /^https?:\/\//i.test(item.url) ? item.url : undefined;
    const search = x.paper_search as {query?: string; mode?: string} | undefined;
    const relation = x.query_relationship as {query?: string; subject?: string; relationship?: string} | undefined;
    const relationLabels: Record<string, string> = {same_entity: '名称对应', former_name: '曾用名', brand: '品牌',
      subsidiary: '子公司', product: '产品相关', technical: '技术相关', unverified: '关联待核'};
    const details = [
      ['作者', strings(x.authors)], ['机构', strings(x.institutions)], ['原始署名', strings(x.affiliations)],
      ['申办方', text(x.sponsor)], ['合作方', strings(x.collaborators)],
      ['申请人摘录', strings(x.applicants)], ['摘录依据', text(x.applicant_quote)],
      ['当前权利人摘录', strings(x.current_assignees)], ['申请日期', text(x.filing_date)],
      ['优先权日', text(x.priority_date)], ['文献种类', text(x.patent_kind)],
      ['检索词', text(search?.query) || text(relation?.query) || text(x.assignee_query)],
      ['匹配线索', relation?.relationship ? `${text(relation.subject)} · ${relationLabels[relation.relationship] || '关联待核'}（待复核）` : ''],
    ].filter(([, value]) => value);
    return <article className="item" key={item.id} data-source-id={item.id}>
      <div className="item-body">
        <div className="item-meta"><span className={`cat cat-${item.category}`}>{CATEGORY_LABEL[item.category]}</span>
          <span className="muted">{item.source}</span>
          {item.processing_status === 'pending' && <span className="muted">相关性待核</span>}
          <time className="muted item-date">{item.published_at ? relTime(item.published_at) : `发表日期未提供 · 收录于 ${relTime(item.collected_at)}`}</time>
        </div>
        {href ? <a className="item-title" href={href} target="_blank" rel="noreferrer noopener">{item.title}</a> : <span className="item-title">{item.title}</span>}
        {item.snippet && <p className="item-summary">{item.snippet}</p>}
        <details className="item-reading-detail"><summary className="muted small">来源与匹配信息</summary>
          <p className="muted small">收录于 {relTime(item.collected_at)}{text(x.doi) ? ` · DOI：${text(x.doi)}` : ''}{text(x.patent_number) ? ` · 专利号：${text(x.patent_number)}` : ''}</p>
          <p className="muted small">{item.category === 'patent' ? '检索线索；权利人及法律状态以专利登记为准。' : '相关来源；不据检索命中认定公司归属。'}
            {item.read_scope === 'summary' ? ' 当前材料为搜索摘录或摘要。' : ''}</p>
          {details.map(([label, value]) => <p key={label}><span className="muted">{label}：</span>{value}</p>)}
        </details>
      </div>
    </article>;
  };
  return <div className="items items-flat research-source-list">{[...groups].map(([key, group]) =>
    <div key={key}>
      {renderItem(group[0])}
      {group[0].family && group[0].family.document_count > group.length && <p className="muted small">
        同族已显示 {group.length} / {group[0].family.document_count} 份，继续显示可查看其余文件
      </p>}
      {group.length > 1 && <details className="item-reading-detail"><summary className="muted small">
        同族其他文件 · {group.length - 1} 份
      </summary>{group.slice(1).map(renderItem)}</details>}
    </div>)}</div>;
}
