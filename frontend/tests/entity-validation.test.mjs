import assert from 'node:assert/strict';
import test from 'node:test';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
const require=createRequire(new URL('../package.json',import.meta.url));
const entry=fileURLToPath(new URL('../src/entityValidation.ts',import.meta.url));
const mod=new Module(entry);
mod._compile(require('esbuild').buildSync({entryPoints:[entry],bundle:true,write:false,platform:'node',format:'cjs'}).outputFiles[0].text,entry);
const {entityIssue,validationMessage,entityFields}=mod.exports;
test('candidate overlong stage identifies the actual field without dropping text',()=>{
 const entity={legal_name:'合成科技有限公司',stage:'C轮（'+ '融资说明'.repeat(20)+'）'};
 const before=structuredClone(entity);
 assert.match(entityIssue(entity),/融资阶段.*60/); assert.deepEqual(entity,before);
 assert.equal(entityIssue({...entity,stage:'C轮'}),null);
});
test('every string limit agrees at boundary and counts unicode codepoints',()=>{
 for(const [key,[label,max]] of Object.entries(entityFields)){
  const entity={legal_name:'合成有限公司',[key]:'字'.repeat(max)};
  assert.equal(entityIssue(entity),null); assert.ok(entityIssue({...entity,[key]:'字'.repeat(max+1)}).includes(label));
 }
 assert.equal(entityIssue({legal_name:'合成',stage:'😀'.repeat(60)}),null);
});
test('structured server error exposes no raw validator input or internal enum',()=>{
 assert.match(validationMessage([{loc:['body','entity','stage'],type:'string_too_long',input:'private'}]),/融资阶段.*60/);
 assert.equal(validationMessage([{loc:['body','kind'],type:'literal_error',msg:'internal enum',input:'secret'}]),'请检查填写的信息，修改后再提交。');
 assert.equal(validationMessage([null,42]),'请检查填写的信息，修改后再提交。');
});
