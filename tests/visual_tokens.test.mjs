import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const css=readFileSync(new URL('../src/project_watch/static/tokens.css',import.meta.url),'utf8');
const token=name=>css.match(new RegExp(`--${name}:(#[a-f0-9]+)`))[1];
function luminance(hex){return hex.slice(1).match(/../g).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);}
function ratio(a,b){const values=[luminance(a),luminance(b)].sort((x,y)=>y-x);return(values[0]+.05)/(values[1]+.05);}
test('primary and secondary text meet AA on the neutral canvas',()=>{
  for(const name of ['ink','muted'])assert.ok(ratio(token(name),token('canvas'))>=4.5);
  assert.ok(ratio(token('focus'),token('canvas'))>=3);
});
test('neutral theme includes no external font dependencies',()=>{
  assert.ok(!css.includes('https://'));
  const client=readFileSync(new URL('../src/project_watch/static/client.css',import.meta.url),'utf8');
  assert.ok(client.includes('prefers-reduced-motion'));
});
