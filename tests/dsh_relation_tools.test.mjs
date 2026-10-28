import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { apply } from "../src/project_watch/studio/dsh_tools.mjs";

test('material role exposes only structured submission with a typed receipt', () => {
  const previous = process.env.PW_DSH_TOOLS;
  try {
    process.env.PW_DSH_TOOLS = JSON.stringify(['pw_submit_observations']);
    const tools = [];
    apply({tools:{register(tool) { tools.push(tool); }}});
    assert.deepEqual(tools.map(t => t.name), ['pw_submit_observations']);
    const tool = tools[0];
    assert.equal(tool.parameters.additionalProperties, false);
    assert.equal(tool.parameters.properties.observations.items.additionalProperties, false);
    assert.deepEqual(tool.parameters.properties.observations.items.required, ['text', 'refs']);
    assert.equal(tool.parameters.properties.observations.items.properties.refs.items.minLength, 64);
    assert.equal(tool.output.schema.additionalProperties, false);
    assert.deepEqual(tool.output.schema.required, ['received', 'review_status']);
    assert.deepEqual(tool.output.schema.properties.review_status.enum, ['pending']);
  } finally {
    if (previous === undefined) delete process.env.PW_DSH_TOOLS;
    else process.env.PW_DSH_TOOLS = previous;
  }
});

for (const toolName of ['pw_judge', 'pw_context', 'pw_finish']) {
test(`${toolName} waits for the host and preserves rejection feedback without retry`, async () => {
  const previous = { ...process.env };
  let calls = 0;
  const server = createServer((req, res) => {
    calls++;
    assert.equal(req.url, `/tools/${toolName}`);
    assert.equal(req.headers.authorization, "Bearer synthetic-only");
    const chunks = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", () => {
      assert.deepEqual(JSON.parse(Buffer.concat(chunks)), { analysis_json: "{}" });
      setTimeout(() => {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "judgment_evidence_review", saved: false }));
      }, Number(process.env.PW_TEST_JUDGMENT_DELAY_MS || 20));
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    process.env.PW_DSH_TOOLS = JSON.stringify([toolName]);
    process.env.PW_DSH_GATEWAY = `http://127.0.0.1:${server.address().port}`;
    process.env.PW_DSH_GATEWAY_TOKEN = "synthetic-only";
    const tools = [];
    apply({ tools: { register(tool) { tools.push(tool); } } });
    assert.deepEqual(await tools[0].execute({ analysis_json: "{}" }), {
      error: "judgment_evidence_review", saved: false,
    });
    assert.equal(calls, 1);
  } finally {
    for (const key of ["PW_DSH_TOOLS", "PW_DSH_GATEWAY", "PW_DSH_GATEWAY_TOKEN"]) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
    await new Promise(resolve => server.close(resolve));
  }
});
}

test("DSH advertises standing relations instead of root-level question creation", () => {
  const previous = process.env.PW_DSH_TOOLS;
  try {
    for (const allowed of [null, ["pw_record", "pw_context", "pw_revise", "pw_finish"]]) {
      if (allowed) process.env.PW_DSH_TOOLS = JSON.stringify(allowed);
      else delete process.env.PW_DSH_TOOLS;
      const tools = [];
      apply({ tools: { register(tool) { tools.push(tool); } } });
      const record = tools.find(t => t.name === "pw_record");
      assert.ok(record.parameters.properties.kind.enum.includes("relation"));
      assert.ok(!record.parameters.properties.kind.enum.includes("watch"));
      assert.ok(record.parameters.properties.relation_type.enum.includes("indication"));
      assert.ok(record.parameters.properties.relation_target);
      assert.equal(record.parameters.additionalProperties, false);
    }
  } finally {
    if (previous === undefined) delete process.env.PW_DSH_TOOLS;
    else process.env.PW_DSH_TOOLS = previous;
  }
});
