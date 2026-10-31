"""Explicit shared-count policy and fixed-work forecasts over the cost journal.

Historical count partitions and ceilings are audit metadata, not dispatch gates.
The price contract, deadline, charges and ambiguous reservations remain intact.
"""
import json
from contextlib import closing

from project_watch.studio.http import HttpError

PREFIX = 'research-count-policy:'
PLAN = 'research_model_work_v1'
HOLD = 'research-work-hold:'
STAGE_PLAN = 'research_stage_work_v1'
ACTIVE_STAGE = 'research_stage_work_active_v1'


def enabled(execution):
    from project_watch.studio.acceptance_batch import _project

    with closing(execution.store._connect()) as db:
        row = _project(db, execution.job['project_id'])
        return bool(row and current(db, row['batch_id']))


def current(db, batch_id):
    prefix = PREFIX + batch_id + ':'
    row = db.execute('SELECT value FROM studio_state WHERE substr(key,1,?)=? ORDER BY key DESC LIMIT 1',
                     (len(prefix), prefix)).fetchone()
    return json.loads(row[0]) if row else None


def enable(store, batch_id, *, authorization):
    """Local explicit policy admission; no runner, account reopen or calls."""
    from project_watch.studio import acceptance_batch as batch
    from project_watch.studio import research_budget

    if not isinstance(authorization, str) or not 1 <= len(authorization) <= 500:
        raise ValueError('shared count policy requires owner authorization')
    with closing(store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM studio_acceptance_batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise ValueError('cost account missing')
        spec = json.loads(row['specification'])
        research_budget.validate(spec.get('cost_policy') or {})
        if current(db, batch_id):
            return current(db, batch_id)
        if db.execute("SELECT 1 FROM studio_acceptance_jobs a JOIN studio_job_calls c ON c.job_id=a.job_id "
                      "JOIN studio_acceptance_projects p ON p.project_id=a.project_id WHERE p.batch_id=? "
                      "AND c.status IN ('started','unknown')", (batch_id,)).fetchone():
            raise HttpError('request_outcome_unknown')
        value = dict(version=1, authorization=authorization, original_specification=batch.digest(spec),
                     model_cap=spec['caps']['model'], deadline=row['deadline'],
                     ceiling=spec['cost_policy']['ceiling'], basis='shared_cost_bounded_work')
        db.execute('INSERT INTO studio_state VALUES (?,?)',
                   (PREFIX + batch_id + ':00000001', batch.encode(value)))
        return value


def limits(db, row, spec, allocation):
    policy = current(db, row['batch_id'])
    if not policy:
        return spec['caps'], allocation['caps']
    from project_watch.studio.acceptance_batch import digest

    if (policy['original_specification'] != digest(spec) or policy['deadline'] != row['deadline']
            or policy['ceiling'] != spec['cost_policy']['ceiling']):
        raise HttpError('research_count_policy_changed')
    # Other explicit source limits are untouched. Model consumption is charged
    # once to the common pool, instead of stranded in a project's partition.
    shared = {**spec['caps'], **policy.get('caps', {}), 'model': policy['model_cap']}
    return shared, {**allocation['caps'], **policy.get('caps', {}), 'model': policy['model_cap']}


def plan_work(execution, *, phase, counts, basis):
    """Freeze the next actual work unit, sharing counts without resetting money.

    This is a soft work forecast, not a monetary reservation for every possible
    future model turn. Dispatch still atomically reserves each request's full
    worst case and protects later phases and fixed-review work holds.
    """
    from project_watch.studio import acceptance_batch as batch
    from project_watch.studio import research_budget
    from project_watch.studio.execution import JobExecution

    if (phase not in research_budget.PHASES or not isinstance(basis, dict) or not basis
            or set(counts) - set(batch.KINDS)
            or any(type(n) is not int or n < 0 for n in counts.values())):
        raise ValueError('bounded work forecast required')
    binding = batch.digest([phase, counts, basis])
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        account = batch._project(db, execution.job['project_id'])
        if not account or not current(db, account['batch_id']):
            return None
        ctx = research_budget.context(db, execution.job)
        if not ctx:
            return None
        row, _cost, _ = ctx
        reader = JobExecution(execution.store, execution.job)
        plans = reader.read_checkpoint(STAGE_PLAN, db=db) or {}
        if binding not in plans:
            policy = current(db, row['batch_id'])
            total_caps, _ = limits(db, row, json.loads(row['specification']), json.loads(row['allocation']))
            spent = db.execute('SELECT coalesce(sum(charged),0) FROM studio_research_costs WHERE batch_id=?',
                               (row['batch_id'],)).fetchone()[0]
            used = {k: execution.budget_used(k, db=db) for k in batch.KINDS}
            total = {k: batch.usage(db, row['batch_id'], k) for k in batch.KINDS}
            caps = {k: max(total_caps[k], total[k] + counts.get(k, 0)) for k in batch.KINDS}
            policy = {**policy, 'version': policy['version'] + 1, 'caps': caps,
                      'model_cap': caps['model'], 'work_binding': binding,
                      'phase': phase, 'spent': spent, 'counts_before': total}
            db.execute('INSERT INTO studio_state VALUES (?,?)',
                       (PREFIX + row['batch_id'] + ':' + f"{policy['version']:08d}", batch.encode(policy)))
            plans[binding] = dict(phase=phase, counts=counts, basis=basis,
                limits={k: used[k] + counts.get(k, 0) for k in batch.KINDS},
                used_before=used, policy_version=policy['version'], forecast_only=True)
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) '
                       'ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
                       (execution.job['id'], STAGE_PLAN, batch.encode(plans)))
        db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) '
                   'ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
                   (execution.job['id'], ACTIVE_STAGE, batch.encode({'binding': binding})))
        execution.budgets.update(plans[binding]['limits'])
        return plans[binding]


