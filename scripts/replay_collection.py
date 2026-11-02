"""Replay one historical collection round offline, into a fresh isolated data directory.

Usage:
  python scripts/replay_collection.py <corpus.sqlite3> <copied-db.sqlite3> <job_id> <empty-workdir>

The source database must be a COPY (opened read-only). The work directory must
be new and empty; nothing outside it is written. The network is never reached:
every provider request is answered from the corpus or reported as a miss.
"""
import json
import socket
import sqlite3
import sys
from pathlib import Path

CANDIDATE_KEYS = ('source', 'category', 'target_id', 'query_version', 'query', 'capability', 'reason',
                  'priority', 'schedule_due', 'window_start', 'window_end', 'applicability_evidence_ids', 'query_rank')


def _read(db, job_id, stage):
    row = db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?', (job_id, stage)).fetchone()
    if row is None:
        raise SystemExit(f'no {stage} checkpoint for job {job_id}')
    value = json.loads(row[0])
    if value.get('_chunked_checkpoint'):
        parts = [json.loads(db.execute('SELECT data FROM studio_job_checkpoints WHERE job_id=? AND stage=?',
                                       (job_id, value['prefix'] + str(i))).fetchone()[0]) for i in range(value['count'])]
        return json.loads(''.join(parts))
    return value


def _deny_network(*_args, **_kwargs):
    raise RuntimeError('network disabled during replay')


def main(corpus_path, source_path, job_id, workdir):
    workdir = Path(workdir)
    if workdir.exists() and any(workdir.iterdir()):
        raise SystemExit('work directory must be new and empty')
    socket.create_connection = _deny_network  # belt and braces: replay must not reach the network

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.collection_ingest import CollectionIngest
    from project_watch.studio.collection_ledger import CollectionLedger
    from project_watch.studio.collection_plan import make_plan
    from project_watch.studio.collection_runner import run_collection
    from project_watch.studio.collection_sources import CollectionSources
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.replay import Corpus, ReplayTransport, replay_factory
    from project_watch.studio.research_sources import research_sources

    source = sqlite3.connect(f'file:{Path(source_path).resolve().as_posix()}?mode=ro', uri=True)
    old_project = source.execute('SELECT project_id FROM studio_jobs WHERE id=?', (job_id,)).fetchone()[0]
    legal, short = source.execute('SELECT legal_name,short_name FROM studio_projects WHERE project_id=?', (old_project,)).fetchone()
    saved = _read(source, job_id, 'collection_round_v1')
    history = {r['unit_id']: r for r in saved['receipts']}

    pipeline = create_studio_app(workdir, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project(short or legal)
    pipeline.store.upsert_project(project.id, {'legal_name': legal, 'short_name': short or ''}, consent=True)
    pipeline.store.enqueue(project.id, 'collect', input_version='replay:' + job_id[:40])
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    pipeline._execution_state.current = execution
    transport = ReplayTransport(Corpus(corpus_path))
    pipeline.http_factory = replay_factory(transport)

    units = [u for u in saved['plan']['units'] if u['source'] not in ('search', 'site', 'cninfo')]
    candidates = [{k: (project.id if k == 'target_id' and u[k] == old_project else u[k]) for k in CANDIDATE_KEYS if k in u}
                  for u in units]
    plan = make_plan(round_id=execution.job['id'], project_id=project.id, binding='offline-replay',
                     mode=saved['plan']['mode'], candidates=candidates, at=saved['plan']['created_at'])
    _, _, _, collectors = pipeline.tools()
    sources = CollectionSources(execution, collectors)  # the same adapters the collect job uses
    ledger = CollectionLedger(execution, plan)
    run_collection(ledger, collect=sources.collect, ingest=CollectionIngest(execution), guard=lambda: None)

    rows = []
    for unit in plan['units']:
        now = next((r for r in ledger.receipts if r['unit_id'] == unit['id']), {})
        then = next((history[o['id']] for o in units if o['source'] == unit['source'] and o['query'] == unit['query']
                     and o['id'] in history), {})
        rows.append(dict(source=unit['source'], query=unit['query'][:60], capability=unit['capability'],
                         historical=then.get('outcome'), historical_count=then.get('result_count'),
                         replayed=now.get('outcome'), replayed_count=now.get('result_count')))
    report = dict(job_id=job_id, units=rows, transport=transport.report(),
                  stored={kind: research_sources(pipeline.store, project.id, kind)['total'] for kind in ('paper', 'patent', 'trial')})
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    raise SystemExit(main(*sys.argv[1:]))
