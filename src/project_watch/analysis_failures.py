"""Allowlisted diagnostics only; never persist provider bodies or exception text."""
from pydantic import ValidationError

from project_watch.executors import ExecutorUnavailable
from project_watch.model_gateway import GateRejected
from project_watch.reading_jobs import ReadingDenied

CODES = frozenset({'unknown', 'legacy_unknown', 'incomplete_response', 'invalid_json',
                   'output_schema', 'evidence_validation', 'transport', 'scope_changed',
                   'request_blocked', 'executor_unavailable', 'worker_interrupted',
                   'citation_mismatch', 'unsupported_number', 'evidence_scope', 'unsupported_impact'})


class AnalysisFailure(ExecutorUnavailable):
    def __init__(self, code):
        self.code = code if code in CODES else 'unknown'
        super().__init__(self.code)


def failure_code(error):
    if isinstance(error, AnalysisFailure):
        return error.code
    if isinstance(error, ReadingDenied):
        return 'scope_changed'
    if isinstance(error, GateRejected):
        return 'request_blocked'
    if isinstance(error, ValidationError):
        return 'output_schema'
    if isinstance(error, (TimeoutError, ConnectionError)):
        return 'transport'
    if isinstance(error, ExecutorUnavailable):
        return {
            'model output incomplete': 'incomplete_response',
            'model output was not a JSON proposal': 'invalid_json',
            'model did not complete a bounded response': 'transport',
            'DSH turn timed out or failed': 'executor_unavailable',
            'DSH requires Node.js and npx': 'executor_unavailable',
        }.get(str(error), 'unknown')
    return 'unknown'