def reserve_review(execution, *, calls, binding, output_tokens):
    """Admit exactly the remaining review batches; no retry/general quota grant."""
    from project_watch.studio import acceptance_batch as batch
    from project_watch.studio import research_budget
    from project_watch.studio.execution import JobExecution

    if type(calls) is not int or calls < 0:
        raise ValueError('invalid review work count')
    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        execution._guard(db)
        ctx = research_budget.context(db, execution.job)
        if not ctx or not current(db, ctx[0]['batch_id']):
            return None
        row, cost, _ = ctx
        policy = current(db, row['batch_id'])
        limits(db, row, json.loads(row['specification']), json.loads(row['allocation']))
        if db.execute("SELECT 1 FROM studio_job_calls c JOIN studio_acceptance_jobs a ON a.job_id=c.job_id "
                      "JOIN studio_acceptance_projects p ON p.project_id=a.project_id WHERE p.batch_id=? "
                      "AND c.status IN ('started','unknown')", (row['batch_id'],)).fetchone():
            raise HttpError('request_outcome_unknown')
        reader = JobExecution(execution.store, execution.job)
        plans = reader.read_checkpoint(PLAN, db=db) or {}
        if binding in plans:
            limit = plans[binding]['job_limit']
        else:
            tariffs = [t for t in cost['tariffs'] if t['kind'] == 'model']
            if not tariffs or any(output_tokens > t['output_tokens'] for t in tariffs):
                raise HttpError('research_price_missing')
            worst = max(t['context_tokens'] * t['input_per_token'] + output_tokens * t['output_per_token'] for t in tariffs)
            spent = db.execute('SELECT coalesce(sum(charged),0) FROM studio_research_costs WHERE batch_id=?',
                               (row['batch_id'],)).fetchone()[0]
            if db.execute('SELECT 1 FROM studio_research_costs WHERE batch_id=? AND settled=-1', (row['batch_id'],)).fetchone():
                raise HttpError('research_price_mismatch')
            used = execution.budget_used('model', db=db)
            total = batch.usage(db, row['batch_id'], 'model')
            limit = used + calls
            needed = total + calls + pending_calls(db, row['batch_id'])
            if needed > policy['model_cap']:
                # Append the calculated finite work allowance. The original
                # specification, lower financial ceiling and old policy remain.
                policy = {**policy, 'version': policy['version'] + 1, 'model_cap': needed,
                          'work_binding': binding, 'prior_model_cap': policy['model_cap'],
                          'spent': spent, 'forecast': calls * worst, 'total_calls_before': total}
                db.execute('INSERT INTO studio_state VALUES (?,?)',
                    (PREFIX + row['batch_id'] + ':' + f"{policy['version']:08d}", batch.encode(policy)))
            plans[binding] = dict(calls=calls, job_limit=limit, batch_calls_before=total,
                job_calls_before=used, forecast=calls * worst, ceiling=cost['ceiling'],
                policy_version=policy['version'], output_tokens=output_tokens)
            db.execute('INSERT INTO studio_job_checkpoints VALUES (?,?,?) '
                       'ON CONFLICT(job_id,stage) DO UPDATE SET data=excluded.data',
                       (execution.job['id'], PLAN, batch.encode(plans)))
            db.execute('INSERT INTO studio_state VALUES (?,?)',
                       (_hold_key(row['batch_id'], execution.job['id'], binding),
                        batch.encode(dict(remaining=calls, unit=worst))))
        execution.budgets['model'] = max(execution.budgets.get('model') or 0, limit)
        return limit


