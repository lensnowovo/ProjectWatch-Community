import type { TrackView, TrackCell } from './api';
import { ChartHint } from './chartHint';
import './trackSignalRows.css';

const TONES = { good: '顺风', bad: '逆风', neutral: '中性', unknown: '未明' };

/** Record counts are coverage, not a proxy for market growth. Keep time windows explicit. */
export function TrackSignalRows({ data, onOpen }: { data: TrackView; onOpen: (cell: TrackCell) => void }) {
  const impacts = data.cells.filter(cell => cell.for_us?.trim());
  const range = data.quarters.length ? `${data.quarters[0]} — ${data.quarters[data.quarters.length - 1]}` : '';
  return <div className="tsr">
    <div className="tsr-heading"><strong>赛道信号</strong><span>{data.industry}</span></div>
    <div className="tsr-columns" aria-hidden="true"><span>信号</span><span>关键数据 / 口径</span><span>方向</span><span>季度收录数</span></div>
    <ul className="tsr-list">{data.cells.map(cell => {
      const missing = !cell.value?.trim() || /^[—–-]$/.test(cell.value.trim());
      const valid = cell.series.length > 1 && cell.series.every(point => Number.isFinite(point.value) && point.value >= 0);
      const chart = valid && cell.series.some(point => point.value > 0);
      const max = chart ? Math.max(...cell.series.map(point => point.value)) : 1;
      return <li className={`tsr-row ${cell.tone}`} key={cell.key}>
        <button className="tsr-name" onClick={() => onOpen(cell)} aria-label={`看全部${cell.label}记录`}>{cell.label}<span aria-hidden="true">↗</span></button>
        <div className={`tsr-metric${missing ? ' missing' : ''}`}><strong>{missing ? '信息不足' : cell.value}</strong>{!missing && cell.sub && <small>{cell.sub}</small>}</div>
        <span className={`tsr-tone ${cell.tone}`} aria-label={`方向：${TONES[cell.tone]}`}>{missing && cell.tone === 'unknown' ? '—' : TONES[cell.tone]}</span>
        <div className="tsr-trend" aria-label={`${cell.label}季度收录记录数`}>
          {chart ? cell.series.map((point, index) => <ChartHint key={`${point.label}-${index}`} title={`${cell.label} · ${point.label}`} value={`${point.value} 条记录`}
            note="仅表示本季度收录的记录数，不代表市场规模或景气变化；左侧指标按其注明的期间统计。">
            <span className="tsr-bar-target"><i style={{ height: `${point.value / max * 100}%` }} /></span>
          </ChartHint>) : <span className="tsr-no-series" aria-label="暂无可展示的分期记录">—</span>}
        </div>
      </li>;
    })}</ul>
    {range && <div className="tsr-range">季度收录数 <span>{range}</span></div>}
    {!!impacts.length && <details className="tsr-impact"><summary>对公司的影响<span>{impacts.length} 项</span></summary><dl>{impacts.map(cell => <div key={cell.key}><dt>{cell.label}</dt><dd>{cell.for_us}</dd></div>)}</dl></details>}
  </div>;
}
