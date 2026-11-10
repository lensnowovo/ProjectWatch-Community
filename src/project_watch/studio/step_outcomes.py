"""Isolate terminal request failures at explicit, independent work boundaries.

No retry, payload rewrite or synthetic success. Transient provider/account errors,
pause, permissions and lost leases still propagate to their existing owners.
"""
from project_watch.studio.http import HttpError
from project_watch.studio.llm import ModelError

CHECKPOINT = 'independent-step-failures-v1'
ISOLATABLE = frozenset({'model_timeout', 'request_outcome_unknown', 'unknown_not_resent',
    'model_output_invalid_json', 'model_output_invalid_shape', 'model_output_incomplete',
    'model_output_truncated', 'model_content_rejected'})


def attempt(execution, name, action):
    failures = execution.read_checkpoint(CHECKPOINT) or {}
    if name in failures:
        return False, None
    try:
        return True, action()
    except (ModelError, HttpError) as exc:
        if exc.code not in ISOLATABLE:
            raise
        failures[name] = {'status': 'blocked', 'code': exc.code}
        execution.checkpoint_chunked(CHECKPOINT, failures)
        return False, None
