"""Lease-protected collection checkpoints; HTTP recovery stays in JobExecution."""
from copy import deepcopy

from project_watch.studio import collection_continuation, collection_history
from project_watch.studio.collection_plan import reconcile

STAGE = 'collection_round_v1'
SUPPLEMENTS = 'collection_supplements_v1'
SUPPLEMENT_PREFIX = 'collection_extra:'


class CollectionLedger:
    """One frozen manifest and one current receipt per logical query.

    Store through JobExecution so expired workers cannot write. This ledger never
    dispatches or recovers HTTP requests, and cannot clear an unknown outcome.
    Reopening must use the original manifest, including its frozen time window.
    """

    def __init__(self, execution, plan, *, stage=STAGE):
        if stage != STAGE and not stage.startswith(SUPPLEMENT_PREFIX):
            raise ValueError('invalid collection ledger stage')
        self.stage = stage
        if plan['round_id'] != execution.job['id']:
            raise ValueError('collection plan belongs to another job')
        if plan['project_id'] != execution.job['project_id']:
            raise ValueError('collection plan belongs to another project')
        self.execution = execution
        saved = execution.read_checkpoint(self.stage)
        if saved is not None:
            if saved['plan'] != plan:
                raise ValueError('collection plan changed during round')
            reconcile(saved['plan'], saved['receipts'])
            self._state = deepcopy(saved)
            collection_continuation.record(execution, plan, self._state['receipts'])
            collection_history.record(execution, plan, self._state['receipts'])
        else:
            reconcile(plan, [])
            self._state = {'plan': deepcopy(plan), 'receipts': []}
            execution.checkpoint_chunked(self.stage, self._state)

    @property
    def plan(self):
        return deepcopy(self._state['plan'])

    @property
    def receipts(self):
        return deepcopy(self._state['receipts'])

    def coverage(self):
        return reconcile(self._state['plan'], self._state['receipts'])

    def record(self, receipt):
        """Persist host evidence before exposing it; terminal results are immutable.

        An interrupted running unit may finish using the existing request journal.
        Failed, deferred and unknown units need a separate authorized recovery
        path, not an overwrite disguised as a fresh pending unit.
        """
        next_state = deepcopy(self._state)
        receipts = next_state['receipts']
        existing = next((r for r in receipts if r['unit_id'] == receipt['unit_id']), None)
        if existing == receipt:
            return self.coverage()
        if existing:
            if existing['outcome'] != 'running':
                raise ValueError('terminal collection receipt cannot be overwritten')
            receipts.remove(existing)
        receipts.append(deepcopy(receipt))
        coverage = reconcile(next_state['plan'], receipts)
        self.execution.checkpoint_chunked(self.stage, next_state)
        self._state = next_state
        collection_continuation.record(self.execution, next_state['plan'], receipts)
        collection_history.record(self.execution, next_state['plan'], receipts)
        return coverage
