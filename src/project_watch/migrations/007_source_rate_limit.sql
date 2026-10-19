ALTER TABLE source_runs DROP CONSTRAINT source_runs_status_check;

ALTER TABLE source_runs ADD CONSTRAINT source_runs_status_check
    CHECK (status IN ('completed', 'no_results', 'unreachable', 'rate_limited', 'not_run'));
