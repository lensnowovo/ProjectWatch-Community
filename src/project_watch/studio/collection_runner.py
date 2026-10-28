"""Serial host dispatch and ingestion under an existing job execution guard."""
from project_watch.studio.http import HttpError


def run_collection(ledger, *, collect, ingest, guard):
    """Run frozen pending units with durable fetched payloads before ingestion.

    Adapters use the existing journalled HTTP client, return request provenance
    and explicit truncation, and never swallow errors as empty lists. Ingestion
    must upsert by source identity: a crash after saving but before the receipt
    checkpoint can repeat ingestion, but must not duplicate stored records.
    No model text enters this execution contract.
    """
    execution = ledger.execution
    plan = ledger.plan
    for unit in plan['units']:
        guard()
        old = next((r for r in ledger.receipts if r['unit_id'] == unit['id']), None)
        if old and old['outcome'] == 'unknown':
            raise HttpError('request_outcome_unknown')
        if unit['disposition'] != 'pending' or (old and old['outcome'] != 'running'):
            continue
        receipt = dict(round_id=plan['round_id'], unit_id=unit['id'], outcome='running')
        ledger.record(receipt)
        payload_stage = 'collection_payload:' + unit['id'][:40]
        try:
            payload = execution.read_checkpoint(payload_stage)
            if payload is None:
                try:
                    payload = collect(unit)
                    _validate_payload(payload)
                except (ValueError, KeyError, TypeError) as exc:
                    raise HttpError('invalid_collection_response') from exc
                guard()
                execution.checkpoint_chunked(payload_stage, payload)
            else:
                _validate_payload(payload)
            guard()
            saved = ingest(unit, payload['items'])
            imported = saved if isinstance(saved, dict) else {'saved_count': saved}
            ledger.record(receipt | dict(outcome=payload.get('outcome', 'success'),
                                         reason=payload.get('reason', ''), result_count=len(payload['items']),
                                         **imported, request_ids=payload['request_ids'],
                                         truncated=payload['truncated'], ingestion_complete=True,
                                         next_cursor=payload.get('next_cursor'),
                                         **{key: payload[key] for key in ('pages_completed', 'visited_cursors') if key in payload},
                                         source_scope={key: payload[key] for key in
                                                       ('coverage_kind', 'per_request_limit', 'window_applied')
                                                       if key in payload},
                                         cache_provenance=payload.get('cache_provenance', [])))
        except HttpError as exc:
            # A pause or a lease/config boundary is not a source result. Existing
            # request journal recovery remains the only retry authority.
            unknown = exc.code == 'request_outcome_unknown' or any(
                value['unknown'] for value in execution.usage().values())
            if unknown:
                ledger.record(receipt | dict(outcome='unknown', reason='request_outcome_unknown'))
                raise
            if exc.code == 'request_budget':
                ledger.record(receipt | dict(outcome='budget_deferred', reason=exc.code))
                # One budget may be exhausted while another source is affordable;
                # each subsequent admission still uses the execution budget gate.
                continue
            if exc.code in ('project_paused', 'provider_backoff'):
                raise
            ledger.record(receipt | dict(outcome='failed', reason=exc.code))
        execution.yield_if_due()
    return ledger.coverage()


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError('invalid collection payload')
    items, ids = payload.get('items'), payload.get('request_ids')
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError('invalid collection items')
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or not key for key in ids):
        raise ValueError('collection payload lacks request provenance')
    if type(payload.get('truncated')) is not bool:
        raise ValueError('collection payload lacks truncation state')
    if payload.get('outcome', 'success') not in ('success', 'failed', 'budget_deferred'):
        raise ValueError('invalid collection payload outcome')
    if payload.get('outcome', 'success') != 'success' and (
            not payload['truncated'] or not isinstance(payload.get('reason'), str) or not payload['reason']):
        raise ValueError('partial collection payload lacks interruption reason')
