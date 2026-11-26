"""A truncated or unusable competition reply ends its own step, not the whole round."""
import json
from contextlib import closing

import pytest
from test_studio_competition_research import research_pipeline
from test_studio_evidence_versions import sample

from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.llm import ModelError

TRUNCATED = json.dumps({'choices': [{'finish_reason': 'length', 'message': {'content': '{"products": [{"na'}}]}).encode()


def _kind(data):
    context = json.loads(data['messages'][1]['content'])
    if 'facts' in context:
        return 'review'
    if context['sources'][0]['source'].startswith('D'):
        return 'discovery'
    return 'business:' + context['company']


def _run_with_truncated(tmp_path, which):
    pipeline, pid, calls, _reads = research_pipeline(tmp_path)
    base = pipeline.http_factory(None).transport
    hits = []
    def transport(method, url, headers, body, timeout):
        data = json.loads(body)
        if 'api.tavily.com' not in url and _kind(data) == which:
            hits.append(which)
            return Response(200, {}, TRUNCATED)
        return base(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(transport)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    pipeline.run(job)
    before = len(calls)
    pipeline.run(job)  # a resumed job does not pay again for the unusable reply
    assert len(calls) == before and len(hits) == 1
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    assert pipeline.store.jobs(pid)[-1]['status'] == 'done'
    saved = pipeline.competition_research.saved(pid)
    assert saved['status'] == 'partial' and 'model_output_truncated' in saved['failures']
    return pipeline, pid, saved


def test_truncated_peer_review_ends_that_peer_only(tmp_path):
    _pipeline, _pid, saved = _run_with_truncated(tmp_path, 'review')
    assert saved['researched_count'] == 1 and saved['supported_count'] == 0
    assert any(c['name'] == 'Beta' for c in saved['candidates'])
    # The peer's reading is kept, so its page summary is still queued.
    [peer] = saved['reading']['peers']
    assert peer['body_count'] == 1


def test_truncated_target_product_batch_keeps_the_round(tmp_path):
    _pipeline, _pid, saved = _run_with_truncated(tmp_path, 'business:Synthetic Optics')
    assert saved['reading']['target']['unread_batches'] == 1
    # The confirmed product from the earlier profile still drives the peer comparison.
    assert saved['researched_count'] == 1 and saved['supported_count'] == 1


@pytest.mark.parametrize('which', ['discovery', 'review'])
def test_uncertain_reply_still_holds_the_job(tmp_path, which):
    # A timeout is not an unusable reply: discovery raises it to the job, a peer holds it.
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    base = pipeline.http_factory(None).transport
    def transport(method, url, headers, body, timeout):
        if 'api.tavily.com' not in url and _kind(json.loads(body)) == which:
            raise HttpError('connection_failed', detail='synthetic timed out', failure_kind='timeout')
        return base(method, url, headers, body, timeout)
    pipeline.http_factory = lambda _: Http(transport)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    if which == 'discovery':
        with pytest.raises(ModelError) as raised:
            pipeline.run(job)
        assert raised.value.code == 'model_timeout'
    else:
        pipeline.run(job)
        pipeline.store.finish(job['id'], lease_token=job['lease_token'])
        assert pipeline.store.jobs(pid)[-1]['status'] == 'failed'
    with pipeline.store._connect() as db:
        assert db.execute("select count(*) from studio_job_calls where status='unknown'").fetchone()[0] == 1


def test_truncated_discovery_reply_keeps_the_round(tmp_path):
    _pipeline, _pid, saved = _run_with_truncated(tmp_path, 'discovery')
    assert saved['candidate_count'] == 0 and saved['researched_count'] == 0
    # The target's own public pages were still read.
    assert saved['reading']['target']['body_count'] == 1


def test_truncated_peer_product_batch_is_marked_unread(tmp_path):
    _pipeline, _pid, saved = _run_with_truncated(tmp_path, 'business:Beta')
    assert saved['researched_count'] == 1 and saved['supported_count'] == 0
    [peer] = saved['reading']['peers']
    assert peer['unread_batches'] == 1 and peer['unread_codes'] == ['model_output_truncated']


def _batches(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    inputs = {'input_version': 'v1', 'binding': pipeline.model_binding(),
              'parts': [{'text': f'part {i}'} for i in range(25)]}  # three batches of at most 12
    return pipeline, pid, inputs


class _Model:
    def __init__(self, failing_batch, code):
        self.seen, self.failing_batch, self.code = 0, failing_batch, code

    def json(self, system, body, max_tokens):
        index, self.seen = self.seen, self.seen + 1
        if index == self.failing_batch:
            raise ModelError('synthetic', code=self.code)
        return {'products': [], 'batch': index}


def test_skipped_batch_keeps_its_siblings(tmp_path):
    pipeline, pid, inputs = _batches(tmp_path)
    skipped = []
    proposals = pipeline._extract_all_parts(pid, inputs, _Model(1, 'model_output_truncated'), 'system',
        lambda value: {'parts': value['parts']}, 'peer_x', lambda: inputs, isolate_failures=False, skipped=skipped)
    assert [p['batch'] for p in proposals] == [0, 2]
    assert skipped == ['model_output_truncated']


@pytest.mark.parametrize('code', ['request_outcome_unknown', 'model_timeout', 'unknown_not_resent'])
def test_provider_uncertainty_still_holds(tmp_path, code):
    pipeline, pid, inputs = _batches(tmp_path)
    with pytest.raises(ModelError) as raised:
        pipeline._extract_all_parts(pid, inputs, _Model(1, code), 'system',
            lambda value: {'parts': value['parts']}, 'peer_x', lambda: inputs, isolate_failures=False, skipped=[])
    assert raised.value.code == code


class _Checkpoints:
    def __init__(self):
        self.data = {}

    def read_checkpoint(self, name):
        return self.data.get(name)

    def checkpoint(self, name, value):
        self.data[name] = value


def test_resume_does_not_pay_again_for_an_unusable_batch(tmp_path):
    pipeline, pid, inputs = _batches(tmp_path)
    execution = _Checkpoints()
    pipeline._execution_state.current = execution
    try:
        class Held(_Model):
            def json(self, system, body, max_tokens):
                if self.seen == 2:
                    self.seen += 1
                    raise ModelError('synthetic', code='request_outcome_unknown')
                return super().json(system, body, max_tokens)
        first = Held(1, 'model_output_truncated')
        with pytest.raises(ModelError):
            pipeline._extract_all_parts(pid, inputs, first, 'system', lambda value: {'parts': value['parts']},
                                        'peer_x', lambda: inputs, isolate_failures=False, skipped=[])
        assert first.seen == 3
        resumed, skipped = _Model(None, ''), []
        proposals = pipeline._extract_all_parts(pid, inputs, resumed, 'system', lambda value: {'parts': value['parts']},
                                                'peer_x', lambda: inputs, isolate_failures=False, skipped=skipped)
        # Batch 0 replays from its checkpoint, batch 1 stays skipped, only batch 2 is sent again.
        assert resumed.seen == 1 and skipped == ['model_output_truncated']
        assert [p['batch'] for p in proposals] == [0, 0]
    finally:
        pipeline._execution_state.current = None


def test_a_partial_profile_is_replaced_by_a_later_complete_read(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    profiles = pipeline.business_profiles
    company = pipeline.objects.ensure_subject(pid)
    text = 'Synthetic Optics sells Lens B for drug screening.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={'visibility': 'public'}))
    current = profiles.inputs(pid, binding=pipeline.model_binding(), subject_id=company['id'], evidence_ids=[evidence['id']])
    partial = profiles.apply(pid, current, None, batch_outputs=[{'products': []}], unread_batches=1)
    assert partial['unread_batches'] == 1 and partial['accepted'] == 0
    part = current['parts'][0]
    quote = text
    assert quote in part['text']
    row = {'company': 'Synthetic Optics', 'name': 'Lens B', 'source': part['marker'], 'quote': quote,
           'use_case': '', 'buyer_type': '', 'delivery': '', 'basis': 'actual', 'polarity': 'asserted'}
    complete = profiles.apply(pid, current, None, batch_outputs=[{'products': [row]}])
    assert 'unread_batches' not in complete
    # A complete record for the same input version is final.
    again = profiles.apply(pid, current, None, batch_outputs=[{'products': []}], unread_batches=1)
    assert again == complete


def test_one_malformed_batch_answer_leaves_only_that_batch_unread(tmp_path):
    pipeline, pid, _calls, _reads = research_pipeline(tmp_path)
    profiles = pipeline.business_profiles
    company = pipeline.objects.ensure_subject(pid)
    text = 'Synthetic Optics sells Lens B for drug screening.'
    evidence = pipeline.evidence.save(sample(pid, text).model_copy(update={'visibility': 'public'}))
    current = profiles.inputs(pid, binding=pipeline.model_binding(), subject_id=company['id'], evidence_ids=[evidence['id']])
    part = current['parts'][0]
    row = {'company': 'Synthetic Optics', 'name': 'Lens B', 'source': part['marker'], 'quote': text,
           'use_case': '', 'buyer_type': '', 'delivery': '', 'basis': 'actual', 'polarity': 'asserted'}
    # A live batch answered {"error": "no material provided"} instead of a product list.
    result = profiles.apply(pid, current, None, batch_outputs=[{'products': [row]}, {'error': 'no material'}])
    assert result['accepted'] == 1 and result['unread_batches'] == 1
    # A single answer with a wrong envelope, or no usable answer at all, keeps the existing profile.
    for outputs in ([{'error': 'no material'}], [{'error': 'no material'}, {'error': 'none either'}]):
        with pytest.raises(ValueError):
            profiles.apply(pid, current, None, batch_outputs=outputs)
    # Batches already skipped upstream do not count as malformed answers here.
    mixed = profiles.apply(pid, current, None, batch_outputs=[{'products': [row]}, {'error': 'x'}], unread_batches=1)
    assert mixed['unread_batches'] == 2 and mixed['accepted'] == 1


def test_saved_batches_replay_without_rebuilding_inputs(tmp_path):
    pipeline, pid, inputs = _batches(tmp_path)
    execution = _Checkpoints()
    pipeline._execution_state.current = execution
    builds = []

    def current():
        builds.append(1)
        return inputs
    try:
        pipeline._extract_all_parts(pid, inputs, _Model(None, ''), 'system', lambda value: {'parts': value['parts']},
                                    'peer_x', current)
        assert len(builds) == 3  # checked before each paid batch
        builds.clear()
        moved = {**inputs, 'input_version': 'v2'}
        proposals = pipeline._extract_all_parts(pid, inputs, _Model(None, ''), 'system',
                                                lambda value: {'parts': value['parts']}, 'peer_x', lambda: moved)
        assert builds == [] and [p['batch'] for p in proposals] == [0, 1, 2]  # replayed, nothing rebuilt or sent
        execution.data.pop('peer_x_proposal_2')
        with closing(pipeline.store._connect()) as db, db:
            db.execute('DELETE FROM studio_extraction_reuse')  # nothing earlier to reuse either
        with pytest.raises(ModelError) as raised:  # a batch still to pay for checks the material first
            pipeline._extract_all_parts(pid, inputs, _Model(None, ''), 'system',
                                        lambda value: {'parts': value['parts']}, 'peer_x', lambda: moved)
        assert raised.value.code == 'input_superseded'
    finally:
        pipeline._execution_state.current = None
