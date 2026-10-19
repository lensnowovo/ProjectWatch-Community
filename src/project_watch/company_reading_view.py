"""Read existing company summaries without generating, enqueueing or creating schema."""

import json
from contextlib import closing


def _current_packet(db, packet):
    source = db.execute("SELECT * FROM sources WHERE id=?", (packet["source_id"],)).fetchone()
    if (not source or source["project_id"] != packet["project_id"]
            or source["content_sha256"] != packet["source_sha256"]):
        return False
    for f in packet["fragments"]:
        original = db.execute("SELECT * FROM document_fragments WHERE source_id=? AND ordinal=?",
                              (packet["source_id"], f["ordinal"])).fetchone()
        if (not original or original["locator"] != f["locator"]
                or original["project_id"] != packet["project_id"]
                or original["text"][f["offset"]:f["offset"] + len(f["text"])] != f["text"]):
            return False
    return True


class CompanyStateView:
    def __init__(self, repository):
        self.repo = repository

    def read(self, project_id):
        batches, stale, seen = [], 0, set()
        with closing(self.repo._connect()) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_reading_batches'").fetchone():
                return {"project_id": project_id, "batches": [], "stale_batches": 0}
            rows = db.execute("""SELECT b.* FROM company_reading_batches b
                JOIN reading_jobs j ON j.id=b.job_id AND j.state='succeeded'
                WHERE b.project_id=? ORDER BY b.created_at DESC,b.job_id DESC""", (project_id,)).fetchall()
            for row in rows:
                packet = json.loads(row["packet_json"])
                if not _current_packet(db, packet):
                    stale += 1
                    continue
                if row["input_digest"] in seen:
                    continue
                seen.add(row["input_digest"])
                batches.append({"id": row["job_id"], "source_id": row["source_id"],
                                "source_reference": packet["reference"], "source_version": packet["source_sha256"],
                                "recorded_at": row["created_at"], "rule_version": row["rule_version"],
                                "items": json.loads(row["output_json"])["items"]})
        return {"project_id": project_id, "batches": batches, "stale_batches": stale}
