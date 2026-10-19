from scripts.claude_review_offline_probe import collector_proxy, e0, e4, fixture_audit, reuse


def test_e0_separates_coverage_from_direct_attempt_outcomes():
    rows = [{'url': 'https://news.google.com/rss/a', 'extra': '{}', 'category': 'company',
             'body_status': status, 'body': text, 'relevant': 1, 'importance': 'major'}
            for status, text in [('skipped', ''), ('twin_body', 'borrowed body')]]
    row = e0(rows)['by_collector_proxy']['google_news_url']
    assert row['usable_body'] == 1 and row['body_coverage'] == 0.5
    assert row['direct_success_rate'] is None
    assert row['notable_major_with_body'] == 1
    assert collector_proxy({**rows[0], 'url': 'https://publisher.test/a'}) == 'unattributed'


def test_reuse_does_not_count_same_project_duplicates_as_cross_project_savings():
    rows = [{'project_id': p, 'url': u} for p, u in [('a', 'u'), ('a', 'u'), ('b', 'u'), ('b', 'v')]]
    row = reuse(rows, 'url')
    assert row['occurrences'] == 4 and row['unique_keys'] == 2
    assert row['shared_occurrences'] == 3 and row['avoidable_cross_project_associations'] == 1
    assert row['association_saving_fraction'] == 1 / 3


def test_cgt_eligibility_experiment_has_negatives_not_only_synonyms():
    report = e4()
    assert report['literal_gate']['false_negative'] > 0
    assert report['controlled_draft']['false_negative'] == 0
    assert report['controlled_draft']['false_positive'] == 0
    assert report['controlled_draft']['positive_cases'] < report['controlled_draft']['cases']


def test_original_fixture_audit_retains_denominator():
    from pathlib import Path
    row = fixture_audit(Path(__file__).parent / 'fixtures/studio_eval/competition.json')
    assert row['cases'] == 140
    assert row['gold_supported'] > 0
    assert row['supported_equal_use'] == row['gold_supported']


def test_business_replay_blocks_without_saved_response_and_preserves_source(tmp_path):
    from test_studio_agent_instance import make_service
    from test_studio_evidence_versions import sample

    from scripts.claude_review_offline_probe import business_replay

    path = tmp_path / 'baseline.sqlite3'
    service, pid = make_service(path)
    service.store.upsert_project(pid, {}, consent=True)
    service.objects.ensure_subject(pid)
    service.evidence.save(sample(pid, 'Synthetic Optics product text').model_copy(
        update={'visibility': 'public', 'source_key': 'public-profile:synthetic'}))
    service.store.enqueue(pid, 'business')
    output = tmp_path / 'private-output'
    output.mkdir()
    result = business_replay(path, output)
    assert result['missing_saved_response_boundaries'] == 1 and result['socket_attempts'] == 0
    assert result['outcomes'][0]['status'] == 'blocked_missing_saved_model_response'
    assert result['counts_after']['studio_business_profiles'] == 0
    with service.store._connect() as db:
        assert db.execute("SELECT status FROM studio_jobs WHERE kind='business'").fetchone()[0] == 'queued'
