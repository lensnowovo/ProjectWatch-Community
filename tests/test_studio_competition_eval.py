from pathlib import Path

from project_watch.studio import competition_evaluation as ev

CASES = Path(__file__).parent / 'fixtures/studio_eval/competition.json'


def test_fixed_suite_has_multiple_projects_sectors_and_independent_gold():
    cases = ev.load_cases(CASES)
    assert len(cases) >= 50 and len({c['project'] for c in cases}) >= 10 and len({c['sector'] for c in cases}) >= 5
    assert len({c['id'] for c in cases}) == len(cases)
    assert all(c['sides'][0]['company'] in c['sides'][0]['quote'] for c in cases)


def test_all_hard_negatives_and_three_replays_keep_full_denominator(tmp_path):
    report = ev.run(ev.load_cases(CASES), tmp_path, repeat=3)
    assert report['mode'] == 'offline_injected_gate_replay'
    assert report['repeat'] == 3 and report['summary']['case_runs'] == 3 * report['case_count']
    assert report['summary']['hard_negative_promotions'] == 0
    assert report['summary']['correct'] == report['summary']['case_runs']
    assert all(r['case_runs'] > 0 for r in report['per_sector'].values())
    assert report['summary']['injected_failures'] == 30
    assert 'event_attribution_accuracy' in report['unmeasured']


def test_failed_real_reviewer_remains_in_denominator(tmp_path):
    def fail(context):
        raise RuntimeError('synthetic fake transport')
    case = next(c for c in ev.load_cases(CASES) if c['scenario'] == 'direct')
    report = ev.run([case], tmp_path, repeat=3, reviewer=fail)
    assert report['summary']['case_runs'] == 3 and report['summary']['failed_calls'] == 3
    assert report['summary']['direct']['recall'] == 0 and report['summary']['accuracy'] == 0
