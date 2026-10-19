export const tabs = [
  ['overview','公司概览'],['internal','经营情况'],['news','公司动态'],['competition','竞争对手'],['industry','行业环境'],['analysis','研究分析'],['scope','监控范围'],['documents','资料库'],
] as const;
export function normalizePath(path:string){
  if(path.includes('/external')){
    const category=new URLSearchParams(path.split('?')[1]||'').get('category');
    return path.replace('/external',category==='peers'?'/competition':category&&['industry','policy','papers'].includes(category)?'/industry':'/news');
  }
  return path;
}
export function readRoute(hash:string){
  const path=normalizePath(hash.replace(/^#/,''));
  const match=path.match(/^\/project\/(chengyao|hengchuan)\/([^?]+)/);
  const tab=match&&tabs.some(([id])=>id===match[2])?match[2]:'overview';
  return {projectId:match?.[1]||'chengyao',tab,params:new URLSearchParams(path.split('?')[1]||''),portfolio:!match};
}
