// Synthetic stored results, no network or product mutations.
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import FeedPage from '../src/pages/Feed';
import DigestPage from '../src/pages/Digest';
import {api, type Item, type Project} from '../src/api';
import {ToastProvider} from '../src/motion';
import '../src/styles.css';

window.fetch = async () => {throw Error('Network prohibited in synthetic preview');};
for (const key of Object.keys(api)) (api as any)[key] = async () => {throw Error('Unmocked API: '+key);};
let scenario='empty', version=0;
const projects=[{id:'synthetic',name:'合成科技',status:'active'} as Project];
const item=():Item=>({id:'same-event',project_id:'synthetic',project_name:'合成科技',category:'company',
  title:version?'合成公告：已核对的同一事件':'合成公告：原始标题',url:'https://example.invalid/news',source:'合成公告',
  published_at:new Date(Date.now()-86400000).toISOString(),collected_at:new Date().toISOString(),
  summary:'仅用于验证页面刷新和日期口径。',snippet:'',importance:version?'major':'routine',
  importance_status:version?'assessed':'pending',status:'read',extra:{},source_count:1});
const summary=()=>({counts:{total:4,in_period:scenario==='empty'?0:1,matching:scenario==='empty'?0:1,
  undated:2,older:1,future:scenario==='empty'?1:0,major:version?1:0,notable:0,routine:0,pending:version?0:1},
  progress:[{id:'synthetic',state:'failed',label:'更新未完成'}],
  note:scenario==='empty'?'所选期间暂无有可靠日期的动态。另有 2 条日期待核，可在项目外部信息查看。1 条早于所选期间。1 条日期在未来，暂不计入本期。':
    `期内有 1 条动态：重大 ${version?1:0} 条，关注 0 条，一般 0 条，待评级 ${version?0:1} 条。`});
api.feed=async()=>scenario==='empty'?[]:[item()];
api.feedSummary=async()=>summary() as any;
api.attention=async()=>({projects:[],totals:{crosscheck:0,silence:0}} as any);
api.digest=async()=>{
  const included=scenario!=='empty'&&version>0;
  return {days:7,projects:included?[{id:'synthetic',name:'合成科技',events:[{...item(),when:item().published_at}],
    more_events:0,doc_changes:[],issues:[],major:1,notable:0}]:[],
    totals:{projects:included?1:0,major:included?1:0,notable:0,doc_changes:0},quiet:[],
    incomplete:[{id:'synthetic',name:'合成科技',state:'failed',label:'更新未完成'}],
    window_note:summary().note,window_summary:summary(),markdown:summary().note,notify:{ready:false}} as any;
};
function App(){const [page,setPage]=useState('feed'),[refresh,setRefresh]=useState(0);return <ToastProvider>
  <div style={{padding:'16px 32px',background:'var(--paper)'}}>
    隔离预览 · 合成数据 {' '}
    <button className="btn btn-sm" onClick={()=>setPage(page==='feed'?'digest':'feed')}>切换今日动态 / 周报</button>{' '}
    <button className="btn btn-sm" onClick={()=>{scenario=scenario==='empty'?'record':'empty';version=0;setRefresh(n=>n+1);}}>切换空白 / 有记录</button>{' '}
    <button className="btn btn-sm" onClick={()=>{version++;document.dispatchEvent(new Event('visibilitychange'));}}>修订同一事件并检查更新</button>
  </div>
  <MemoryRouter key={refresh}>{page==='feed'?<FeedPage projects={projects}/>:<DigestPage/>}</MemoryRouter>
  </ToastProvider>;}
createRoot(document.getElementById('root')!).render(<App/>);
