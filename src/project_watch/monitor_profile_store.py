"""Persistent human profile fields, schema and edit validation; no reading methods."""

PROFILE_FIELDS = ("business", "stage", "products", "customers", "thesis")


def ensure_profile_schema(connection):
    """Additive SQLite-only upgrade. Team/Postgres storage is deliberately unchanged."""
    connection.execute("""CREATE TABLE IF NOT EXISTS monitor_profile_revisions (
        project_id TEXT NOT NULL REFERENCES projects(id),
        version INTEGER NOT NULL CHECK (version > 0),
        fields_json TEXT NOT NULL,
        focus_json TEXT,
        actor_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (project_id, version)
    )""")
    for operation in ("UPDATE", "DELETE"):
        connection.execute(f"""CREATE TRIGGER IF NOT EXISTS monitor_profile_no_{operation.lower()}
            BEFORE {operation} ON monitor_profile_revisions
            BEGIN SELECT RAISE(ABORT, 'monitor profile history is append only'); END""")


def validate_edit(fields, focus, restore_fields, restore_focus):
    if not isinstance(fields, dict) or set(fields) - set(PROFILE_FIELDS):
        raise ValueError("不支持的项目档案字段。")
    if any(not isinstance(v, str) or len(v) > 2000 for v in fields.values()):
        raise ValueError("每项内容最多 2000 字。")
    if (
        not isinstance(restore_fields, (list, tuple))
        or any(f not in PROFILE_FIELDS for f in restore_fields)
        or set(restore_fields) & set(fields)
    ):
        raise ValueError("恢复材料建议的字段不能同时修改。")
    if focus is not None and (
        not isinstance(focus, list)
        or len(focus) > 20
        or any(not isinstance(t, str) or not t.strip() or len(t) > 300 for t in focus)
    ):
        raise ValueError("关注事项最多 20 项，每项为 1–300 字。")
    if type(restore_focus) is not bool or (restore_focus and focus is not None):
        raise ValueError("恢复关注建议时不能同时填写关注事项。")
    return {k: v.strip() for k, v in fields.items()}, (
        list(dict.fromkeys(t.strip() for t in focus)) if focus is not None else None
    )
