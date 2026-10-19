// Read-only handoff: upload/parse never implies permission to send a BP to AI.
export async function waitForBp({projectId,sourceId,request,cancelled=()=>false,
  onWaiting=()=>{},sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),attempts=120}) {
  for(let i=0;i<attempts;i++) {
    if(cancelled())return null;
    const jobs=await request(`/api/projects/${projectId}/intake`);
    if(cancelled())return null;
    const job=jobs.find(j=>j.source_id===sourceId);
    if(job?.status==='failed')throw Error('这份 BP 暂未读取成功，原件已保留。可从项目资料查看原因。');
    if(job?.status==='needs_ocr')throw Error('部分页面需要文字识别，原件已保留。请先在项目资料补齐可读内容。');
    if(job?.status==='completed') {
      const fragments=await request(`/api/projects/${projectId}/sources/${sourceId}/fragments`);
      if(cancelled())return null;
      if(!fragments.length)throw Error('这份 BP 没有可读文字，原件已保留。');
      return fragments;
    }
    onWaiting();await sleep(1500);
  }
  throw Error('BP 仍在后台解析。可以关闭窗口，稍后选择这份材料继续，不必重复上传。');
}
