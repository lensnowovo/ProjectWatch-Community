import React,{useState} from 'react';
import {createRoot} from 'react-dom/client';
import {MemoryRouter} from 'react-router-dom';
import ProjectsPage from '../src/pages/Projects';
import {api,type Project} from '../src/api';
import {ToastProvider} from '../src/motion';
import '../src/styles.css';
window.fetch=async()=>{throw Error('Network prohibited in synthetic preview');};
for(const key of Object.keys(api))(api as any)[key]=async()=>{throw Error('Unmocked API: '+key);};
let fail=false;
let projects=Array.from({length:5},(_,i)=>({id:`synthetic-${i}`,name:i===0?'合成创新科技ABCDEFGHIJKLMNOPQRSTUVWXYZ有限公司（长名称测试）':`合成科技 ${i+1}`,
  legal_name:'合成创新科技有限公司',industry:'合成技术',status:'active',headline:'已保存的合成判断，用于验证表格的正常列宽。',
  last_event:'合成公司披露新一阶段产品进展',last_event_at:'2026-10-29T05:00:00Z',major_recent:0,unread:0} as Project));
api.projects=async()=>projects;
api.attention=async()=>({projects:[]} as any);
api.removedProjects=async()=>[];
document.documentElement.dataset.removeCalls='0';
api.removeProject=async(id)=>{
  document.documentElement.dataset.removeCalls=String(Number(document.documentElement.dataset.removeCalls)+1);
  document.documentElement.dataset.removeTarget=id;
  await new Promise(resolve=>setTimeout(resolve,600));
  if(fail)throw Error('合成失败：请稍后再试');
  projects=projects.filter(p=>p.id!==id);
  return {} as any;
};
function App(){const [failure,setFailure]=useState(false);return <ToastProvider>
  <div style={{padding:'16px 32px',background:'var(--paper)'}}>隔离预览 · 全部为合成数据 · 操作不影响真实项目 {' '}
    <button className="btn btn-sm" onClick={()=>{fail=!failure;setFailure(fail);}}>模拟失败：{failure?'开启':'关闭'}</button></div>
  <MemoryRouter><ProjectsPage/></MemoryRouter>
</ToastProvider>;}
createRoot(document.getElementById('root')!).render(<App/>);
