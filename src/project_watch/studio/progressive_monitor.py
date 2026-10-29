"""Monitor source delivery ends at durable events and one analysis handoff."""
from contextlib import closing

from project_watch.studio import (
    bootstrap_collection,
    collection_host,
    event_insight_work,
    progressive_bootstrap,
    question_work,
    scope_maintenance,
)
from project_watch.studio.dsh_bootstrap import PublicBootstrap, text_arg


class MaterialMonitor(PublicBootstrap):
    bootstrap_projection = False
    allow_scope_projection = False

    def publication_intent(self, news):
        event_insight_work.publication(self.execution, news)

    def project_monitoring_scope(self, records):
        scope_maintenance.intent(self, records)
        return dict(admitted_count=0, deferred_count=len(records),
                    deferred=[{'record_key': r['key'], 'reason': 'separate_scope_maintenance'} for r in records])


class MaterialTopic(MaterialMonitor):
    topic_projection = True

    def __init__(self, pipeline, execution, collectors, reader):
        self.topic_request = execution.read_checkpoint('collection_request_v1')
        super().__init__(pipeline, execution, collectors, reader)

    def guard(self):
        from project_watch.studio.dsh_topic import parents, snapshot

        super().guard()
        selected = next((w for w in self.pipeline.monitoring_plan.items(self.project_id)
                         if w['id'] == self.topic_request['question']['id']), None)
        if not selected or snapshot(selected) != self.topic_request['question']:
            raise ValueError('selected question changed')
        with closing(self.store._connect()) as db:
            if parents(db, self.project_id, selected['parent_id']) != self.topic_request['parents']:
                raise ValueError('selected question parent changed')

    def pw_search(self, args):
        from project_watch.studio import collection_dsh
        from project_watch.studio.collection_scope import watch_direction

        watch = self.topic_request['question']
        return collection_dsh.search(self, text_arg(args, 'query', 300), watch_direction(watch), target_id='watch:' + watch['id'])

    def pw_finish(self, args):
        result = super().pw_finish(args)
        result.update(mode='topic', source_errors=self.state['errors'],
            material_work=self.execution.read_checkpoint(progressive_bootstrap.CLOSEOUT),
            source_collection=(self.execution.read_checkpoint('bootstrap_collection_v1') or {}).get('finished'))
        self.state['finished'] = result
        return result


def handoff(pipeline, execution, result):
    state = event_insight_work.settle(pipeline, execution)
    question_work.settle(pipeline, execution, result)
    scope_maintenance.dispatch(pipeline, execution.job['project_id'], execution)
    return state


def run(pipeline, execution, runtime_root):
    from project_watch.studio import dsh_runtime

    request = execution.read_checkpoint('collection_request_v1') or {}
    mode = request.get('mode', 'incremental')
    if mode not in ('incremental', 'refresh', 'topic'):
        raise ValueError('invalid material monitor mode')
    admission = collection_host.admit(execution, mode=mode)
    if not admission['enabled']:
        raise ValueError('material monitor requires fresh collection admission')
    collection_host.limit_budgets(execution, mode)
    if execution.read_checkpoint(event_insight_work.ROUND) is None:
        execution.checkpoint(question_work.PROTOCOL, True)
    event_insight_work.begin(execution)
    settings, http, _, collectors = pipeline.tools()
    research = (MaterialTopic if mode == 'topic' else MaterialMonitor)(pipeline, execution, collectors, pipeline.reader())
    if research.state['finished']:
        research.guard()
        handoff(pipeline, execution, research.state['finished'])
        return research.state['finished']
    http = dsh_runtime.guarded_http(research, http)
    collectors.http = http
    dsh_runtime.Gateway(research, settings.model, http)
    collection_host.prepare(research)
    directory = dsh_runtime.session_directory(research)
    bootstrap_collection.run(research, settings.model, http, runtime_root, directory)
    result = progressive_bootstrap.run(research, settings.model, http, runtime_root, directory)
    state = handoff(pipeline, execution, result)
    result['analysis_work'] = {k: state[k] for k in ('phase', 'insight_job_id', 'unresolved', 'cap') if k in state}
    research.state['finished'] = result
    research.save()
    return result
