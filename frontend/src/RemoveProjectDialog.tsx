import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { api, type Project } from './api';
import { ErrorNote } from './ui';
import { useToast } from './motion';

/** Top-layer confirmation: independent of table scrolling and animated card parents. */
export function RemoveProjectDialog({project, onClose, onRemoved}: {
  project: Project; onClose: () => void; onRemoved: () => void;
}) {
  const dialog=useRef<HTMLDialogElement>(null), cancel=useRef<HTMLButtonElement>(null);
  const submitting=useRef(false);
  const [busy,setBusy]=useState(false), [error,setError]=useState<unknown>(null);
  const id=useId(), toast=useToast();
  useEffect(()=>{
    const previous=document.activeElement as HTMLElement|null;
    const overflow=document.body.style.overflow;
    dialog.current?.showModal(); cancel.current?.focus();
    document.body.style.overflow='hidden';
    return ()=>{document.body.style.overflow=overflow;if(previous?.isConnected)previous.focus({preventScroll:true});};
  },[]);
  const remove=async()=>{
    if(submitting.current)return;
    submitting.current=true;setBusy(true);setError(null);
    try {await api.removeProject(project.id);toast(`已将 ${project.name} 移出关注`,'info');onRemoved();onClose();}
    catch(e){setError(e);}
    finally{submitting.current=false;setBusy(false);}
  };
  return createPortal(<dialog ref={dialog} className="remove-project-dialog" aria-labelledby={`${id}-title`}
    aria-describedby={`${id}-description`} aria-busy={busy} onCancel={e=>{e.preventDefault();if(!submitting.current)onClose();}}
    onClick={e=>{e.stopPropagation();if(e.target!==e.currentTarget||submitting.current)return;
      const r=e.currentTarget.getBoundingClientRect();
      if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)onClose();
    }} onKeyDown={e=>{
      e.stopPropagation();if(e.key!=='Tab')return;
      const buttons=Array.from(e.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
      if(!buttons.length){e.preventDefault();return;}
      const first=buttons[0],last=buttons[buttons.length-1];
      if(e.shiftKey&&(document.activeElement===first||document.activeElement===e.currentTarget)){e.preventDefault();last.focus();}
      else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
    }}>
    <h2 id={`${id}-title`}>移出关注？</h2>
    <div id={`${id}-description`} className="remove-project-copy">
      <p className="remove-project-name">{project.name}</p>
      <p>移出后将停止后续自动更新，并从项目列表与侧栏隐藏。</p>
      <p className="muted">资料与历史记录仍会保留，可在项目列表底部恢复关注。</p>
    </div>
    <ErrorNote error={error}/>
    <div className="remove-project-actions">
      <button ref={cancel} type="button" className="btn btn-ghost" disabled={busy} onClick={onClose}>取消</button>
      <button type="button" className="btn btn-danger" disabled={busy} onClick={remove}>{busy?'正在移出…':'确认移出'}</button>
    </div>
  </dialog>,document.body);
}
