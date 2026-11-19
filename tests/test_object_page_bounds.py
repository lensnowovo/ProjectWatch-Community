"""Internal readers see every object; only the external API pages them."""
import json
from contextlib import closing

import pytest

from project_watch.studio.app import create_studio_app


def test_internal_object_reads_are_not_cut_at_500(tmp_path):
    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Many Objects').id
    pipeline.store.upsert_project(pid, {'legal_name': 'Synthetic Many Objects'}, consent=True)
    subject = pipeline.objects.ensure_subject(pid)
    with closing(pipeline.store._connect()) as db, db:
        record = json.loads(db.execute('SELECT record FROM studio_monitor_objects WHERE id=?', (subject['id'],)).fetchone()[0])
        # Live: 703 objects; the project's own product sorted after the 500th and left competition inputs.
        for i in range(600):
            object_id = f'0{i:05d}-synthetic'
            db.execute('INSERT INTO studio_monitor_objects(id,project_id,version,visibility,record,user_override) '
                       "VALUES (?,?,1,'public',?,0)",
                       (object_id, pid, json.dumps({**record, 'id': object_id, 'kind': 'product', 'purpose': 'unknown'})))
    everything = pipeline.objects.page(pid, include_internal=False)
    assert len(everything) == 601 and subject['id'] in {o['id'] for o in everything}
    first = pipeline.objects.page(pid, limit=500)
    assert len(first) == 500 and len(pipeline.objects.page(pid, limit=500, after=first[-1]['id'])) == 101
    with pytest.raises(ValueError):
        pipeline.objects.page(pid, limit=501)
