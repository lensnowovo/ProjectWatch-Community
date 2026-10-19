import test from 'node:test';
import assert from 'node:assert/strict';
import {captureReadingPosition} from '../src/project_watch/static/reading_position.mjs';

test('new content above a paragraph preserves its visual position and keyboard focus',()=>{
  const active={isConnected:true};let top=100,focused=false;
  const replacement={focus(options){assert.deepEqual(options,{preventScroll:true});focused=true;}};
  const node={dataset:{readingAnchor:'source-backed-item'},getBoundingClientRect:()=>({top,bottom:top+200,height:200}),contains:n=>n===active,querySelectorAll:()=>active.isConnected?[active]:[replacement]};
  const root={querySelectorAll:()=>[node]},moves=[];
  const restore=captureReadingPosition([root],{innerHeight:900,scrollBy:(x,y)=>moves.push([x,y])},{activeElement:active});
  top=450;active.isConnected=false;restore();
  assert.deepEqual(moves,[[0,350]]);assert.equal(focused,true);
});

test('hidden views and removed items cannot move the page or steal outside focus',()=>{
  const outside={isConnected:true};let nodes=[];const moves=[];
  const root={querySelectorAll:()=>nodes},page={innerHeight:900,scrollBy:(...args)=>moves.push(args)};
  nodes=[{dataset:{readingAnchor:'hidden'},getBoundingClientRect:()=>({height:0,top:0,bottom:0})}];
  captureReadingPosition([root],page,{activeElement:outside})();
  nodes=[{dataset:{readingAnchor:'visible'},getBoundingClientRect:()=>({height:100,top:100,bottom:200}),contains:()=>false}];
  const restore=captureReadingPosition([root],page,{activeElement:outside});nodes=[];restore();
  assert.deepEqual(moves,[]);
});
