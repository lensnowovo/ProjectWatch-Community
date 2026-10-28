"""Monitor source delivery ends at durable events and one analysis handoff."""
from project_watch.studio import (
    bootstrap_collection,
    collection_host,
    event_insight_work,
    progressive_bootstrap,
    scope_maintenance,
)
from project_watch.studio.dsh_bootstrap import PublicBootstrap


class MaterialMonitor(PublicBootstrap):
    bootstrap_projection = False
    allow_scope_projection = False

    def publication_intent(self, news):
        event_insight_work.publication(self.execution, news)

    def project_monitoring_scope(self, records):
        scope_maintenance.intent(self, records)
        return dict(admitted_count=0, deferred_count=len(records),
                    deferred=[{'record_key': r['key'], 'reason': 'separate_scope_maintenance'} for r in records])


def run(pipeline, execution, runtime_root):
    from project_watch.studio import dsh_runtime

    request = execution.read_checkpoint('collection_request_v1') or {}
    mode = request.get('mode', 'incremental')
    if mode not in ('incremental', 'refresh'):
        raise ValueError('invalid material monitor mode')
    admission = collection_host.admit(execution, mode=mode)
    if not admission['enabled']:
        raise ValueError('material monitor requires fresh collection admission')
    collection_host.limit_budgets(execution, mode)
    event_insight_work.begin(execution)
    settings, http, _, collectors = pipeline.tools()
    research = MaterialMonitor(pipeline, execution, collectors, pipeline.reader())
    if research.state['finished']:
        research.guard()
        event_insight_work.settle(pipeline, execution)
        scope_maintenance.dispatch(pipeline, research.project_id, execution)
        return research.state['finished']
    http = dsh_runtime.guarded_http(research, http)
    collectors.http = http
    dsh_runtime.Gateway(research, settings.model, http)
    collection_host.prepare(research)
    directory = dsh_runtime.session_directory(research)
    bootstrap_collection.run(research, settings.model, http, runtime_root, directory)
    result = progressive_bootstrap.run(research, settings.model, http, runtime_root, directory)
    handoff = event_insight_work.settle(pipeline, execution)
    scope_maintenance.dispatch(pipeline, research.project_id, execution)
    result['analysis_work'] = {k: handoff[k] for k in ('phase', 'insight_job_id', 'unresolved', 'cap') if k in handoff}
    research.state['finished'] = result
    research.save()
    return result
