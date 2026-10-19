"""Storage-only external scope schema; no legacy monitoring or analysis imports.

Keep historical scopes and consent immutable. This is the original migration,
shared by the repository and the compatibility entry point in external_focus.
"""


def ensure_external_focus_schema(connection):
    previous = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='external_focus'").fetchone()
    if previous and "'bing'" not in previous[0]:
        # SQLite cannot widen a CHECK in place. Preserve rows and immutable
        # triggers atomically. No other tables reference external_focus.
        connection.execute('SAVEPOINT external_focus_bing')
        try:
            connection.execute('''CREATE TABLE external_focus_bing (
                plan_id TEXT PRIMARY KEY REFERENCES monitoring_plans(id),
                category TEXT NOT NULL CHECK(category IN ('company','sector','competitors','policy','industry')),
                term TEXT NOT NULL, provider TEXT NOT NULL CHECK(provider IN ('gdelt','brave','bing')),
                consent_version TEXT NOT NULL, created_at TEXT NOT NULL, actor_ref TEXT NOT NULL)''')
            connection.execute('INSERT INTO external_focus_bing SELECT * FROM external_focus')
            connection.execute('DROP TABLE external_focus')
            connection.execute('ALTER TABLE external_focus_bing RENAME TO external_focus')
            for operation in ('UPDATE', 'DELETE'):
                connection.execute(f'''CREATE TRIGGER external_focus_no_{operation.lower()}
                    BEFORE {operation} ON external_focus
                    BEGIN SELECT RAISE(ABORT, 'external focus is immutable'); END''')
            connection.execute('RELEASE external_focus_bing')
        except Exception:
            connection.execute('ROLLBACK TO external_focus_bing')
            connection.execute('RELEASE external_focus_bing')
            raise
    connection.execute('''CREATE TABLE IF NOT EXISTS external_focus (
        plan_id TEXT PRIMARY KEY REFERENCES monitoring_plans(id),
        category TEXT NOT NULL CHECK(category IN ('company','sector','competitors','policy','industry')),
        term TEXT NOT NULL, provider TEXT NOT NULL CHECK(provider IN ('gdelt','brave','bing')),
        consent_version TEXT NOT NULL, created_at TEXT NOT NULL,
        actor_ref TEXT NOT NULL
    )''')
    for operation in ('UPDATE', 'DELETE'):
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS external_focus_no_{operation.lower()}
            BEFORE {operation} ON external_focus
            BEGIN SELECT RAISE(ABORT, 'external focus is immutable'); END''')
