import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { apply } from "../src/project_watch/studio/dsh_tools.mjs";

test('new retrieval tools are frozen opt-in; legacy schema does not change', () => {
  const previous = {...process.env};
  const schema = () => {
    const tools = [];
    apply({tools: {register(tool) {tools.push(tool);}}});
    return tools.find(t => t.name === 'pw_collect').parameters;
  };
  try {
    process.env.PW_DSH_TOOLS = JSON.stringify(['pw_collect', 'pw_finish']);
    delete process.env.PW_DSH_RETRIEVAL_CONTRACT;
    const legacy = schema();
    assert.ok(!legacy.properties.adapter.enum.includes('google_patent'));
    process.env.PW_DSH_RETRIEVAL_CONTRACT = 'evidence-query-v2';
    const current = schema();
    assert.ok(current.properties.adapter.enum.includes('europepmc_affiliation'));
    assert.ok(current.properties.adapter.enum.includes('google_patent'));
    assert.ok(current.properties.relationship.enum.includes('subsidiary'));
    delete process.env.PW_DSH_RETRIEVAL_CONTRACT;
    assert.deepEqual(schema(), legacy);
  } finally {
    for (const key of ['PW_DSH_TOOLS', 'PW_DSH_RETRIEVAL_CONTRACT']) {
      if (previous[key] === undefined) delete process.env[key]; else process.env[key] = previous[key];
    }
  }
});

test('bibliographic schema is opt-in and legacy material wires remain unchanged', () => {
  const previous = { tools: process.env.PW_DSH_TOOLS, records: process.env.PW_DSH_RESEARCH_RECORDS };
  try {
    process.env.PW_DSH_TOOLS = JSON.stringify(['pw_submit_records']);
    function schema() {
      const tools = [];
      apply({tools: {register(tool) { tools.push(tool); }}});
      return tools[0].parameters.properties.observations.items.properties;
    }
    delete process.env.PW_DSH_RESEARCH_RECORDS;
    const legacy = schema();
    assert.ok(!legacy.kind.enum.includes('paper'));
    assert.equal(legacy.research_relation, undefined);
    process.env.PW_DSH_RESEARCH_RECORDS = 'bibliography-v1';
    const current = schema();
    assert.ok(current.kind.enum.includes('paper') && current.kind.enum.includes('patent'));
    assert.deepEqual(current.research_relation.enum, ['author_affiliation', 'applicant', 'technical_relevance', 'unverified']);
    delete process.env.PW_DSH_RESEARCH_RECORDS;
    assert.deepEqual(schema(), legacy);
  } finally {
    for (const [key, value] of [['PW_DSH_TOOLS', previous.tools], ['PW_DSH_RESEARCH_RECORDS', previous.records]]) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test('independent question tools require an answer state without legacy judgment or source tools', () => {
  const previous = process.env.PW_DSH_TOOLS;
  try {
    process.env.PW_DSH_TOOLS = JSON.stringify(['pw_context', 'pw_assess', 'pw_status', 'pw_finish']);
    const tools = [];
    apply({tools:{register(tool) { tools.push(tool); }}});
    assert.equal(tools.length, 4);
    const assess = tools.find(t => t.name === 'pw_assess');
    assert.ok(assess.parameters.required.includes('answer_status'));
    assert.ok(assess.parameters.required.includes('gaps'));
    assert.match(tools.find(t => t.name === 'pw_context').description, /No collection or publication/);
    assert.match(tools.find(t => t.name === 'pw_finish').description, /without an answer/);
  } finally {
    if (previous === undefined) delete process.env.PW_DSH_TOOLS;
    else process.env.PW_DSH_TOOLS = previous;
  }
});

test('independent scope role exposes no analysis, source or internal tools', () => {
  const previous = process.env.PW_DSH_TOOLS;
  try {
    const allowed = ['pw_context', 'pw_scope', 'pw_status', 'pw_finish'];
    process.env.PW_DSH_TOOLS = JSON.stringify(allowed);
    const tools = [];
    apply({tools:{register(tool) { tools.push(tool); }}});
    assert.deepEqual(new Set(tools.map(t => t.name)), new Set(allowed));
    assert.match(tools.find(t => t.name === 'pw_scope').description, /No comprehensive judgment prerequisite/);
    assert.deepEqual(tools.find(t => t.name === 'pw_finish').parameters.required, ['reason']);
  } finally {
    if (previous === undefined) delete process.env.PW_DSH_TOOLS;
    else process.env.PW_DSH_TOOLS = previous;
  }
});

test('bounded collection role cannot publish records or alter monitoring scope', () => {
  const previous = process.env.PW_DSH_TOOLS;
  try {
    const allowed = ['pw_search', 'pw_read', 'pw_collect', 'pw_status', 'pw_finish'];
    process.env.PW_DSH_TOOLS = JSON.stringify(allowed);
    const tools = [];
    apply({tools:{register(tool) { tools.push(tool); }}});
    assert.deepEqual(new Set(tools.map(t => t.name)), new Set(allowed));
    assert.match(tools.find(t => t.name === 'pw_finish').description, /collection pass only/);
    assert.deepEqual(tools.find(t => t.name === 'pw_read').parameters.properties.view.enum, ['record', 'page']);
  } finally {
    if (previous === undefined) delete process.env.PW_DSH_TOOLS;
    else process.env.PW_DSH_TOOLS = previous;
  }
});

test('typed material role requires attribution and locations without exposing legacy extraction', () => {
  const previous = process.env.PW_DSH_TOOLS;
  try {
    process.env.PW_DSH_TOOLS = JSON.stringify(['pw_submit_records']);
    const tools = [];
    apply({tools:{register(tool) { tools.push(tool); }}});
    assert.deepEqual(tools.map(t => t.name), ['pw_submit_records']);
    const item = tools[0].parameters.properties.observations.items;
    assert.deepEqual(item.required, ['kind', 'subject', 'label', 'text', 'refs', 'start', 'end']);
    assert.equal(item.properties.text.maxLength, 600);
    assert.equal(item.properties.refs.maxItems, 1);
    for (const field of ['quote', 'source_id', 'exit_rule']) assert.equal(item.properties[field], undefined);
    assert.ok(item.properties.relation_type.enum.includes('customer'));
    assert.deepEqual(tools[0].output.schema.required, ['received', 'review_status']);
  } finally {
    if (previous === undefined) delete process.env.PW_DSH_TOOLS;
    else process.env.PW_DSH_TOOLS = previous;
  }
});

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
