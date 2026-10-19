import assert from "node:assert/strict";
import test from "node:test";

import { uploadBatch } from "../src/project_watch/static/upload_batch.mjs";

test("uploads a batch in order and reports one aggregate result", async () => {
  const files = [{ name: "one.txt" }, { name: "two.pdf" }, { name: "three.xls" }];
  const uploaded = [];
  const updates = [];
  const result = await uploadBatch(files, async (file) => {
    uploaded.push(file.name);
    if (file.name === "two.pdf") {
      const error = new Error("unsupported");
      error.status = 415;
      throw error;
    }
  }, (progress) => updates.push({ ...progress }));

  assert.deepEqual(uploaded, ["one.txt", "two.pdf", "three.xls"]);
  assert.equal(result.attempted, 3);
  assert.equal(result.accepted, 2);
  assert.equal(result.notSent, 0);
  assert.equal(result.stopped, false);
  assert.deepEqual(result.failures, [{ name: "two.pdf", reason: "unsupported" }]);
  assert.deepEqual(updates.map((item) => item.attempted), [0, 1, 2, 3]);
});

test("stops after authorization or transport failure without retrying unsent files", async () => {
  for (const status of [401, undefined, 503]) {
    const uploaded = [];
    const result = await uploadBatch(
      [{ name: "one.txt" }, { name: "two.txt" }, { name: "three.txt" }],
      async (file) => {
        uploaded.push(file.name);
        if (file.name === "two.txt") {
          const error = new Error("connection or permission lost");
          error.status = status;
          throw error;
        }
      },
      () => {},
    );
    assert.deepEqual(uploaded, ["one.txt", "two.txt"]);
    assert.equal(result.attempted, 2);
    assert.equal(result.accepted, 1);
    assert.equal(result.notSent, 1);
    assert.equal(result.stopped, true);
  }
});

test("normalizes a non-string upload error for the failure list", async () => {
  const result = await uploadBatch(
    [{ name: "bad.xls" }],
    async () => { throw { status: 415, message: { detail: "unsupported" } }; },
    () => {},
  );
  assert.equal(typeof result.failures[0].reason, "string");
});
