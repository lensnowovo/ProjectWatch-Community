import test from 'node:test';
import assert from 'node:assert/strict';
import {selectAttention,monitorCopy} from '../src/project_watch/static/attention.mjs';
test('material arrival is not a business event; historical news stays out of highlights',()=>{
 assert.deepEqual(selectAttention([{kind:'materials'},{kind:'news',freshness:'historical'}]),[]);
});
test('current attributed news deduplicates exact URLs and highlights at most five',()=>{
 const news=Array.from({length:8},(_,i)=>({kind:'news',project_id:'p',url:`https://example.org/${i}`,title:'News',freshness:'current',time:`2026-10-0${i+1}`}));
 const result=selectAttention([...news,news[7]]);
 assert.equal(result.length,5);assert.equal(result[0].time,'2026-10-08');
 assert.ok(result.every(x=>x.reason));
});
test('monitor failure, not enabled and no new message are different',()=>{
 assert.match(monitorCopy([],true),/无法/);
 assert.match(monitorCopy([],false),/未开启/);
 assert.match(monitorCopy([{enabled:false,last_run_status:'completed'}]),/已暂停/);
 assert.match(monitorCopy([{enabled:true,last_run_status:'unreachable'}]),/未成功/);
 assert.match(monitorCopy([{enabled:true,last_run_status:'no_results'}]),/未发现/);
});
