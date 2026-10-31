"""Frozen, opt-in research cost envelopes on the existing dispatch journal.

Amounts are integer millionths of the declared currency. A reference tariff is
an accounting limit, never a claim about a subscription invoice. Unknown or
missing usage retains the entire reservation. No network/pricing lookup here.
"""
import json
from contextlib import closing
from datetime import UTC, datetime
from urllib.parse import urlsplit

from project_watch.studio.http import HttpError

PHASES = ('collect', 'extract', 'review', 'analyze', 'report')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS studio_research_costs (
 batch_id TEXT NOT NULL REFERENCES studio_acceptance_batches(id),
 job_id TEXT NOT NULL, call_key TEXT NOT NULL, root_id TEXT NOT NULL,
 phase TEXT NOT NULL, tariff TEXT NOT NULL,
 reserved INTEGER NOT NULL, charged INTEGER NOT NULL, settled INTEGER NOT NULL,
 PRIMARY KEY(job_id,call_key),
 FOREIGN KEY(job_id,call_key) REFERENCES studio_job_calls(job_id,call_key)
);
'''


def _int(value, low=0, high=10**12):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid research budget amount')
    return value


def validate(policy):
    """Price/capability evidence is explicit and frozen, never guessed as zero."""
    policy = json.loads(json.dumps(policy))
    if type(policy.get('version')) is not int or policy['version'] != 1 or policy.get('currency') not in ('CNY', 'USD'):
        raise ValueError('invalid research budget contract')
    if policy.get('basis') not in ('published_tariff', 'reference_tariff'):
        raise ValueError('research budget price basis required')
    if 'stop_on_failure' in policy and type(policy['stop_on_failure']) is not bool:
        raise ValueError('invalid research failure boundary')
    _int(policy['ceiling'], 1)
    expiry = datetime.fromisoformat(policy['valid_until'])
    if expiry.tzinfo is None or expiry <= datetime.now(UTC):
        raise ValueError('research price expired')
    if set(policy['phase_reserves']) != set(PHASES):
        raise ValueError('research phase reserves required')
    for amount in policy['phase_reserves'].values():
        _int(amount)
    if sum(policy['phase_reserves'].values()) >= policy['ceiling']:
        raise ValueError('research reserves exceed ceiling')
    if not policy['tariffs'] or len(policy['tariffs']) > 32:
        raise ValueError('research tariffs required')
    identities = set()
    for tariff in policy['tariffs']:
        endpoint = urlsplit(tariff['endpoint'])
        if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.query or endpoint.fragment):
            raise ValueError('research tariff requires exact public endpoint')
        if tariff['kind'] not in ('model', 'search', 'page', 'open_data', 'site'):
            raise ValueError('invalid research tariff kind')
        identity = (tariff['endpoint'], tariff['kind'], tariff.get('model', ''))
        if identity in identities:
            raise ValueError('duplicate research tariff')
        identities.add(identity)
        if not str(tariff.get('source', '')).startswith('https://'):
            raise ValueError('research tariff evidence required')
        if tariff['kind'] == 'model':
            if not isinstance(tariff.get('model'), str) or not tariff['model']:
                raise ValueError('research model identity required')
            for key in ('input_per_token', 'output_per_token'):
                _int(tariff[key])
            _int(tariff['context_tokens'], 1, 2_000_000)
            _int(tariff['output_tokens'], 1, tariff['context_tokens'])
            _int(tariff['wire_bytes'], 32768, 4_000_000)
        else:
            _int(tariff['per_request'])
    return policy


def context(db, job, *, active=True):
    from project_watch.studio import acceptance_batch as batch

    row = batch._project(db, job['project_id'])
    if not row or batch._ordinary_after_release(db, row, job['id']):
        return None
    policy = json.loads(row['specification']).get('cost_policy')
    if policy is None:
        return None
    if active:
        batch._active(db, row)
        from project_watch.studio.research_window import guard_job

        guard_job(db, row, job)
    member = db.execute('SELECT * FROM studio_acceptance_jobs WHERE job_id=?', (job['id'],)).fetchone()
    if not member:
        raise HttpError('acceptance_unbound_job')
    seen = set()
    while member['parent_job_id']:
        if member['job_id'] in seen:
            raise HttpError('research_budget_lineage')
        seen.add(member['job_id'])
        member = db.execute('SELECT * FROM studio_acceptance_jobs WHERE job_id=?',
                            (member['parent_job_id'],)).fetchone()
        if not member or member['project_id'] != job['project_id']:
            raise HttpError('research_budget_lineage')
    from project_watch.studio.research_window import checked

    window = checked(db, row)
    if window:
        policy = {**policy, 'valid_until': window['deadline']}
    if active and datetime.fromisoformat(policy['valid_until']) <= datetime.now(UTC):
        raise HttpError('research_price_expired')
    return row, policy, member['job_id']


def policy(execution):
    if not hasattr(execution, 'store') or not hasattr(execution, 'job'):
        return None  # In-memory context packers have no dispatch authority.
    with closing(execution.store._connect()) as db:
        result = context(db, execution.job, active=False)
        return result[1] if result else None


def phase(execution):
    override = getattr(execution, '_research_budget_phase', None)
    if override:
        return override
    if execution.job['kind'] == 'brief':
        return 'report'
    if execution.job['kind'] in ('insight', 'scope', 'assessment'):
        return 'analyze'
    return 'extract' if getattr(execution, 'task', None) else 'collect'


def model_limits(execution):
    spec = policy(execution)
    if not spec:
        return None
    tariffs = [t for t in spec['tariffs'] if t['kind'] == 'model']
    # Multiple configured routes are checked again against their exact URL/model
    # at dispatch. This conservative minimum never widens any route's capacity.
    if not tariffs:
        raise HttpError('research_price_missing')
    return {k: min(t[k] for t in tariffs) for k in ('output_tokens', 'wire_bytes')}


def _tariff(spec, kind, request):
    if not isinstance(request, dict):
        raise HttpError('research_price_missing')
    endpoint = request.get('url')
    if isinstance(endpoint, str):
        parsed = urlsplit(endpoint)
        endpoint = parsed._replace(query='', fragment='').geturl()
    if (request.get('trusted_public_read') is True and spec.get('public_reads_included') is True
            and kind in ('open_data', 'page', 'site')):
        return dict(kind=kind, per_request=0, source='explicit_public_connector_capability'), 0
    model = ''
    if kind == 'model':
        try:
            body = json.loads(request.get('body') or b'{}')
            model = body['model']
        except (ValueError, KeyError, TypeError):
            raise HttpError('research_request_unpriced') from None
    tariff = next((t for t in spec['tariffs'] if t['kind'] == kind
                   and t['endpoint'] == endpoint and t.get('model', '') == model), None)
    if not tariff:
        raise HttpError('research_price_missing')
    if kind != 'model':
        return tariff, tariff['per_request']
    # Reserve the model's FULL verified context capacity, not a chars/token
    # heuristic. Explicit output limit is required before any network call.
    output = body.get('max_tokens', body.get('max_completion_tokens'))
    if (type(output) is not int or not 1 <= output <= tariff['output_tokens']
            or len(request['body']) > tariff['wire_bytes'] or body.get('stream') is True):
        raise HttpError('research_request_unbounded')
    return tariff, tariff['context_tokens'] * tariff['input_per_token'] + output * tariff['output_per_token']


def reserve(db, execution, key, kind, request):
    """Called inside the started receipt transaction; no independent commit."""
    ctx = context(db, execution.job)
    if not ctx:
        return
    row, spec, root = ctx
    if spec.get('stop_on_failure') and db.execute(
            "SELECT 1 FROM studio_research_costs r JOIN studio_job_calls c "
            "ON c.job_id=r.job_id AND c.call_key=r.call_key WHERE r.batch_id=? "
            "AND (c.status='unknown' "
            "OR (c.kind='model' AND c.status='completed' AND c.response_status>=400)) LIMIT 1",
            (row['batch_id'],)).fetchone():
        raise HttpError('research_window_failed')
    # A journaled read failure is local to that source. not_sent proves no
    # dispatch; neither invalidates independent work. Keep their reservations
    # and receipts, while ambiguous calls and received model failures still fence
    # the paid window. Replaying the same failed read never sends it again.
    # Imported receipts must already belong to this account; adopting historical
    # unpriced work cannot silently label its cost zero or reset the ceiling.
    if db.execute('SELECT 1 FROM studio_job_budget_links l LEFT JOIN studio_research_costs c '
                  'ON c.job_id=l.origin_job_id AND c.call_key=l.call_key '
                  'WHERE l.job_id=? AND (c.batch_id IS NULL OR c.batch_id!=?) LIMIT 1',
                  (execution.job['id'], row['batch_id'])).fetchone():
        raise HttpError('research_history_unpriced')
    if db.execute('SELECT 1 FROM studio_research_costs WHERE batch_id=? AND settled=-1 LIMIT 1',
                  (row['batch_id'],)).fetchone():
        raise HttpError('research_price_mismatch')
    tariff, amount = _tariff(spec, kind, request)
    stage = phase(execution)
    if stage not in PHASES:
        raise HttpError('research_budget_phase')
    # Keep every reservation and settlement; historical ceilings are audit data.
    from project_watch.studio import research_work_budget
    if kind == 'model':
        research_work_budget.consume(db, execution, row['batch_id'])
    db.execute('INSERT INTO studio_research_costs VALUES (?,?,?,?,?,?,?,?,0)',
               (row['batch_id'], execution.job['id'], key, root, stage,
                json.dumps(tariff, sort_keys=True), amount, amount))


def settle(db, job_id, key, response, counts):
    row = db.execute('SELECT * FROM studio_research_costs WHERE job_id=? AND call_key=?',
                     (job_id, key)).fetchone()
    if not row or response.status != 200:
        return
    tariff = json.loads(row['tariff'])
    if tariff['kind'] == 'model':
        inputs, outputs = counts['input_tokens'], counts['output_tokens']
        if inputs is None or outputs is None:
            return
        # No cache discount guessed. Cache fields can be additional on some
        # protocols, so retain the reservation until an exact tariff exists.
        if counts.get('cache_creation_tokens') or counts.get('cache_read_tokens'):
            return
        amount = inputs * tariff['input_per_token'] + outputs * tariff['output_per_token']
        mismatch = inputs > tariff['context_tokens'] or outputs > tariff['output_tokens'] or amount > row['reserved']
        if mismatch:
            # Invalid/provider-incompatible usage is not an invoice. Keep the
            # reservation and halt further dispatch; never overflow SQLite or
            # lose the already-received response while storing an absurd count.
            amount = row['reserved']
    else:
        amount, mismatch = tariff['per_request'], False
    db.execute('UPDATE studio_research_costs SET charged=?,settled=? WHERE job_id=? AND call_key=?',
               (amount, -1 if mismatch else 1, job_id, key))


def summary(store, batch_id):
    with closing(store._connect()) as db:
        row = db.execute('SELECT specification FROM studio_acceptance_batches WHERE id=?', (batch_id,)).fetchone()
        spec = json.loads(row[0]).get('cost_policy') if row else None
        if not spec:
            return None
        values = dict(db.execute('SELECT coalesce(sum(charged),0) AS committed, '
            'coalesce(sum(CASE WHEN settled=0 THEN charged ELSE 0 END),0) AS retained, '
            'sum(settled=-1) AS price_mismatches FROM studio_research_costs WHERE batch_id=?', (batch_id,)).fetchone())
        return dict(currency=spec['currency'], basis=spec['basis'], ceiling=None,
                    historical_ceiling=spec['ceiling'], remaining=None, **values)


def prepare_request(execution, kind, body):
    """New opted-in jobs only; freeze a finite output before hashing the wire."""
    if kind != 'model':
        return body
    bounds = model_limits(execution)
    if bounds is None:
        return body
    try:
        value = json.loads(body)
        key = 'max_completion_tokens' if 'max_completion_tokens' in value else 'max_tokens'
        old = value.get(key)
        if old is not None and (type(old) is not int or old < 1):
            raise ValueError()
        value[key] = min(old, bounds['output_tokens']) if old else bounds['output_tokens']
        return json.dumps(value, ensure_ascii=False).encode()
    except (TypeError, ValueError):
        raise HttpError('research_request_unbounded') from None


def model_allowance(execution):
    """Request counts and historical financial ceilings are not dispatch quotas."""
    return None
