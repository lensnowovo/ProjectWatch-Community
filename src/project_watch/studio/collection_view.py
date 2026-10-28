"""Read-only public source projection, independent of research completion."""
from contextlib import closing


def discovered_sources(store, project_id, *, limit=20, offset=0):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ValueError('invalid source page')
    empty = dict(items=[], total=0, pending=0, offset=offset, next_offset=None)
    with closing(store._connect()) as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_collection_sources'").fetchone():
            return empty
        # Rank before checking visibility/lifecycle: never resurrect an older
        # snapshot when the current one has been withdrawn or made unavailable.
        candidates = """WITH snapshots AS (
            SELECT s.*,ROW_NUMBER() OVER (PARTITION BY s.item_id
                ORDER BY s.observed_at DESC,s.identity) AS rank
            FROM studio_collection_sources s WHERE s.project_id=?
        ), candidates AS (
            SELECT i.id,i.title,i.url,i.source,i.published_at,i.collected_at,
                   i.category,i.triaged,s.observed_at,s.evidence_version_id,
                   p.read_status,p.basis AS reading_basis,
                   b.id AS body_evidence_version_id,
                   json_extract(CASE WHEN p.basis='own_text' AND p.read_status IN
                     ('article_body','partial_body','filing_text','search_summary','paper_abstract')
                     AND b.id IS NOT NULL THEN b.record ELSE e.record END,'$.source_type') AS source_type,
                   json_extract(CASE WHEN p.basis='own_text' AND p.read_status IN
                     ('article_body','partial_body','filing_text','search_summary','paper_abstract')
                     AND b.id IS NOT NULL THEN b.record ELSE e.record END,'$.read_scope') AS read_scope
            FROM snapshots s
            JOIN studio_items i ON i.id=s.item_id AND i.project_id=s.project_id
            JOIN studio_evidence_versions e ON e.id=s.evidence_version_id AND e.project_id=s.project_id
            LEFT JOIN studio_item_evidence p ON p.item_id=i.id
            LEFT JOIN studio_evidence_versions b ON b.id=p.evidence_version_id AND b.project_id=s.project_id
            WHERE s.rank=1 AND e.visibility='public' AND i.status!='dismissed'
              AND (i.triaged=0 OR i.relevant=1)
              AND (p.item_id IS NULL OR (p.project_id=s.project_id AND
                   (p.evidence_version_id IS NULL OR b.visibility='public')))
              AND NOT EXISTS (SELECT 1 FROM studio_evidence_lifecycle l
                  WHERE l.project_id=s.project_id AND l.status='withdrawn'
                    AND l.evidence_version_id IN (e.id,b.id))
        ) """
        counts = db.execute(candidates + 'SELECT count(*),COALESCE(sum(triaged=0),0) FROM candidates',
                            (project_id,)).fetchone()
        rows = db.execute(candidates + 'SELECT * FROM candidates '
                          'ORDER BY observed_at DESC,id LIMIT ? OFFSET ?', (project_id, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['processing_status'] = 'organized' if item.pop('triaged') else 'pending'
            items.append(item)
        return dict(items=items, total=counts[0], pending=counts[1], offset=offset,
                    next_offset=offset + limit if offset + limit < counts[0] else None)
