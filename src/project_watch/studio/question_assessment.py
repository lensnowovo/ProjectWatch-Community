"""Fresh question assessments with the existing independent evidence reviewer."""
import json
from contextlib import closing
from typing import ClassVar

from project_watch.studio import dsh_runtime, public_stage_work, question_work
from project_watch.studio.dsh_bootstrap import PublicBootstrap, digest, text_arg
from project_watch.studio.dsh_extraction import ExtractionGateway
from project_watch.studio.dsh_monitor import PublicMonitor
from project_watch.studio.event_insight import ObjectInsight, RoleExecution
from project_watch.studio.http import HttpError
from project_watch.studio.material_ownership import project_guard
from project_watch.studio.questions import Assessment
from project_watch.studio.store import now

TOOLS = ('pw_context', 'pw_assess', 'pw_status', 'pw_finish')


class QuestionRole(PublicMonitor):
    tools = TOOLS
    prompt = question_work.PROMPT
    labels: ClassVar = {'pw_context': 'DSH 读取固定问题', 'pw_assess': 'DSH 复核问题答案'}
    runtime_receipt_stage = 'question_runtime'
    review_candidate = ObjectInsight.review_candidate

    def __init__(self, pipeline, execution, collectors, work):
        self.work = work
        PublicBootstrap.__init__(self, pipeline, execution, collectors, None)
        self.analysis_only = True
        self.session_id = 'question-' + work['fingerprint'][:24]
        self.task = json.dumps(dict(watch_id=work['watch']['id'], fingerprint=work['fingerprint']), ensure_ascii=False)
        saved = execution.read_checkpoint('question_answer')
        if saved:
            self.state['finished'] = saved

    def guard(self):
        PublicBootstrap.guard(self)
        with closing(self.store._connect()) as db:
            project_guard(db, self.project_id, self.execution.job['id'])

    def check_inputs(self):
        self.guard()
        with closing(self.store._connect()) as db:
            question_work.validate(self.pipeline, self.project_id, self.work, db=db, check_pending=True)

    def context(self, *, event_ids=None):
        self.check_inputs()
        return dict(numbered=self.work['numbered'], watches=[self.work['watch']], fingerprint=self.work['fingerprint'],
            event_versions=self.work['event_versions'], question_contract=dict(exit_rule=self.work['watch']['exit_rule'],
                rule='answered requires this unchanged exit rule to be satisfied by cited evidence; evidence_only is not object progress',
                answer_allowed=self.work['input_complete'] and self.work.get('collection_complete', False),
                knowledge_states=['answered', 'unresolved', 'insufficient']))

    def pw_context(self, args):
        ctx = self.context()
        self.execution.checkpoint_chunked('dsh_monitor_context', ctx)
        return {**ctx, 'next_tool': 'pw_assess'}

    def frozen(self):
        self.check_inputs()
        ctx = self.execution.read_checkpoint('dsh_monitor_context')
        if not ctx or ctx['fingerprint'] != self.work['fingerprint']:
            raise ValueError('fixed question context required')
        return ctx

    def routed_refs(self, ctx, watch):
        return list(ctx['numbered']) if watch['id'] == self.work['watch']['id'] else []

    def source_rows(self):
        result = {}
        for row in self.work['numbered'].values():
            result.setdefault(row['id'], []).append(dict(title=row['title'], body=row['context']))
        return result

    def defer(self, reason):
        self.state['finished'] = dict(status='pending', reason=reason, watch_id=self.work['watch']['id'],
                                      knowledge_state='unresolved', fingerprint=self.work['fingerprint'])
        self.save()
        return self.state['finished']

    def pw_status(self, args):
        return dict(watch_id=self.work['watch']['id'], fingerprint=self.work['fingerprint'],
                    remaining=max(0, self.execution.budgets['model'] - self.execution.budget_used('model')))

    def pw_assess(self, args):
        ctx, watch = self.frozen(), self.work['watch']
        if args.get('watch_id') != watch['id']:
            raise ValueError('fixed question required')
        note = text_arg(args, 'note', 500)
        entries = self.statement_evidence(ctx, args, watch, 'note')
        status, basis = args.get('status'), args.get('change_basis')
        if status not in ('progress', 'setback', 'unchanged', 'insufficient') or basis not in ('object_change', 'evidence_only', 'unclear'):
            raise ValueError('question status and change basis required')
        effective = 'unchanged' if basis == 'evidence_only' else 'insufficient' if basis == 'unclear' else status
        answer, gaps = args.get('answer_status'), args.get('gaps', [])
        if answer not in ('answered', 'unresolved', 'insufficient') or not isinstance(gaps, list) or len(gaps) > 8 or any(
                not isinstance(gap, str) or not gap.strip() or len(gap) > 500 for gap in gaps):
            raise ValueError('bounded question answer state required')
        if answer != 'answered' and not gaps:
            raise ValueError('unresolved question requires gaps')
        if answer == 'answered' and (not watch['exit_rule'] or basis == 'unclear'):
            raise ValueError('answered requires fixed exit condition and supported evidence')
        if answer == 'answered' and not ctx['question_contract']['answer_allowed']:
            raise ValueError('incomplete source/material or bounded question input cannot close the question')
        analysis = dict(watch_id=watch['id'], status=answer, text=note, refs=args['refs'], gaps=gaps,
                        assessment_status=effective, change_basis=basis)
        self.execution.checkpoint_chunked('question_draft', args)
        review = self.review_candidate(analysis, ctx)
        if review is None:
            return self.state['finished']
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self.execution._guard(db)
            project_guard(db, self.project_id, self.execution.job['id'])
            question_work.validate(self.pipeline, self.project_id, self.work, db=db, check_pending=True)
            assessment = Assessment(status=effective, note=note, event_ids=tuple(dict.fromkeys(e['id'] for e in entries)),
                visibility='public', idempotency_key=digest([self.work['fingerprint'], analysis, review['binding']]))
            record = self.pipeline.questions.record(self.project_id, watch['id'], 'system', assessment,
                fingerprint=self.work['fingerprint'], include_internal=False,
                consumed_events=tuple(e for e in self.work['pending_events'] if e['id'] in assessment.event_ids),
                expected_question_fingerprint=self.work['question_fingerprint'], db=db)
            closure = 'not_answered'
            if answer == 'answered':
                current_watch = dict(db.execute('SELECT * FROM studio_watchpoints WHERE id=?', (watch['id'],)).fetchone())
                if self.plan._owned_by_people(current_watch) or current_watch['current_state'] != watch['current_state']:
                    self.plan._log(db, self.project_id, watch['id'], 'modify', 'system', '公开问题答案已通过独立复核，等待所有者确认',
                        {'current_state': current_watch['current_state']}, {'current_state': note}, state='proposed', evidence=entries)
                    closure = 'owner_confirmation_required'
                else:
                    exploration = db.execute('SELECT * FROM studio_explorations WHERE watch_id=?', (watch['id'],)).fetchone()
                    if exploration and exploration['state'] == 'active':
                        self.plan.update(watch['id'], {'current_state': note}, actor='system', reason='满足固定问题退出标准',
                            evidence=entries, expected_lifecycle_version=watch['lifecycle_version'],
                            expected_exploration_generation=self.work['exploration_generation'], exploration_answer=True, db=db)
                        closure = 'answered'
                    else:
                        closure = 'exploration_not_active'
            key = 'question:' + watch['id']
            dependency = self.pipeline.evidence.dependencies.track(self.project_id, key, 'question', self.work['evidence_ids'], db=db)
            if dependency['status'] != 'current':
                raise ValueError('question evidence is not current')
            self.store.event_revisions.bind(self.project_id, key, self.work['event_versions'], db=db)
            result = dict(status='complete', watch_id=watch['id'], fingerprint=self.work['fingerprint'], work=self.work,
                knowledge_state='unknown' if answer == 'insufficient' else answer, answer_status=answer,
                assessment_id=record['id'], assessment_status=effective, change_basis=basis, text=note, gaps=gaps,
                refs=args['refs'], review=review, closure=closure, job_id=self.execution.job['id'], updated_at=now())
            db.execute('INSERT INTO studio_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('question-public:' + self.project_id + ':' + watch['id'], json.dumps(result, ensure_ascii=False)))
            public_stage_work.put(db, self.execution.job['id'], self.execution._stage('question_answer'), result)
        self.state['finished'] = result
        return result

    def pw_finish(self, args):
        self.frozen()
        text_arg(args, 'reason', 500)
        return self.defer('assessment_not_submitted')


class QuestionGateway(dsh_runtime.Gateway):
    received = ExtractionGateway.received

    def model_call(self, payload):
        role = self.research
        with role.lock, role.store.write_scope(role.execution._guard):
            role.guard()
            if not role.state['finished']:
                role.check_inputs()
            if self.closing.is_set():
                raise HttpError('execution_stopped')
            if payload.get('model') != self.model.model or {t.get('function', {}).get('name') for t in payload.get('tools', [])} != set(TOOLS):
                raise ValueError('unexpected question model/tool route')
            body = {k: payload[k] for k in ('model', 'messages', 'tools', 'tool_choice') if k in payload}
            body.update(stream=False, max_tokens=min(self.model.max_output_tokens, 4096))
            attempts = role.execution.read_checkpoint('question_attempts') or []
            replay = self.received(body)
            if not replay and not role.state['finished'] and (len(attempts) >= 4 or role.execution.budget_used('model') >= role.execution.budgets['model']):
                role.defer('question_generation_budget_or_turn_limit')
            if role.state['finished']:
                return {'choices': [{'message': {'role': 'assistant', 'content': '问题评估阶段已保存。'}, 'finish_reason': 'stop'}]}
            self.advertised_tools.update(TOOLS)
            signature = digest(body)
            if not replay and signature not in attempts:
                role.execution.checkpoint('question_attempts', [*attempts, signature])
            return self._send_model(body)


def run(pipeline, execution, runtime_root):
    manifest = execution.read_checkpoint(public_stage_work.INPUT)
    batch = public_stage_work.prepare(pipeline, execution, manifest)
    results = []
    for work in batch['questions']:
        scoped = RoleExecution(execution, work['fingerprint'])
        settings, http, _, collectors = pipeline.tools(execution=scoped)
        role = QuestionRole(pipeline, scoped, collectors, work)
        if not role.state['finished']:
            try:
                role.check_inputs()
                if not work['numbered']:
                    if question_work.current(pipeline, role.project_id, work['watch']['id']):
                        role.defer('no_current_routed_evidence_for_recheck')
                    else:
                        role.state['finished'] = dict(status='complete', knowledge_state='unknown', watch_id=work['watch']['id'],
                            reason='no_routed_admitted_evidence', fingerprint=work['fingerprint'])
                        role.save()
                else:
                    gateway = QuestionGateway(role, settings.model, dsh_runtime.guarded_http(role, http))
                    draft = scoped.read_checkpoint('question_draft')
                    if draft:
                        role.pw_context({})
                        role.tool('pw_assess', draft)
                    if not role.state['finished']:
                        dsh_runtime.run_dsh(role, gateway, runtime_root, dsh_runtime.session_directory(role) / role.session_id, timeout=180)
            except ValueError:
                role.guard()
                try:
                    role.check_inputs()
                except (ValueError, KeyError):
                    role.defer('input_version_changed')
                else:
                    raise
        outcome = role.state['finished']
        results.append(outcome)
        if (outcome and outcome['status'] == 'complete' and batch.get('collection_complete')
                and not pipeline.questions.new_events(role.project_id, work['watch']['id'], limit=1)):
            pipeline.monitoring_plan.explorations.finish(role.project_id, batch['cycle'], watch_ids={work['watch']['id']})
        execution.checkpoint_chunked(question_work.RESULT, dict(questions=results,
            status='complete' if all(r and r['status'] == 'complete' for r in results) else 'pending',
            model_used=execution.budget_used('model'), cap=manifest['cap']))
