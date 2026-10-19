// Retain the paragraph and keyboard target, not just an absolute page offset.
// A new item above the paragraph must not pull the reader away from it.
export function captureReadingPosition(roots, page=window, doc=document) {
  const nodes=roots.flatMap(root=>[...root.querySelectorAll('[data-reading-anchor]')]);
  const anchor=nodes.find(node=>{const r=node.getBoundingClientRect();return r.height>0&&r.bottom>64&&r.top<page.innerHeight;});
  if(!anchor)return ()=>{};
  const key=anchor.dataset.readingAnchor,top=anchor.getBoundingClientRect().top;
  const active=doc.activeElement;
  const focusAnchor=nodes.find(node=>node.contains(active));
  const selector='button,a,summary,input,textarea,select';
  const focusIndex=focusAnchor?[...focusAnchor.querySelectorAll(selector)].indexOf(active):-1;
  const focusKey=focusAnchor?.dataset.readingAnchor;
  return ()=>{
    const current=roots.flatMap(root=>[...root.querySelectorAll('[data-reading-anchor]')]);
    const next=current.find(node=>node.dataset.readingAnchor===key&&node.getBoundingClientRect().height>0);
    if(next)page.scrollBy(0,next.getBoundingClientRect().top-top);
    if(focusIndex>=0&&!active.isConnected){
      current.find(node=>node.dataset.readingAnchor===focusKey&&node.getBoundingClientRect().height>0)?.querySelectorAll(selector)[focusIndex]?.focus({preventScroll:true});
    }
  };
}
