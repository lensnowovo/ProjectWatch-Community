-- Existing numeric observations have no provable actual/forecast classification.
-- Never infer one from period or source name during upgrade.
ALTER TABLE observations
    ADD COLUMN basis text NOT NULL DEFAULT 'unknown'
    CHECK (basis IN ('actual', 'forecast', 'budget', 'unknown'));
