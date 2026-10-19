import json

import pytest
from test_studio_competition_research import research_pipeline
from test_studio_evidence_versions import sample


def mixed_output(tmp_path):
    pipeline, pid, _, _ = research_pipeline(tmp_path)
    text = ('Synthetic Optics works with Beta in a trial. Synthetic Optics Series A announced on 2026-08-01. '
            'Synthetic Optics Series B received 数千万 元 CNY 2026Q3 公司单体 on 2026-08-02.')
    pipeline.evidence.save(sample(pid, text).model_copy(update={'source_key': 'public-profile:mixed', 'visibility': 'public'}))
    inputs = pipeline.structured_facts.inputs(pid, pipeline.model_binding())
    proposal = {'relations': [{'source': 'B1', 'quote': text, 'peer': 'Beta', 'role': 'pilot_partner',
        'stage': 'trial', 'role_text': 'works with', 'stage_text': 'trial', 'basis': 'actual'}],
        'events': [{'source': 'B1', 'quote': text, 'name': 'Series A', 'channel': 'funding',
            'state': 'announced', 'state_text': 'announced', 'role': 'recipient', 'basis': 'actual', 'occurred_at': '2026-08-01'}],
        'funds': [{'source': 'B1', 'quote': text, 'measure': 'funding', 'stage': 'received', 'stage_text': 'received',
            'value': '数千万', 'unit': '元', 'currency': 'CNY', 'period': '2026Q3',
            'consolidation_scope': '公司单体', 'occurred_at': '2026-08-02', 'basis': 'actual'}]}
    return pipeline, pid, inputs, proposal


def test_mixed_output_retains_valid_rows_without_inventing_amount_or_stage(tmp_path):
    from project_watch.studio.observations import read
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    result = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert result['status'] == 'partial' and result['rejected'] == 1 and result['unquantified'] == 1
    assert len(result['fact_ids']) == 2
    assert pipeline.structured_facts.apply(pid, inputs, proposal) == result
    rows = read(pipeline, pid)['items']
    assert {r['kind'] for r in rows} == {'business_relation', 'funding_fact'}
    money = next(r for r in rows if r['kind'] == 'funding_fact')
    assert money['raw_value'] is None and money['normalized_value'] is None
    assert not money['is_funding_received'] and money['support'] == 'pending'
    assert '数千万' in money['refs'][0]['quote']


@pytest.mark.parametrize('value', ['not in original', 1200, {'nested': 'sensitive'}])
def test_bad_money_field_is_rejected_not_coerced_to_numeric_fact(tmp_path, value):
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    proposal['funds'][0]['value'] = value
    result = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert len(result['fact_ids']) == 1 and result['rejected'] == 2
    assert 'sensitive' not in json.dumps(result) and 'not in original' not in json.dumps(result)


def test_malformed_rows_are_isolated_but_wrong_envelopes_fail(tmp_path):
    from pydantic import ValidationError
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    proposal['events'] += [None, {'quote': 'sensitive invalid payload'}]
    result = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert result['rejected'] == 3 and len(result['fact_ids']) == 2
    assert 'sensitive' not in json.dumps(result)
    for bad in ({'events': 'not-list'}, {'unexpected': []}):
        with pytest.raises(ValidationError):
            pipeline.structured_facts.apply(pid, inputs, bad)


def test_more_than_twelve_rows_are_validated_without_truncation(tmp_path):
    from project_watch.studio.structured_facts import parse_proposal
    _, _, _, proposal = mixed_output(tmp_path)
    rows = [{**proposal['relations'][0], 'peer': f'Peer {i}'} for i in range(16)]
    parsed, rejected, _, issues = parse_proposal({'relations': [*rows, None]})
    assert [r.peer for r in parsed.relations] == [f'Peer {i}' for i in range(16)]
    assert rejected == 1 and issues[0]['index'] == 16


def test_extra_envelope_fields_preserve_rows_and_report_sanitized_partial(tmp_path):
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    proposal.pop('events')
    proposal.pop('funds')
    proposal.update({'private-key-name': {'instruction': 'sensitive payload'}, 'invented_facts': [1, 2]})
    result = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert len(result['fact_ids']) == 1
    assert result['status'] == 'partial' and result['rejected'] == 0
    assert result['ignored_fields'] == 2
    assert result['validation_issues'] == [{'group': 'envelope', 'codes': ['extra_ignored'], 'count': 2}]
    assert not any(text in json.dumps(result) for text in ('private-key-name', 'sensitive', 'invented_facts'))
    assert pipeline.structured_facts.apply(pid, inputs, proposal) == result


def test_extra_fields_do_not_hide_invalid_known_group_or_unknown_only_output():
    from pydantic import ValidationError

    from project_watch.studio.structured_facts import parse_proposal
    for payload in ({'unexpected': []}, {'events': None, 'extra': []}, {'events': 'secret', 'extra': []}, []):
        with pytest.raises(ValidationError):
            parse_proposal(payload)


def test_extra_fields_in_one_batch_do_not_discard_other_batches(tmp_path):
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    result = pipeline.structured_facts.apply(pid, inputs, None, batch_outputs=[
        {'relations': proposal['relations']}, {'events': [], 'private-extra': 'sensitive'},
    ])
    assert result['status'] == 'partial' and len(result['fact_ids']) == 1
    assert result['ignored_fields'] == 1 and result['rejected'] == 0


