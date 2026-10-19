import test from 'node:test';
import assert from 'node:assert/strict';
import {captureReturnFocus} from '../src/project_watch/static/return_focus.mjs';

function setup(){
  const calls=[];
  const button={isConnected:true,tagName:'BUTTON',textContent:'BP · line 1',getAttribute:()=>null,getBoundingClientRect:()=>({height:30}),focus:options=>calls.push(options)};
  const anchor={dataset:{readingAnchor:'news-one'},querySelectorAll:()=>[button]};
  button.closest=()=>anchor;
  const root={contains:n=>n===button,querySelectorAll:()=>[anchor]};
  return {button,anchor,root,calls,doc:{activeElement:button}};
}
test('closing source returns keyboard focus without scrolling',()=>{
  const f=setup();captureReturnFocus(f.root,f.doc)();
  assert.deepEqual(f.calls,[{preventScroll:true}]);
});
test('polling replacement restores the same citation, not another button',()=>{
  const f=setup(),restore=captureReturnFocus(f.root,f.doc);f.button.isConnected=false;
  const other={...f.button,isConnected:true,textContent:'different citation',focus:()=>assert.fail('wrong citation')};
  f.anchor.querySelectorAll=()=>[other,{...f.button,isConnected:true}];
  restore();assert.equal(f.calls.length,1);
});
test('removed or hidden citation cannot steal focus',()=>{
  const f=setup(),restore=captureReturnFocus(f.root,f.doc);f.button.getBoundingClientRect=()=>({height:0});
  restore();assert.equal(f.calls.length,0);
  f.button.isConnected=false;f.root.querySelectorAll=()=>[];restore();assert.equal(f.calls.length,0);
});
