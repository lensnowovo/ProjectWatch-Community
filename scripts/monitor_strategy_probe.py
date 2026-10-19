"""Explicit operator-only strategy probe, not a production background worker.

Configuration and pre-reviewed excerpts enter via stdin. Full output is stored
only at an explicitly chosen private receipt path; stdout contains counts/usage.
Default preview does not read stdin or call a model. No source search is executed.
"""
import argparse
import json
import sys
from pathlib import Path

from project_watch.dsh_executor import DshProposalModel
from project_watch.model_provider import ModelProviderConfig
from project_watch.monitor_strategy import OUTPUT_TOKENS, Packet, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args(argv)
    if not args.run:
        print(json.dumps({'mode': 'preview_no_network', 'purpose': 'BP monitoring strategy',
                          'max_calls': 1, 'max_output_tokens': OUTPUT_TOKENS,
                          'starts_public_collection': False}))
        return 0
    model = None
    try:
        if not args.receipt or not args.receipt.is_absolute() or args.receipt.exists():
            raise ValueError('new absolute private receipt path required')
        raw = sys.stdin.read(300001)
        if len(raw) > 300000:
            raise ValueError('input too large')
        body = json.loads(raw)
        config = ModelProviderConfig(**body['provider'])
        packet = Packet.model_validate(body['packet']).model_dump()
        approved = body['approved_packet_digest']
        if body.get('consent') is not True or approved != digest(packet):
            raise ValueError('explicit packet approval required')
        model = DshProposalModel(config, body['api_key'], max_runs=1)
        dispatched = False

        def gate(request):
            nonlocal dispatched
            if dispatched or digest(packet) != approved:
                raise ValueError('packet approval changed or already dispatched')
            dispatched = True

        strategy = model.plan_monitoring(packet, before_send=gate)
        result = {'strategy': strategy, 'model': config.model, 'receipt': model.receipt,
                  'scope': 'operator_reviewed_BP_excerpts_only', 'collection_started': False}
        # A real-material receipt is an explicitly local private artifact, not a
        # repo fixture. Exclusive create prevents overwriting earlier evidence.
        with args.receipt.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'passed': True, 'scope_count': len(strategy['scopes']),
                          'categories': sorted({s['category'] for s in strategy['scopes']}),
                          'model': config.model, 'receipt': model.receipt,
                          'collection_started': False}, ensure_ascii=True))
        return 0
    except Exception as exc:
        cause = exc.__cause__ or exc
        known = {'search anchor not present in its evidence', 'duplicate public search anchor',
                 'unknown or duplicate source reference', 'model output too large',
                 'model did not complete a bounded response', 'DSH turn timed out',
                 'explicit packet approval required'}
        diagnostics = {'cause_type': type(cause).__name__}
        if str(cause) in known:
            diagnostics['code'] = str(cause)
        if hasattr(cause, 'errors'):
            diagnostics['schema_error_types'] = sorted({e['type'] for e in cause.errors(include_input=False)})
        print(json.dumps({'passed': False, 'error_type': type(exc).__name__,
                          **diagnostics, 'receipt': model.receipt if model else {},
                          'details_suppressed': True, 'automatic_retry': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
