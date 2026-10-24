// Synthetic company dossier. Preview only; no API, real sources or persistence.
import React, { useRef, useState } from 'react';
import './overview-editorial-preview.css';

const sources = [
  {title:'产品手册', date:'2026.09.12', text:'Sense 非植入式采集设备已向科研单位交付，用于研究，不作为医疗器械获批状态的说明。Link 植入式系统处于临床研究阶段。Rehab 训练终端处于样机验证阶段。'},
  {title:'临床研究公告', date:'2026.10.18', text:'Link 植入式系统完成首例临床入组，研究面向运动功能障碍人群；未取得产品上市许可。公司计划于 2027 年扩大临床研究，计划尚未完成。'},
  {title:'团队介绍', date:'2026.08.21', text:'林知远为创始人，拥有神经工程研究背景；许文清负责临床研究，具有康复医学背景。以上为虚构履历，无真实人物指代。'},
  {title:'融资公告', date:'2026.09.26', text:'公司宣布完成新一轮融资，资金用于临床研究与研发。公告列示投资方晨桥创投、知行医疗基金，未披露本轮金额、轮次与持股比例。以上机构均为设计样本。'},
  {title:'公司档案', date:'2026.09.30', text:'澄序神经科技（上海）有限公司于 2018 年成立，业务包括神经接口系统、神经信号采集与康复研究工具。统一社会信用代码和详细注册地址未纳入本样本。'},
  {title:'业务回顾', date:'2026.09.12', text:'公司于 2018 年成立；2023 年完成 Sense 研究设备首批交付；产品面向科研单位与临床研究团队。当前材料未包含客户名单、收入或回款数字。'},
];
const products = [
  {name:'Sense', sub:'神经信号采集设备', route:'非植入式', application:'科研采集与算法研究', stage:'已交付', tone:'established', date:'2026.09', source:0, detail:'面向科研单位提供采集设备与分析软件。交付记录不等同于医疗器械获批；本样本没有收入与回款数据。'},
  {name:'Link', sub:'植入式神经接口系统', route:'植入式', application:'运动功能重建研究', stage:'临床研究', tone:'research', date:'2026.10', source:1, detail:'已完成首例临床入组。产品处于研究阶段，尚未取得上市许可；下一节点是扩大临床研究。'},
  {name:'Rehab', sub:'神经康复训练终端', route:'外部训练终端', application:'康复训练与闭环反馈', stage:'样机验证', tone:'prototype', date:'2026.09', source:0, detail:'开展设备与算法联调。现有样本不包含注册申报、商业订单或临床疗效结论。'},
];
const history = [
  {date:'2018', title:'公司成立', detail:'启动神经接口技术研发', source:4},
  {date:'2023', title:'首批研究设备交付', detail:'Sense 进入科研应用', source:5},
  {date:'2026.10', title:'植入式系统首例入组', detail:'Link 进入临床研究', source:1},
  {date:'2027 · 计划', title:'扩大临床研究', detail:'尚未完成', source:1, planned:true},
];

