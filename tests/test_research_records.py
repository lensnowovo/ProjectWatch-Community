"""Typed research facts on the actual source -> review -> event -> insight path.

Synthetic transport only. No real company response or credentials in this file.
"""
import json
from contextlib import closing
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from test_event_insight import install as install_events
from test_event_insight import next_insight, source
from test_studio_dsh_monitor import synthetic_review

from project_watch.studio import (
    digest,
    dsh_runtime,
    evidence_tasks,
    insight,
    news_window,
    research_records,
)
from project_watch.studio.http import Http, HttpError, Response
from project_watch.studio.patent_registry import PatentRecord, normalize_page


def paper(affiliation='Synthetic Host', when=None):
    return dict(id='https://openalex.org/W1234567', display_name='Synthetic protein characterization',
        publication_date=when or datetime.now(UTC).date().isoformat(),
        authorships=[{'author': {'display_name': 'Synthetic Author'},
                     'institutions': [{'display_name': affiliation}], 'raw_affiliation_strings': []}],
        primary_location={'source': {'display_name': 'Synthetic Journal'}})


def install(tmp_path, monkeypatch, *, patent=False, rejection=False, old=False, two=False, rated=False, native=False,
            wrong_subject=False, false_grant=False):
    pipeline, execution, seen = install_events(tmp_path, monkeypatch, monitor=False, source_count=0, native=native)
    original = pipeline.http_factory(None).transport
    checked = []
    title = 'Synthetic Host patent application for synthetic proteins'
    run = dsh_runtime.run_dsh

    def capture_task(research, *args, **kwargs):
        seen['research_task'] = json.loads(research.task)
        return run(research, *args, **kwargs)

    monkeypatch.setattr(dsh_runtime, 'run_dsh', capture_task)

    def send(method, url, headers, raw, timeout):
        host = urlsplit(url).hostname
        if host == 'api.openalex.org':
            row = paper(affiliation='Other Institute' if rated or wrong_subject else 'Synthetic Host', when='2020-01-01' if old else None)
            rows = [row, row | {'id': 'https://openalex.org/W1234568'}] if two else [row]
            return Response(200, {}, json.dumps({'results': [] if patent else rows,
                                                 'meta': {'count': 0 if patent else 1}}).encode())
        if host == 'www.ebi.ac.uk':
            rows = [dict(id='US123456A1', source='PAT', title=title, patentDetails={'typeCode': 'A1'},
                         affiliation='Synthetic Host', firstPublicationDate='2020-01-01',
                         abstractText=title)] if patent and 'SRC%3APAT' in url else []
            return Response(200, {}, json.dumps({'hitCount': len(rows), 'resultList': {'result': rows}}).encode())
        body = json.loads(raw) if host == 'model.example.invalid' and raw else {}
        tools = {t['function']['name'] for t in body.get('tools', [])}
        if tools == {'pw_submit_records'}:
            task = seen['research_task']
            ref, part = next(iter(task['refs'].items()))
            text = title if patent else paper()['display_name']
            start = part['quote'].index(text)
            value = dict(kind='patent' if patent else 'paper', subject='Synthetic Host', label=text, text=text,
                refs=[ref], start=start, end=start+len(text), research_relation='technical_relevance',
                reason='Relevant synthetic research; no claim of ownership.',
                event_at=None if patent else datetime.now(UTC).date().isoformat())
            if rated:
                value.update(importance='notable', importance_reason='A reviewed synthetic technology tracking signal.')
            if wrong_subject:
                value['subject'] = 'Unrelated Company'
            if false_grant:
                value['text'] = text + '; granted.'
            message = {'role': 'assistant', 'tool_calls': [{'id': 'candidate', 'type': 'function',
                'function': {'name': 'pw_submit_records', 'arguments': json.dumps({'observations': [value]})}}]}
            return Response(200, {}, json.dumps({'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}).encode())
        if body and not tools:
            payload = json.loads(body['messages'][-1]['content'])
            if payload.get('purpose') == 'public_record_admission':
                checked.append(payload)
                assert payload['research_policy']
                for value in payload['fields'].values():
                    record = json.loads(value)
                    record = record.get('record', record)
                    assert record['research_metadata']['kind'] == ('patent' if patent else 'paper')
                    assert record['research_metadata']['legal_status'] == 'not_provided'
                reply = synthetic_review(payload)
                if rejection or wrong_subject or false_grant:
                    for check in reply['checks']:
                        check.update(status='insufficient', reason='Synthetic independent rejection: unsupported subject or legal status')
                return Response(200, {}, json.dumps({'choices': [{'message': {'role': 'assistant',
                    'content': json.dumps(reply)}, 'finish_reason': 'stop'}]}).encode())
        return original(method, url, headers, raw, timeout)

    pipeline.http_factory = lambda _: Http(send)
    return pipeline, execution, seen, checked


@pytest.mark.parametrize('patent', [False, True])
def test_source_review_event_analysis_and_report_windows(tmp_path, monkeypatch, patent):
    pipeline, execution, seen, checked = install(tmp_path, monkeypatch, patent=patent)
    handoff = source(pipeline, execution)
    pid = execution.job['project_id']
    assert checked  # independent admission is mandatory, even deterministic extraction
    [event] = pipeline.store.list_events(project_id=pid)
    assert event['category'] == ('patent' if patent else 'paper')
    with closing(pipeline.store._connect()) as db:
        item = db.execute('SELECT extra FROM studio_items WHERE event_id=?', (event['id'],)).fetchone()
        [claim] = json.loads(item[0])['dsh_claims']
    assert claim['research_metadata']['identifier']
    assert claim['research_metadata']['evidence_version_id'] == claim['refs'][0]['evidence_version_id']
    assert claim['research_relation'] == ('technical_relevance' if patent else 'author_affiliation')
    assert not (pipeline.store.get_public_profile(pid) or {}).get('facts')
    assert handoff['phase'] == 'scheduled'
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    result = insight.load(pipeline.store, pid)
    assert result['status'] == 'current' and result['review']['passed']
    # Today and weekly use admitted event dates, never import/observation times.
    today = news_window.summarize(pipeline.store, 1, project_id=pid)['counts']
    assert today['in_period'] == (0 if patent else 1)
    assert today['undated'] == (1 if patent else 0)
    week = digest.build_digest(pipeline.store, 7)
    assert not week['projects']  # neither a pending rating nor an undated patent becomes important news
    before = execution.usage()
    pipeline.run(execution.job)
    assert execution.usage() == before
    if not patent:
        assert not seen['materials']  # immutable bibliography saved one extraction request


def test_independent_rejection_keeps_candidate_and_never_publishes(tmp_path, monkeypatch):
    pipeline, execution, _, checked = install(tmp_path, monkeypatch, rejection=True)
    source(pipeline, execution)
    assert checked
    assert not pipeline.store.list_events(project_id=execution.job['project_id'])
    with closing(pipeline.store._connect()) as db:
        row = db.execute('SELECT status,result FROM studio_evidence_tasks').fetchone()
        assert row['status'] == 'complete' and json.loads(row['result'])['review_status'] == 'pending'
        assert db.execute('SELECT triaged FROM studio_items').fetchone()[0] == 0


def test_old_publication_does_not_become_this_week_news(tmp_path, monkeypatch):
    pipeline, execution, _, _ = install(tmp_path, monkeypatch, old=True)
    source(pipeline, execution)
    summary = news_window.summarize(pipeline.store, 7)['counts']
    assert summary['older'] == 1 and summary['in_period'] == 0
    assert not digest.build_digest(pipeline.store, 7)['projects']


def test_query_echo_never_becomes_affiliation():
    raw = paper(affiliation='Other Institution')
    raw['request'] = {'queryString': 'AFFILIATION:"Synthetic Host"'}
    value = research_records.metadata({'source_record': raw})
    assert value['affiliations'] == ['Other Institution']
    assert 'Synthetic Host' not in json.dumps(value)


def test_registry_is_off_until_real_provider_mapping_is_approved():
    with pytest.raises(HttpError, match='patent_registry_not_configured'):
        normalize_page([])


def registry():
    return dict(contract='patent-registry-record-v1', registry='Synthetic Registry', publication_number='US123456A1',
        title='Synthetic protein application', url='https://registry.example.invalid/US123456A1',
        applicants=['Synthetic Host'], inventors=['Synthetic Author'], status='application',
        event_date='2026-01-02', date_basis='application', status_quote='Application filed; not granted.',
        original_text='US123456A1 Synthetic protein application. Applicant: Synthetic Host. '
                      'Inventor: Synthetic Author. Application filed; not granted. 2026-01-02')


def test_patent_applicant_inventor_and_application_stay_separate():
    [item] = normalize_page([registry()], enabled=True)
    value = research_records.metadata(item | {'source_record': item['extra']['source_record']})
    assert value['applicants'] == ['Synthetic Host'] and value['inventors'] == ['Synthetic Author']
    assert value['authors'] == [] and value['legal_status'] == 'application'
    with pytest.raises(ValueError, match='date/status mismatch'):
        PatentRecord.model_validate(registry() | {'status': 'grant'})
    with pytest.raises(ValueError, match='verbatim evidence'):
        PatentRecord.model_validate(registry() | {'applicants': ['Other Company']})


def test_unverified_relation_cannot_be_supported_by_a_positive_reviewer(tmp_path, monkeypatch):
    from project_watch.studio import dsh_record_review
    from project_watch.studio.dsh_bootstrap import PublicBootstrap
    pipeline, execution, _, _ = install(tmp_path, monkeypatch)
    source(pipeline, execution)
    research = PublicBootstrap(pipeline, execution, None, None)
    [record] = research.state['records']
    record['research_relation'] = 'unverified'
    research.record_reviewer = lambda _: pytest.fail('Unattributed candidates must not spend a review call')
    accepted, _, receipt = dsh_record_review.admit(research, [record], [])
    assert not accepted and receipt['rejected'][0]['code'] == 'research_relation_unverified'


def test_started_material_cannot_switch_to_deterministic_extraction(tmp_path, monkeypatch):
    pipeline, execution, _, _ = install(tmp_path, monkeypatch)
    source(pipeline, execution)
    with closing(pipeline.store._connect()) as db:
        task = dict(db.execute('SELECT * FROM studio_evidence_tasks').fetchone())
    bound = evidence_tasks._input(execution, task)
    assert research_records.proposal(execution, task, bound)
    execution.checkpoint('extract_attempt:' + task['id'], {'old': 'request identity retained'})
    assert research_records.proposal(execution, task, bound) is None
    assert research_records.extraction_contract(execution, bound) is None


def test_distinct_stable_ids_never_merge_on_identical_paper_titles(tmp_path, monkeypatch):
    pipeline, execution, _, _ = install(tmp_path, monkeypatch, two=True)
    source(pipeline, execution)
    events = pipeline.store.list_events(project_id=execution.job['project_id'])
    assert len(events) == 2 and events[0]['title'] == events[1]['title']
    before = execution.usage()
    pipeline.run(execution.job)
    assert execution.usage() == before
    assert len(pipeline.store.list_events(project_id=execution.job['project_id'])) == 2


@pytest.mark.parametrize('native', [False, True])
def test_reviewed_research_signal_flows_into_weekly_and_insight(tmp_path, monkeypatch, native):
    pipeline, execution, _, checked = install(tmp_path, monkeypatch, rated=True, native=native)
    source(pipeline, execution)
    assert any('editorial_assessment' in json.loads(value) for p in checked for value in p['fields'].values())
    [event] = pipeline.store.list_events(project_id=execution.job['project_id'])
    assert event['category'] == 'paper' and event['importance'] == 'notable'
    week = digest.build_digest(pipeline.store, 7)
    assert len(week['projects']) == 1
    child = next_insight(pipeline, execution)
    pipeline.run(child.job)
    result = insight.load(pipeline.store, execution.job['project_id'])
    assert result['status'] == 'current' and result['review']['passed']


@pytest.mark.parametrize('patent', [False, True])
def test_wrong_subject_or_grant_claim_is_quarantined_by_independent_review(tmp_path, monkeypatch, patent):
    pipeline, execution, _, checked = install(tmp_path, monkeypatch, patent=patent,
        wrong_subject=not patent, false_grant=patent)
    source(pipeline, execution)
    assert checked
    fields = [json.loads(v) for p in checked for v in p['fields'].values()]
    assert any('granted.' in f['text'] for f in fields) if patent else any(f['subject'] == 'Unrelated Company' for f in fields)
    assert not pipeline.store.list_events(project_id=execution.job['project_id'])
    assert not digest.build_digest(pipeline.store, 7)['projects']
    assert not insight.load(pipeline.store, execution.job['project_id'])
    with closing(pipeline.store._connect()) as db:
        row = db.execute('SELECT status,result FROM studio_evidence_tasks').fetchone()
        assert row['status'] == 'complete' and json.loads(row['result'])['review_status'] == 'pending'
