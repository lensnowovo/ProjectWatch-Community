"""Serial host dispatch and ingestion under an existing job execution guard."""
from project_watch.feeds import FeedError
from project_watch.studio.http import HttpError

STOP = frozenset(("project_paused", "project_not_consented"))
# Definitely never transmitted: no network, or the provider is backing off after that.
# Such a unit stays open so the same job can retry it; the caller decides (see collect_job).
NOT_SENT = frozenset(("connection_not_sent", "provider_backoff"))
OFFLINE_WAIT_HOURS = 24  # then the round settles as failed and the normal cadence resumes


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
                                                       ('coverage_kind', 'per_request_limit', 'window_applied', 'dropped_records')
                                                       if key in payload},
                                         cache_provenance=payload.get('cache_provenance', [])))
        except HttpError as exc:
            # A pause or missing consent stops the round. Everything else is this
            # source's own outcome: an unclear request is recorded (never resent),
            # a definite "not sent" (backoff, monthly budget, in flight) or a
            # failed read leaves the other sources to run (dispatch.py).
            if exc.code in STOP:
                raise
            if exc.code in NOT_SENT:
                ledger.record(receipt | dict(reason=exc.code))
            elif exc.code == 'request_outcome_unknown':
                ledger.record(receipt | dict(outcome='unknown', reason=exc.code))
            elif exc.code == 'request_budget':
                ledger.record(receipt | dict(outcome='budget_deferred', reason=exc.code))
            else:
                ledger.record(receipt | dict(outcome='failed', reason=exc.code))
        except FeedError as exc:
            # A completed read whose body is not a usable feed (blocked page, no items).
            # The response is journalled; this source fails, the round goes on.
            ledger.record(receipt | dict(outcome='failed', reason=str(exc)[:80]))
        except (ConnectionError, TimeoutError) as exc:
            # The journal already marked the request unknown (or a failed read).
            ledger.record(receipt | dict(outcome='unknown', reason=type(exc).__name__))
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


def settle_not_sent(ledger, coverage, *, offline_retry=True):
    """Units whose request never left the machine: retry the round later, or close them.

    When every scheduled source failed that way and at least one could not connect, the
    computer is offline: the job is deferred with the worker's provider backoff and its
    open units are retried by the same job, instead of the round failing and waiting a
    full monitoring cycle. A partial outage (one provider backing off, a bad key) closes
    the unsent units as failed so the material that did arrive is analysed now.
    """
    available = [r for r in coverage['rows'] if r['capability'] == 'available' and r['outcome'] != 'not_due']
    waiting = [r for r in available if r['outcome'] == 'running']
    if not waiting:
        return coverage
    if offline_retry and len(waiting) == len(available) and _offline(ledger, waiting):
        raise HttpError('connection_not_sent')
    plan = ledger.plan
    for row in waiting:
        ledger.record(dict(round_id=plan['round_id'], unit_id=row['unit_id'], outcome='failed',
                           reason=row['reason'] or 'not_sent'))
    return ledger.coverage()


def _offline(ledger, waiting):
    """No connection could be made, now or by another job sharing these providers.

    A backoff with status 0 was written by a request that never connected, so a pass
    that only met backoffs (another project's round, or this job after a yield) still
    counts as offline. A 401/403/429 backoff does not. The wait is bounded.
    """
    from contextlib import closing
    from datetime import UTC, datetime, timedelta

    from project_watch.studio.store import now

    started = datetime.fromisoformat(ledger.plan['created_at'])
    started = started if started.tzinfo else started.replace(tzinfo=UTC)
    if datetime.now(UTC) - started > timedelta(hours=OFFLINE_WAIT_HOURS):
        return False
    if any(r['reason'] == 'connection_not_sent' for r in waiting):
        return True
    with closing(ledger.execution.store._connect()) as db:
        return db.execute('SELECT 1 FROM studio_provider_backoff WHERE status=0 AND retry_after>? LIMIT 1',
                          (now(),)).fetchone() is not None
