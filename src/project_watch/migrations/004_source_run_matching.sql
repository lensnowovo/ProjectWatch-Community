ALTER TABLE source_runs
ADD COLUMN matched_count integer CHECK (matched_count >= 0);

-- NULL means this historical run predated project-name filtering. Never infer
-- a match count from the old fetched-item count.
