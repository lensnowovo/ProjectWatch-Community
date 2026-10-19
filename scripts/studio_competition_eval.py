"""Fixed synthetic competition replay. Offline by default; --model may incur fees.

Model mode reads only PW_MODEL_* environment, never private Studio settings.
It measures semantic review on authored facts, not discovery or BP extraction.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from project_watch.studio import competition_evaluation as evaluation
from project_watch.studio.usage import UsageCounter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--cases', default=str(Path(__file__).resolve().parents[1] / 'tests/fixtures/studio_eval/competition.json'))
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--out', required=True, help='JSON output, preferably outside the repository')
    parser.add_argument('--model', action='store_true', help='Explicitly run paid semantic model review using PW_MODEL_*')
    args = parser.parse_args(argv)
    reviewer = None
    counter = UsageCounter()
    usage = counter.totals
    if args.model:
        from project_watch.studio.competition_research import REVIEW
        from project_watch.studio.http import Http, urllib_transport
        from project_watch.studio.llm import LLM
        from project_watch.studio.settings import ModelConfig

        config = ModelConfig(os.getenv('PW_MODEL_PROTOCOL', 'openai'), os.getenv('PW_MODEL_BASE_URL', ''),
                             os.getenv('PW_MODEL_ID', ''), os.getenv('PW_MODEL_KEY', ''), max_output_tokens=4000)
        if not config.ready:
            parser.error('--model requires PW_MODEL_BASE_URL / PW_MODEL_ID / PW_MODEL_KEY')
        llm = LLM(config, counter.http(Http(urllib_transport(os.getenv('PW_PROXY', '')))))
        def reviewer(context):
            return llm.json(REVIEW, json.dumps(context, ensure_ascii=False), max_tokens=4000)
    with tempfile.TemporaryDirectory(prefix='projectwatch-synthetic-eval-') as workdir:
        report = evaluation.run(evaluation.load_cases(args.cases), workdir, repeat=args.repeat, reviewer=reviewer)
    report['usage'] = usage
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('rows', 'per_sector', 'rounds')}, ensure_ascii=False, indent=2))
    summary = report['summary']
    return int(bool(summary['failed_cases'] or summary['failed_calls'] or summary['hard_negative_promotions'] or summary['correct'] != summary['case_runs']))


if __name__ == '__main__':
    raise SystemExit(main())
