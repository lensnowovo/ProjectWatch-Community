// Return to the invoking citation even if polling replaced its DOM node.
export function captureReturnFocus(root,doc=document){
  const active=doc.activeElement;
  if(!root||!active||!root.contains(active))return ()=>{};
  const key=active.closest('[data-reading-anchor]')?.dataset.readingAnchor;
  const signature=node=>`${node.tagName}|${node.getAttribute('aria-label')||node.textContent}`;
  const label=signature(active);
  const visible=node=>node?.isConnected&&node.getBoundingClientRect().height>0;
  return ()=>{
    if(visible(active)){active.focus({preventScroll:true});return;}
    if(!key)return;
    const anchor=[...root.querySelectorAll('[data-reading-anchor]')].find(n=>n.dataset.readingAnchor===key);
    const target=[...(anchor?.querySelectorAll('button,a,summary,input,textarea,select')||[])].find(n=>visible(n)&&signature(n)===label);
    target?.focus({preventScroll:true});
  };
}
