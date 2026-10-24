import assert from "node:assert/strict";
import test from "node:test";
import { apply } from "../src/project_watch/studio/dsh_tools.mjs";

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
