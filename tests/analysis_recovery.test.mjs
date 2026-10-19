import test from 'node:test';
import assert from 'node:assert/strict';
import {failureCopy,retryPayload} from '../src/project_watch/static/analysis_recovery.mjs';

test('failure copy is plain language and never renders arbitrary provider errors',()=>{
  assert.match(failureCopy('incomplete_response'),/完整/);
  assert.match(failureCopy('evidence_validation'),/证据/);
  assert.match(failureCopy('legacy_unknown'),/未保存/);
  assert.equal(failureCopy('<script>private key</script>'),'本次未取得可用结果');
});
test('retry needs explicit confirmation and a server-bound disclosure',()=>{
  const viewed={disclosure_digest:'a'.repeat(64),private:'unused'};
  assert.throws(()=>retryPayload(viewed,false));
  assert.throws(()=>retryPayload(null,true));
  assert.throws(()=>retryPayload({disclosure_digest:'wrong'},true));
  assert.deepEqual(retryPayload(viewed,true),{consent:true,disclosure_digest:'a'.repeat(64)});
});
