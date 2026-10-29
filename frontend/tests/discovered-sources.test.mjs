import assert from 'node:assert/strict';
import {createRequire, Module} from 'node:module';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
const require = createRequire(new URL('../package.json', import.meta.url));
const {buildSync} = require('esbuild'), React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const root = fileURLToPath(new URL('../', import.meta.url));
const mod = new Module(root+'discovered-sources-test.cjs');
mod.filename = root+'discovered-sources-test.cjs'; mod.paths = require.resolve.paths('react');
mod._compile(buildSync({stdin:{contents:"export {DiscoveredSourceList} from './src/DiscoveredSources';",resolveDir:root,loader:'tsx'},
  bundle:true,write:false,platform:'node',format:'cjs',packages:'external',loader:{'.css':'empty','.svg':'text'}}).outputFiles[0].text,mod.filename);
const render = items => renderToStaticMarkup(React.createElement(mod.exports.DiscoveredSourceList,{data:{items,total:items.length,pending:items.length,offset:0,next_offset:null}}));
const item = {id:'synthetic',title:'Synthetic source <b>untrusted</b>',url:'https://example.org/source',source:'Synthetic publisher',
  published_at:null,collected_at:'2026-10-29',read_scope:'summary',source_type:'search_summary',processing_status:'pending'};

test('unreviewed source shows provenance and uncertainty without invented analysis',()=>{
 const html=render([item]);
 assert.match(html,/搜索摘要/); assert.match(html,/发布日期未知/); assert.match(html,/归属待确认/);
 assert.match(html,/&lt;b&gt;untrusted&lt;\/b&gt;/); assert.doesNotMatch(html,/完整正文|已确认|新增事件/);
});
test('structured records are not presented as full article text and unsafe links are inert',()=>{
 const html=render([{...item,source_type:'structured_record',read_scope:'partial',url:'javascript:alert(1)',processing_status:'organized'}]);
 assert.match(html,/结构化记录/); assert.match(html,/已整理/);
 assert.doesNotMatch(html,/href=|完整正文|javascript:/);
});