export default function OverviewEditorial({sparse = false}:{sparse?:boolean}) {
  const [expanded,setExpanded] = useState<number|null>(null);
  const [source,setSource] = useState(0);
  const modal = useRef<HTMLDialogElement>(null);
  const openSource = (id:number) => {setSource(id); modal.current?.showModal();};
  const ref = (id:number) => <button className="oe-ref" aria-label={`查看${sources[id].title}模拟来源`} onClick={() => openSource(id)}>[{String(id+1).padStart(2,'0')}]</button>;
  return <div className="oe-dossier" id="hp-context">
    <div className="oe-page-line"><span>公司档案</span><div>公开资料 · 截至 2026.10 <details className="oe-source-index"><summary>来源索引 ↗</summary><ul>{sources.map((item,id)=><li key={item.title}><button onClick={()=>openSource(id)}>{String(id+1).padStart(2,'0')}　{item.title}<span>{item.date}</span></button></li>)}</ul></details></div></div>
    {sparse ? <section className="oe-empty"><h2>公司档案正在建立</h2><p>主体已确认。产品、团队与融资信息取得来源后会在这里整理。</p><div><span className="hp-pulse"/> 正在整理公开资料</div></section> : <>
      <section className="oe-products" aria-labelledby="oe-product-title">
        <div className="oe-heading"><h2 id="oe-product-title"><span>01</span>业务与产品</h2><small>产品状态分别记录</small></div>
        <div className="oe-business-lead"><div><span className="oe-lead-label">业务进展</span><p>科研设备已有交付，<br className="oe-lead-break"/>植入式产品推进临床研究。{ref(0)}</p></div><div className="oe-lead-stage"><span>Link · 当前节点</span><strong>临床研究</strong><small>尚未取得上市许可 {ref(1)}</small></div></div>
        <div className="oe-business-layout">
          <div className="oe-table-wrap"><table className="oe-product-table"><thead><tr><th scope="col">产品 / 技术路线</th><th scope="col">应用场景</th><th scope="col">当前阶段</th><th scope="col">来源</th></tr></thead><tbody>
            {products.map((product,i)=><React.Fragment key={product.name}><tr className={expanded===i?'selected':''}><th scope="row"><button className="oe-product-button" aria-expanded={expanded===i} aria-controls={`oe-product-${i}`} onClick={()=>setExpanded(expanded===i?null:i)}><span className="oe-product-name">{product.name}<i>{expanded===i?'−':'+'}</i></span><span>{product.sub}</span><small>{product.route}</small></button></th><td>{product.application}</td><td><span className={`oe-stage ${product.tone}`}>{product.stage}</span><small>截至 {product.date}</small></td><td>{ref(product.source)}</td></tr><tr id={`oe-product-${i}`} hidden={expanded!==i}><td colSpan={4} className="oe-product-detail">{product.detail}</td></tr></React.Fragment>)}
          </tbody></table></div>
          <aside className="oe-market" aria-label="客户与应用"><h3>客户与应用</h3><dl><div><dt>现有应用</dt><dd>科研单位与临床研究团队{ref(5)}</dd></div><div><dt>产品交付</dt><dd>科研设备已有交付记录；植入式系统处于临床研究。{ref(0)}</dd></div></dl><p className="oe-gap">未见可用客户名单与收入数据</p></aside>
        </div>
      </section>
      <div className="oe-two-col">
        <section aria-labelledby="oe-team-title"><div className="oe-heading"><h2 id="oe-team-title"><span>02</span>核心团队</h2><small>公开履历</small></div><ul className="oe-people"><li><div className="oe-person-id"><strong>林知远</strong><span>创始人</span></div><p>神经工程研究背景，负责技术与产品方向。</p>{ref(2)}</li><li><div className="oe-person-id"><strong>许文清</strong><span>临床负责人</span></div><p>康复医学背景，负责临床研究与医院合作。</p>{ref(2)}</li></ul></section>
        <section aria-labelledby="oe-capital-title"><div className="oe-heading"><h2 id="oe-capital-title"><span>03</span>融资与股东</h2><small>公告日期 2026.09.26</small></div><div className="oe-financing"><div className="oe-financing-title"><strong>新一轮融资完成</strong>{ref(3)}</div><p>用于临床研究及产品研发</p><dl><div><dt>公告投资方</dt><dd>晨桥创投 · 知行医疗基金</dd></div><div><dt>轮次 / 金额</dt><dd className="oe-muted">本次公告未披露</dd></div></dl><details className="oe-inline-detail"><summary>股权资料</summary><p>现有样本没有股东名册或持股比例，不能由投资方名单推算股权结构。</p></details></div></section>
      </div>
      <section className="oe-history" aria-labelledby="oe-history-title"><div className="oe-heading"><h2 id="oe-history-title"><span>04</span>发展历程</h2><small><i className="oe-key-dot"/> 已发生 <i className="oe-key-dot planned"/> 计划</small></div><ol>{history.map(item=><li key={item.date} className={item.planned?'planned':''}><time>{item.date}</time><button onClick={()=>openSource(item.source)}><strong>{item.title}</strong><span>{item.detail}</span></button></li>)}</ol><details className="oe-undated"><summary>其他记录 <span>1 条日期待补充</span></summary><p>合作实验室建设记录未载明时间，不计入时间轴。<span className="oe-muted">合成记录</span></p></details></section>
    </>}
    <details className="oe-registry"><summary><span>工商档案</span><span className="oe-registry-teaser">2018 年成立 · 上海</span><span className="oe-registry-expand">展开 ⌄</span></summary><dl><div><dt>工商全称</dt><dd>澄序神经科技（上海）有限公司</dd></div><div><dt>成立时间</dt><dd>2018 年{ref(4)}</dd></div><div><dt>统一社会信用代码</dt><dd className="oe-muted">尚未取得</dd></div><div><dt>注册地址</dt><dd className="oe-muted">尚未取得</dd></div></dl><p>以上为虚构主体；不提供可误用的统一社会信用代码或真实地址。</p></details>
    <dialog ref={modal} className="hp-dialog oe-source-dialog" aria-labelledby="oe-source-title" onClick={e=>{if(e.target===modal.current)modal.current.close();}}><div className="hp-dialog-top"><span>来源条目 · 合成样本</span><button autoFocus aria-label="关闭档案来源" onClick={()=>modal.current?.close()}>×</button></div><h2 id="oe-source-title">{sources[source].title}</h2><small>{sources[source].date} · 公开资料示例</small><p>{sources[source].text}</p><small>仅用于排版和交互评审，没有外部原文链接。</small></dialog>
  </div>;
}
