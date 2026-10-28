// Only these public tools are advertised by the isolated sdk-minimal profile.
// The Python gateway owns authorization, validation, journaling and persistence.
import { request } from "node:http";

function reviewedRequest(toolName, args) {
  // One local tool can contain several bounded upstream reviews. The host owns
  // the round deadline; fetch's implicit headers deadline must not cut it short.
  const body = JSON.stringify(args);
  return new Promise((resolve, reject) => {
    const req = request(`${process.env.PW_DSH_GATEWAY}/tools/${toolName}`, {
      method: "POST", agent: false,
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body),
        Authorization: `Bearer ${process.env.PW_DSH_GATEWAY_TOKEN}` },
    }, response => {
      const chunks = [];
      response.on("data", chunk => chunks.push(chunk));
      response.on("error", reject);
      response.on("end", () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
        catch (error) { reject(error); }
      });
    });
    req.on("error", reject);
    req.end(body);
  });
}
export const name = "projectwatch-public-tools";
export const inject = ["tools"];
const string = { type: "string" };
const definitions = [
  ["pw_search", "Search public sources for the confirmed company or a justified related topic.",
    { query: string, category: { type: "string", enum: ["company", "competitor", "industry", "policy"] } }, ["query", "category"]],
  ["pw_collect", "Query a specialist public source with an entity or topic term quoted from saved public evidence. The query must occur in quote. Retains a query scope for later rounds and executes through the host ledger. Choose sponsor versus intervention separately; results remain candidates for attribution review. Does not mark sources inapplicable or establish company ownership.",
    { adapter: { type: "string", enum: ["openalex_affiliation", "openalex_author", "openalex_topic", "patent_search", "clinical_sponsor", "clinical_term", "cn_regulatory"] },
      query: string, source_id: string, quote: string, reason: string }, ["adapter", "query", "source_id", "quote", "reason"]],
  ["pw_read", "Read a discovered source. Collected C sources default to saved record fields; view=page requests the original webpage and returns a separate source ID for page citations, preserving the record. Reuse the returned ID. Cached body windows have next_offset. No arbitrary URL/file access.",
    { source_id: string, offset: { type: "integer", minimum: 0 }, view: { type: "string", enum: ["record", "page"] } }, ["source_id"]],
  ["pw_record", "Retain one evidence-linked fact/news/peer/relation. Relations require relation_type, relation_target appearing in the quote, reason, axis and query, with no exit_rule. Quote must be continuous verbatim text (max 1500 chars) containing all claimed dates/numbers; split records if necessary. Do not add dates to every label or infer event dates from provider_date. Keep qualifications. On record_validation fix all issues together; suggested_quote is only a source-local candidate, not entailment.",
    { kind: {type: "string", enum: ["business", "product", "person", "founded", "location",
        "financing", "milestone", "company", "competitor", "industry", "policy", "peer", "relation"]},
      label: {type: "string", minLength: 1, maxLength: 200}, source_id: string,
      quote: {type: "string", minLength: 6, maxLength: 1500}, text: {type: "string", minLength: 1, maxLength: 600},
      reason: string, axis: string, query: string, exit_rule: string,
      relation_type: {type: "string", enum: ["technology", "product", "indication", "competitor",
        "supplier", "customer", "industry", "policy"]},
      relation_target: string, relation_aliases: { type: "array", items: string, maxItems: 8 } }, ["kind", "label", "source_id", "quote", "text"]],
  ["pw_status", "Inspect host collection receipts and collected_sources before supplemental research. Page saved sources with collection_offset=next_offset; pw_read reads their exact saved fields without another request. Structured records are not page full text.",
    { collection_offset: { type: "integer", minimum: 0 } }, []],
  ["pw_finish", "Persist the public project result. Explicitly state uncovered or partial categories; no implied completeness.",
    { gaps: { type: "object", properties: Object.fromEntries(
      ["company", "products", "people", "competitor", "industry", "policy"].map(k => [k, string])),
      additionalProperties: false } }, ["gaps"]],
];
export function apply(ctx) {
  const allowed = JSON.parse(process.env.PW_DSH_TOOLS || JSON.stringify(definitions.map(x => x[0])));
  const monitor = allowed.includes("pw_context");
  const list = [...definitions,
    ["pw_submit_observations", "Submit source assertions for separate host validation and review. Select supplied reference IDs; do not rewrite quotes.",
      { observations: { type: "array", maxItems: 50, items: { type: "object", additionalProperties: false,
        properties: { text: { type: "string", minLength: 1, maxLength: 3000 },
          event_at: { anyOf: [{ type: "string", minLength: 10, maxLength: 10 }, { type: "null" }] },
          refs: { type: "array", minItems: 1, maxItems: 20, items: {type: "string", minLength: 64, maxLength: 64} } },
        required: ["text", "refs"] } } }, ["observations"]],
    ["pw_submit_records", "Submit typed public candidates, not confirmed facts. Name the subject and category explicitly. Select one immutable part ID and continuous character offsets; never transcribe quotes. Preserve qualifications and provide cited relationship attributes where applicable.",
      { observations: { type: "array", maxItems: 50, items: { type: "object", additionalProperties: false,
        properties: { ...definitions.find(x => x[0] === "pw_record")[2],
          subject: { type: "string", minLength: 1, maxLength: 200 },
          refs: { type: "array", minItems: 1, maxItems: 1, items: {type: "string", minLength: 64, maxLength: 64} },
          start: { type: "integer", minimum: 0 }, end: { type: "integer", minimum: 1 },
          event_at: { anyOf: [{ type: "string", minLength: 10, maxLength: 10 }, { type: "null" }] } },
        required: ["kind", "subject", "label", "text", "refs", "start", "end"] } } }, ["observations"]],
    ["pw_context", "Publish retained public records and inspect evidence, standing relations, temporary questions and due scope. Also inspect host collected_sources; use collection_offset=next_offset for more, and pw_read for stored evidence.",
      { collection_offset: { type: "integer", minimum: 0 } }, []],
    ["pw_explore", "Search a due standing relation or research question using one of its configured queries.",
      { watch_id: string, query: string }, ["watch_id", "query"]],
    ["pw_assess", "Record a cited assessment. Distinguish object_change from evidence_only (research coverage, NOT company progress) and unclear. Evidence-only saves unchanged; unclear saves insufficient. Does not replace human conclusions.",
      { watch_id: string, status: {type: "string", enum: ["progress", "setback", "unchanged", "insufficient"]},
        change_basis: {type: "string", enum: ["object_change", "evidence_only", "unclear"]},
        note: string, refs: {type: "array", items: string, minItems: 1, maxItems: 12} }, ["watch_id", "status", "change_basis", "note", "refs"]],
    ["pw_judge", "Save comprehensive judgment using numbered public evidence and the system JSON contract.",
      { analysis_json: string }, ["analysis_json"]],
    ["pw_revise", "Review scope after judgment. Do not modify context.revision_blocked_ids; explain gaps in reason instead. changes_json is an array; every row needs action/watch_id/why/refs. note and finish also REQUIRE text (cited answer, max 200 chars). set_exit needs exit_rule; explore needs text/axis/queries/exit_rule. [] is valid. On revision_validation fix the listed fields only, without regenerating judgment. Check items: dropped/proposed does not mean applied.",
      { changes_json: string, reason: string }, ["changes_json", "reason"]],
  ].filter(x => allowed.includes(x[0])).map(x => {
    if (monitor && x[0] === "pw_finish") return [
      "pw_finish", "End this reviewed round. Does not mean every topic is answered.", {reason: string}, ["reason"]];
    if (monitor && x[0] === "pw_record") return [x[0], x[1], {...x[2],
      kind: {...x[2].kind, enum: x[2].kind.enum.filter(k => k !== "watch")}}, x[3]];
    if (x[0] === "pw_submit_records") {
      const properties = { ...x[2].observations.items.properties };
      for (const key of ["source_id", "quote", "exit_rule"]) delete properties[key];
      return [x[0], x[1], { observations: {...x[2].observations,
        items: {...x[2].observations.items, properties}} }, x[3]];
    }
    return x;
  });
  for (const [toolName, description, properties, required] of list) {
    ctx.tools.register({
      name: toolName, description,
      parameters: { type: "object", properties, required, additionalProperties: false },
      output: { schema: ["pw_submit_observations", "pw_submit_records"].includes(toolName) ? { type: "object", additionalProperties: false,
        properties: {received: {type: "boolean"}, review_status: {type: "string", enum: ["pending"]}},
        required: ["received", "review_status"] } : {},
        render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }] },
      // Calls are serialized again by the gateway, including model traffic.
      isConcurrencySafe: () => false,
      async execute(args) {
        if (["pw_judge", "pw_context", "pw_finish"].includes(toolName)) return reviewedRequest(toolName, args);
        const response = await fetch(`${process.env.PW_DSH_GATEWAY}/tools/${toolName}`, {
          method: "POST", headers: { "Content-Type": "application/json",
            Authorization: `Bearer ${process.env.PW_DSH_GATEWAY_TOKEN}` },
          body: JSON.stringify(args),
        });
        return await response.json();
      },
    });
  }
}
