"""Opt-in model-quality evaluation on synthetic cases. Credentials come only from PW_* environment.

  PW_MODEL_PROTOCOL=openai PW_MODEL_BASE_URL=https://api.deepseek.com/v1 PW_MODEL_ID=deepseek-chat \
  PW_MODEL_KEY=... python scripts/studio_eval.py --repeat 2 --out eval-report

Writes <out>.json and <out>.md. Never prints the key, request headers or raw model replies.
Exit code 1 on failed calls or an unmet --min-* threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from project_watch.studio import evaluation
from project_watch.studio.http import Http, urllib_transport
from project_watch.studio.llm import LLM
from project_watch.studio.settings import ModelConfig
from project_watch.studio.usage import UsageCounter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", default=str(Path(__file__).resolve().parents[1] / "tests/fixtures/studio_eval/cases.json"))
    parser.add_argument("--repeat", type=int, default=1, help="run each suite N times (model answers vary)")
    parser.add_argument("--out", default="studio-eval", help="output path prefix (.json and .md)")
    parser.add_argument("--min-extraction-recall", type=float, default=None)
    parser.add_argument("--min-triage-relevance", type=float, default=None)
    parser.add_argument("--min-same-event", type=float, default=None)
    args = parser.parse_args(argv)

    env = os.environ
    config = ModelConfig(env.get("PW_MODEL_PROTOCOL", "openai"), env.get("PW_MODEL_BASE_URL", "").rstrip("/"),
                         env.get("PW_MODEL_ID", ""), env.get("PW_MODEL_KEY", ""))
    if not config.ready:
        print("缺少 PW_MODEL_BASE_URL / PW_MODEL_ID / PW_MODEL_KEY，跳过评测。")
        return 2
    counter = UsageCounter()
    usage = counter.totals
    llm = LLM(config, counter.http(Http(urllib_transport(env.get("PW_PROXY", "")))))
    report = evaluation.run(llm, evaluation.load_cases(args.cases), repeat=max(1, args.repeat))
    report["model"] = {"protocol": config.protocol, "model": config.model, "base_url_host": config.base_url.split("/")[2]}
    report["usage"] = usage
    usage['calls'] = usage['provider_requests']  # Compatible field, now actual HTTP attempts.
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(f"{args.out}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    text = evaluation.markdown(report, model=config.model) + (
        f"\n模型请求 {usage['calls']} 次；完整用量 {usage['known_calls']} 次、用量不全 {usage['unknown_calls']} 次。"
        f"已记录输入 {usage['input_tokens']} / 输出 {usage['output_tokens']} tokens（非账单）。\n")
    Path(f"{args.out}.md").write_text(text, encoding="utf-8")
    sys.stdout.write(text)

    s = report["summary"]
    checks = [(args.min_extraction_recall, s["extraction_recall"]),
              (args.min_triage_relevance, s["triage_relevance_accuracy"]),
              (args.min_same_event, s["same_event_accuracy"])]
    return 1 if s["failed_calls"] or any(minimum is not None and (value or 0) < minimum
                                       for minimum, value in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