def _hold_key(batch_id, job_id, binding):
    from project_watch.studio.acceptance_batch import digest

    return HOLD + batch_id + ':' + digest([job_id, binding])


def held(db, batch_id):
    prefix = HOLD + batch_id + ':'
    return sum(v['remaining'] * v['unit'] for (raw,) in db.execute(
        'SELECT value FROM studio_state WHERE substr(key,1,?)=?', (len(prefix), prefix))
        for v in [json.loads(raw)])


def pending_calls(db, batch_id):
    prefix = HOLD + batch_id + ':'
    return sum(json.loads(raw)['remaining'] for (raw,) in db.execute(
        'SELECT value FROM studio_state WHERE substr(key,1,?)=?', (len(prefix), prefix)))


def consume(db, execution, batch_id):
    """Convert one owned work reservation into a dispatch reservation atomically."""
    binding = getattr(execution, '_review_work_binding', None)
    if binding is None:
        return
    key = _hold_key(batch_id, execution.job['id'], binding)
    row = db.execute('SELECT value FROM studio_state WHERE key=?', (key,)).fetchone()
    if not row:
        return
    value = json.loads(row[0])
    if value['remaining'] <= 0:
        return  # Exhausted forecasts never prevent actual journaled work.
    value['remaining'] -= 1
    db.execute('UPDATE studio_state SET value=? WHERE key=?', (json.dumps(value), key))


def release(execution, binding):
    """Keep the work audit, release only unused future reservations on exit."""
    from project_watch.studio import research_budget

    with closing(execution.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        ctx = research_budget.context(db, execution.job, active=False)
        if not ctx:
            return
        key = _hold_key(ctx[0]['batch_id'], execution.job['id'], binding)
        row = db.execute('SELECT value FROM studio_state WHERE key=?', (key,)).fetchone()
        if row:
            value = json.loads(row[0])
            value.update(released=value['remaining'], remaining=0)
            db.execute('UPDATE studio_state SET value=? WHERE key=?', (json.dumps(value), key))


def restore(execution, db):
    """Final journal guard: only the exact admitted work may cross an old cap."""
    from project_watch.studio import acceptance_batch as batch
    from project_watch.studio.execution import JobExecution

    row = batch._project(db, execution.job['project_id'])
    if not row or not current(db, row['batch_id']):
        return
    reader = JobExecution(execution.store, execution.job)
    active = reader.read_checkpoint(ACTIVE_STAGE, db=db)
    stage = (reader.read_checkpoint(STAGE_PLAN, db=db) or {}).get((active or {}).get('binding'))
    if stage:
        execution.budgets.update(stage['limits'])
    binding = getattr(execution, '_review_work_binding', None)
    plan = (reader.read_checkpoint(PLAN, db=db) or {}).get(binding)
    if plan:
        execution.budgets['model'] = plan['job_limit']
