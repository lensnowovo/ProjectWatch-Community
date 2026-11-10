"""Read owner-reviewed page copy from a local sidecar; never generate or schedule work.

The sidecar belongs to the data home, not to a release or a particular company.
Imported prose is dated and invalidated by its saved source/analysis fingerprint.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing

SUBJECTS = {'brief', 'operations', 'overview', 'external', 'trials', 'papers', 'patents',
            'competition', 'chain', 'industry', 'policy', 'technology', 'ops', 'business', 'numbers', 'fin', 'risks'}


def _refs(store, pid, values):
    result = []
    for ref in values if isinstance(values, list) else []:
        if not isinstance(ref, dict):
            continue
        url = ref.get('url', '')
        value = {'code': f'S{len(result)+1}', 'kind': 'public',
                 'label': str(ref.get('label', ''))[:1000], 'text': '',
                 'url': url if isinstance(url, str) and url.startswith(('https://', 'http://')) else '',
                 'date': str(ref.get('date', ''))[:40]}
        if ref.get('kind') == 'internal':
            with closing(store._connect()) as db:
                exists = db.execute('SELECT 1 FROM sources WHERE id=? AND project_id=?',
                                     (ref.get('source_id'), pid)).fetchone()
            if not exists:
                continue
            value.update(kind='internal', source_id=ref['source_id'], url='',
                         source_name=str(ref.get('source_name', '内部资料'))[:1000],
                         locator=str(ref.get('locator', ''))[:500])
        result.append(value)
    return result

def fingerprint(store, project_id):
    with closing(store._connect()) as db:
        rows = db.execute('SELECT id,title,url,snippet,summary,published_at FROM studio_items '
                          'WHERE project_id=? ORDER BY id', (project_id,)).fetchall()
    value = [list(r) for r in rows]
    value.append(store.get_brief(project_id))
    value.append(store.get_profile(project_id))
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def read(store, project_id):
    path = store.db_path.parent / 'editorial-summaries.json'
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 2_000_000:
        return {}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        saved = payload.get('projects', {}).get(project_id, {})
        if payload.get('version') != 1 or not isinstance(saved, dict):
            return {}
        pages = saved.get('pages', {})
        if not isinstance(pages, dict) or not isinstance(saved.get('generated_at'), str):
            return {}
        stale = saved.get('basis') != fingerprint(store, project_id)
        result = {}
        for key, entry in pages.items():
            if key not in SUBJECTS or not isinstance(entry, dict):
                continue
            text = entry.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 3000:
                continue
            refs = _refs(store, project_id, entry.get('refs', []))
            details = [dict(text=d['text'], refs=_refs(store, project_id, d.get('refs', [])))
                       for d in entry.get('details', []) if isinstance(d, dict)
                       and isinstance(d.get('text'), str) and len(d['text']) <= 3000] if isinstance(entry.get('details', []), list) else []
            result[key] = {'text': text, 'refs': refs, 'details': details, 'date': saved['generated_at'],
                           'historical': stale, 'editorial': True}
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
