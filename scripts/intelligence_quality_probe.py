"""Bounded, real DSH evaluation using ONLY the checked-in synthetic corpus.

No project DB, file discovery, public searches or publication. Expected answers
and reviewer boundaries are never sent to the model. Failed calls stay in the
receipt; there is no retry. Input on stdin: {"config": {...}, "key": "..."}.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from project_watch.analysis_failures import failure_code
from project_watch.dsh_executor import DshProposalModel
from project_watch.external_analysis import Packet, digest
from project_watch.model_provider import ModelProviderConfig


class CompactProbeModel(DshProposalModel):
    """Pin the compact wire even if a later release changes the default."""

    @staticmethod
    def _external_analysis_prompt(packet):
        from project_watch.external_analysis_compact import compact_prompt
        return compact_prompt(packet)

    @staticmethod
    def _resolve_external_analysis(packet, output):
        from project_watch.external_analysis_compact import resolve_compact_analysis
        return resolve_compact_analysis(packet, output)


class SelectionProbeModel(DshProposalModel):
    """Keep the original baseline explicitly selectable for controlled comparison."""

    @staticmethod
    def _external_analysis_prompt(packet):
        from project_watch.external_analysis_selection import selection_prompt
        return selection_prompt(packet)

    @staticmethod
    def _resolve_external_analysis(packet, output):
        from project_watch.external_analysis_selection import resolve_analysis_selection
        return resolve_analysis_selection(packet, output)


class MonitoringProbeModel(CompactProbeModel):
    """Only a relevance instruction changes; the wire/validator are identical."""

    @staticmethod
    def _external_analysis_prompt(packet):
        from project_watch.external_analysis_compact import monitoring_candidate_prompt
        return monitoring_candidate_prompt(packet)


def packet_for(profile, case):
    """Only profile/title are evidence. Do not leak expected labels into the prompt."""
    version = digest(case["title"])
    refs = [dict(ref_id="news:title", kind="external", text=case["title"],
                 source_version=version, locator="synthetic headline"),
            dict(ref_id="material:0", kind="internal", text=profile,
                 source_version=digest(profile), locator="synthetic profile",
                 source_id="synthetic-profile", ordinal=1, offset=0)]
    return Packet.model_validate(dict(
        rule_version="external-analysis-v1", project_id="synthetic-"+case["profile"],
        signal_id=case["id"], signal_version=version, category="industry",
        term="合成行业案例", published_at="2026-10-06T00:00:00+00:00",
        source_url="https://example.org/synthetic/"+case["id"],
        content_scope="headline_and_feed_summary_only", references=refs,
    )).model_dump()


def evaluate(corpus, cases, model, emit):
    rows = []
    for case in cases:
        packet = packet_for(corpus["profiles"][case["profile"]], case)
        frozen = digest(packet)
        sends = 0

        def before_send(_request_body, *, packet=packet, frozen=frozen):
            nonlocal sends
            if sends or digest(packet) != frozen:
                raise ValueError("synthetic scope changed or duplicate dispatch")
            sends += 1

        row = {"case_id": case["id"], "profile": case["profile"], "packet_digest": frozen,
               "expected_relevance": case["relevance"], "review_boundary": case["boundary"],
               "review_status": "needs_review"}
        started = time.monotonic()
        try:
            row["analysis"] = model.analyze_external(packet, before_send=before_send)
            row["status"] = "succeeded"
        except Exception as exc:
            row.update(status="failed", error_code=failure_code(exc))
        row.update(seconds=round(time.monotonic()-started, 2), dispatched=sends)
        rows.append(row)
        emit(row)
    return {"cases": len(rows), "succeeded": sum(r["status"] == "succeeded" for r in rows),
            "dispatched": sum(r["dispatched"] for r in rows),
            "independent_reviewer_gate": "not_evaluated"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-paid-model", action="store_true", required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--cases", help="Comma-separated fixed corpus IDs; omitted means all 30")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--contract", choices=("selection-v1", "compact-v2", "monitoring-candidate"), default="compact-v2")
    args = parser.parse_args()
    corpus = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/intelligence-quality-cases.json").read_text(encoding="utf-8"))
    ids = args.cases.split(",") if args.cases else [c["id"] for c in corpus["cases"]]
    cases = [c for c in corpus["cases"] if c["id"] in ids]
    if len(set(ids)) != len(ids) or len(cases) != len(ids) or not 1 <= len(cases) <= args.max_calls <= 30:
        parser.error("Use unique corpus IDs and an explicit quota of 1–30 calls")
    secret_input = json.load(sys.stdin)
    config = ModelProviderConfig(**secret_input["config"])
    model_class = {'compact-v2': CompactProbeModel, 'selection-v1': SelectionProbeModel,
                   'monitoring-candidate': MonitoringProbeModel}[args.contract]
    model = model_class(config, secret_input.pop("key"), max_runs=args.max_calls)
    # Exclusive JSONL: preserve partial runs on interruption; never overwrite.
    with args.receipt.open("x", encoding="utf-8") as output:
        def emit(row):
            output.write(json.dumps(row, ensure_ascii=False)+"\n")
            output.flush()
            print(json.dumps({k: row[k] for k in ("case_id", "status", "seconds", "dispatched")}), flush=True)
        output.write(json.dumps({"synthetic": True, "model": config.model, "base_url": config.base_url,
                                "contract": args.contract,
                                "prompt_digests": {c['id']: digest(model._external_analysis_prompt(
                                    packet_for(corpus['profiles'][c['profile']], c))) for c in cases},
                                "fixture_sha256": digest(corpus), "case_ids": ids,
                                "started_at": datetime.now(UTC).isoformat()})+"\n")
        output.flush()
        summary = evaluate(corpus, cases, model, emit)
        output.write(json.dumps({"summary": summary})+"\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