def test_nineteen_cached_batches_with_extra_envelope_finish_without_dispatch(tmp_path, monkeypatch):
    """Synthetic shape of the observed batch-7 failure; never uses a real receipt."""
    from copy import deepcopy

    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import Http

    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    # Exactly 19 batches at the production 12-part boundary. Repeated located
    # evidence stays synthetic; storage must deduplicate the repeated assertions.
    inputs = {**inputs, 'parts': [deepcopy(inputs['parts'][0]) for _ in range(19 * 12)]}
    monkeypatch.setattr(pipeline.structured_facts, 'inputs', lambda *_: inputs)
    def forbidden(*args, **kwargs):
        raise AssertionError('cached output replay attempted provider dispatch')
    pipeline.http_factory = lambda _: Http(forbidden)
    pipeline.schedule_details(pid)
    claimed = pipeline.store.claim()
    execution = JobExecution(pipeline.store, claimed)
    execution.checkpoint('detail_inputs', {'input_version': inputs['input_version'], 'binding': inputs['binding']})
    outputs = []
    for index in range(19):
        output = {group: [] for group in ('relations', 'policies', 'events', 'milestones', 'funds', 'commitments')}
        output['relations'] = proposal['relations']
        if index == 7:
            output.update({'private-extra-a': 'sensitive-a', 'private-extra-b': {'action': 'ignore evidence'},
                           'private-extra-c': [1, 2]})
        outputs.append(output)
        execution.checkpoint(f'detail_proposal_{index}', output)
    original = deepcopy(outputs)
    pipeline.run(claimed)
    assert pipeline.store.finish(claimed['id'], lease_token=claimed['lease_token'])
    result = pipeline.structured_facts.get(pid)
    assert result['status'] == 'partial' and result['ignored_fields'] == 3
    assert result['rejected'] == 0 and len(result['fact_ids']) == 1
    assert execution.usage() == {}
    assert not any(word in json.dumps(result) for word in ('private-extra', 'sensitive-a', 'ignore evidence'))
    assert outputs == original
    assert [execution.read_checkpoint(f'detail_proposal_{i}') for i in range(19)] == original
    assert pipeline.structured_facts.apply(pid, inputs, None, batch_outputs=outputs) == result


def test_cached_proposal_replays_after_local_apply_failure_without_model_retry(tmp_path, monkeypatch):
    from project_watch.studio.http import Http, Response
    pipeline, pid, _, proposal = mixed_output(tmp_path)
    calls = []
    def transport(method, url, headers, body, timeout):
        calls.append(url)
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps(proposal)}}]}).encode())
    pipeline.http_factory = lambda _: Http(transport)
    job = pipeline.schedule_details(pid)
    claimed = pipeline.store.claim()
    original = pipeline.structured_facts.apply
    def fail_once(*args, **kwargs):
        raise RuntimeError('synthetic local apply crash')
    monkeypatch.setattr(pipeline.structured_facts, 'apply', fail_once)
    with pytest.raises(RuntimeError):
        pipeline.run(claimed)
    monkeypatch.setattr(pipeline.structured_facts, 'apply', original)
    pipeline.run(claimed)
    pipeline.store.finish(job['id'], lease_token=claimed['lease_token'])
    assert len(calls) == 1 and pipeline.structured_facts.get(pid)['status'] == 'partial'


def test_seeded_saved_output_can_complete_without_any_provider_request(tmp_path):
    from project_watch.studio.execution import JobExecution
    from project_watch.studio.http import Http
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('offline replay attempted network')
    pipeline.http_factory = lambda _: Http(forbidden)
    pipeline.schedule_details(pid)
    claimed = pipeline.store.claim()
    execution = JobExecution(pipeline.store, claimed)
    execution.checkpoint('detail_inputs', inputs)
    execution.checkpoint('detail_proposal', proposal)
    pipeline.run(claimed)
    assert pipeline.store.finish(claimed['id'], lease_token=claimed['lease_token'])
    assert pipeline.structured_facts.get(pid)['status'] == 'partial'
    assert execution.usage() == {}


def test_envelope_failure_is_sanitized_in_worker_job_error(tmp_path, monkeypatch):
    from project_watch.studio.http import Http, Response
    from project_watch.studio.pipeline import StudioWorker
    pipeline, pid, _, _ = mixed_output(tmp_path)
    def transport(*args):
        return Response(200, {}, json.dumps({'choices': [{'message': {'content': json.dumps({'events': 'secret-context'})}}]}).encode())
    pipeline.http_factory = lambda _: Http(transport)
    pipeline.schedule_details(pid)
    worker = StudioWorker(pipeline)
    monkeypatch.setattr(worker, 'schedule', lambda: None)
    assert worker.step()
    error = pipeline.store.jobs(pid)[0]['error']
    assert error == '模型返回的字段格式不符合要求，已有结果保留。请重试。'
    assert 'secret-context' not in error and 'pydantic' not in error


def test_partial_metadata_is_exposed_without_raw_invalid_output(tmp_path):
    from fastapi.testclient import TestClient

    from project_watch.studio.app import create_studio_app
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    pipeline.structured_facts.apply(pid, inputs, proposal)
    response = TestClient(create_studio_app(tmp_path, start_worker=False)).get(f'/api/studio/projects/{pid}')
    assert response.status_code == 200
    assert response.json()['detail_summary'] == {'status': 'partial', 'fact_count': 2, 'rejected': 1, 'unquantified': 1}


def test_explicit_new_output_can_improve_partial_result_without_losing_prior_facts(tmp_path):
    pipeline, pid, inputs, proposal = mixed_output(tmp_path)
    first = pipeline.structured_facts.apply(pid, inputs, proposal)
    proposal['events'][0]['state'] = 'unknown'
    proposal['funds'][0]['value'] = None
    second = pipeline.structured_facts.apply(pid, inputs, proposal)
    assert set(first['fact_ids']).issubset(second['fact_ids']) and len(second['fact_ids']) == 3
    assert second['rejected'] == 0 and second['unquantified'] == 1 and second['status'] == 'partial'
    assert pipeline.structured_facts.apply(pid, inputs, proposal) == second
