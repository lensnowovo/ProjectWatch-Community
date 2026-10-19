"""Zero-network review experiments. Private outputs must stay outside Git.

E0 uses collector *proxies*: legacy items do not retain a collector ID. E1
reports exact URLs and opaque journal keys, not semantic query similarity.
E4 is a small authored CGT eligibility experiment, not a production classifier
or an independently validated clinical/business taxonomy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

BODY = {'article_body', 'partial_body', 'twin_body', 'filing_text'}
VOCAB = {
    'use_case': {
        'cd19-b-all': {'CD19阳性B-ALL治疗', 'CD19阳性B细胞急性淋巴细胞白血病治疗'},
        'bcma-mm': {'BCMA阳性多发性骨髓瘤治疗', 'BCMA靶向多发性骨髓瘤治疗'},
        'cg-manufacturing': {'细胞治疗生产工艺开发', '细胞治疗制造工艺开发'},
    },
    'buyer_type': {
        'hospital': {'医院', '医疗机构'},
        'biopharma': {'生物制药企业', '生物医药企业'},
    },
    'delivery': {
        'autologous-car-t': {'自体CAR-T细胞制剂', '患者自体CAR-T制剂'},
        'allogeneic-car-t': {'异体CAR-T细胞制剂', '供者来源CAR-T制剂'},
        'process-service': {'工艺开发服务', '生产工艺服务'},
    },
}


def normal(value):
    return ' '.join(str(value).split()).casefold()


def collector_proxy(row):
    extra = json.loads(row['extra'] or '{}')
    host = (urlsplit(row['url']).hostname or '').lower()
    if host == 'news.google.com':
        return 'google_news_url'
    if host == 'static.cninfo.com.cn' and 'sec_code' in extra:
        return 'cninfo_metadata'
    if row['category'] == 'paper' and 'cited_by' in extra:
        return 'openalex_metadata'
    if extra.get('search_providers'):
        return 'web_search:' + '+'.join(sorted(extra['search_providers']))
    if host == 'patents.google.com':
        return 'patent_url'
    if host == 'clinicaltrials.gov':
        return 'clinicaltrials_url'
    return 'unattributed'  # Publisher name is not a collector identity.


def e0(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[collector_proxy(row)].append(row)
    out = {}
    for key, group in sorted(groups.items()):
        successes = sum(r['body_status'] in BODY and bool(r['body']) for r in group)
        direct = sum(r['body_status'] in {'article_body', 'partial_body', 'filing_text'} and bool(r['body']) for r in group)
        attempted = sum(r['body_status'] in {'article_body', 'partial_body', 'filing_text', 'unavailable'} for r in group)
        out[key] = {'items': len(group), 'statuses': dict(Counter(r['body_status'] or 'unset' for r in group)),
                    'usable_body': successes, 'body_coverage': successes / len(group),
                    'direct_read_success': direct, 'observed_direct_outcomes': attempted,
                    'direct_success_rate': direct / attempted if attempted else None,
                    'notable_major_items': sum(r['relevant'] and r['importance'] in {'major', 'notable'} for r in group),
                    'notable_major_with_body': sum(r['relevant'] and r['importance'] in {'major', 'notable'}
                                                 and r['body_status'] in BODY and bool(r['body']) for r in group)}
    return {'by_collector_proxy': out, 'notice': 'Coverage includes unread/skipped items; direct outcome rate excludes those. '
            'Legacy collector attribution and failed-dispatch completeness are not independently recoverable.'}


def reuse(rows, value_key, project_key='project_id'):
    bindings = defaultdict(set)
    occurrences = Counter()
    for row in rows:
        value = row[value_key]
        if not value:
            continue
        bindings[value].add(row[project_key])
        occurrences[value] += 1
    cross = {k: v for k, v in bindings.items() if len(v) > 1}
    total = sum(occurrences.values())
    # Saving one fetch per additional project is a distinct denominator from
    # the share of all item rows whose URLs happen to be shared.
    saving = sum(len(v) - 1 for v in cross.values())
    associations = sum(len(v) for v in bindings.values())
    return {'occurrences': total, 'unique_keys': len(bindings), 'project_key_associations': associations,
            'cross_project_keys': len(cross), 'shared_occurrences': sum(occurrences[k] for k in cross),
            'shared_occurrence_fraction': sum(occurrences[k] for k in cross) / total if total else None,
            'avoidable_cross_project_associations': saving,
            'association_saving_fraction': saving / associations if associations else None}


def audit(db):
    items = [dict(r) for r in db.execute('SELECT project_id,url,category,extra,body_status,body,relevant,importance FROM studio_items')]
    calls = [dict(r) for r in db.execute("SELECT j.project_id,c.call_key,c.status FROM studio_job_calls c "
                                       "JOIN studio_jobs j ON j.id=c.job_id WHERE c.kind='search'")]
    tables = ('studio_items', 'studio_events', 'studio_evidence_versions', 'studio_monitor_objects',
              'studio_fact_records', 'studio_business_profiles', 'studio_competition_run_history')
    return {'counts': {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0] for t in tables},
            'E0': e0(items), 'E1': {'exact_url': reuse(items, 'url'), 'opaque_search_key': reuse(calls, 'call_key'),
                                  'completed_search_key': reuse([c for c in calls if c['status'] == 'completed'], 'call_key'),
                                  'notice': 'Keys include request parameters and headers; query strings are not retained. '
                                  'No semantic query/content duplication or future CGT reuse claim.'},
            'jobs': [dict(r) for r in db.execute('SELECT kind,status,count(*) AS count FROM studio_jobs GROUP BY kind,status')],
            'job_call_kinds': [dict(r) for r in db.execute('SELECT j.kind,c.kind AS call_kind,c.status,count(*) AS count '
                'FROM studio_job_calls c JOIN studio_jobs j ON j.id=c.job_id GROUP BY j.kind,c.kind,c.status')]}


def cgt_cases():
    """Authored paraphrases, hard negatives and multi-use cases; no real company data."""
    def side(company, values, *, family, scope='full', basis='actual'):
        product = company + '合成产品'
        quote = company + '的' + product + '：' + '；'.join(v for vs in values.values() for v in vs)
        return {'company': company, 'product': product, 'quote': quote, 'facts': values,
                'family': family, 'read_scope': scope, 'basis': basis}

    left = {'use_case': ['CD19阳性B-ALL治疗'], 'buyer_type': ['医院'], 'delivery': ['自体CAR-T细胞制剂']}
    same = {k: list(v) for k, v in left.items()}
    synonym = {'use_case': ['CD19阳性B细胞急性淋巴细胞白血病治疗'], 'buyer_type': ['医疗机构'],
               'delivery': ['患者自体CAR-T制剂']}
    cases = []
    def add(name, right, expected, *, relation='direct', left_values=None, **kwargs):
        cases.append({'id': name, 'relation': relation, 'expected': expected,
                      'sides': [side('合成甲', left_values or left, family='a'), side('合成乙', right, family='b', **kwargs)]})
    add('exact', same, True)
    add('all-paraphrases', synonym, True)
    for dim in left:
        add('paraphrase-' + dim, {**same, dim: synonym[dim]}, True)
    add('multiple-uses', same, True, left_values={**left, 'use_case': [*left['use_case'], 'BCMA阳性多发性骨髓瘤治疗']})
    add('multiple-buyers', same, True, left_values={**left, 'buyer_type': ['医院', '生物制药企业']})
    add('different-indication', {**same, 'use_case': ['BCMA靶向多发性骨髓瘤治疗']}, False)
    add('different-buyer', {**same, 'buyer_type': ['生物制药企业']}, False)
    add('different-delivery-direct', {**same, 'delivery': ['异体CAR-T细胞制剂']}, False)
    add('different-delivery-substitute', {**synonym, 'delivery': ['供者来源CAR-T制剂']}, True, relation='substitute')
    add('unknown-generic', {**same, 'use_case': ['细胞治疗']}, False)
    add('summary', synonym, False, scope='summary')
    add('forecast', synonym, False, basis='forecast')
    add('missing-buyer', {**same, 'buyer_type': []}, False)
    add('process-not-therapy', {'use_case': ['细胞治疗制造工艺开发'], 'buyer_type': ['生物医药企业'],
                               'delivery': ['生产工艺服务']}, False)
    case = json.loads(json.dumps(cases[0]))
    case.update(id='shared-family', expected=False)
    case['sides'][1]['family'] = 'a'
    cases.append(case)
    return cases


def eligible(case, *, controlled):
    sides = case['sides']
    if sides[0]['family'] == sides[1]['family']:
        return False
    groups = []
    for side in sides:
        if side['read_scope'] == 'summary' or side['basis'] != 'actual':
            return False
        if not all(side[k] in side['quote'] for k in ('company', 'product')):
            return False
        mapped = {}
        for dim in VOCAB:
            values = side['facts'].get(dim, [])
            if not values or any(v not in side['quote'] for v in values):
                return False
            if controlled:
                codes = {code for v in values for code, aliases in VOCAB[dim].items() if normal(v) in {normal(a) for a in aliases}}
                # An unmapped additional use is not silently discarded.
                if any(not any(normal(v) in {normal(a) for a in aliases} for aliases in VOCAB[dim].values()) for v in values):
                    return False
            else:
                codes = {normal(v) for v in values}
                if len(codes) != 1:
                    return False
            mapped[dim] = codes
        groups.append(mapped)
    if not all(groups[0][d] & groups[1][d] for d in ('use_case', 'buyer_type')):
        return False
    delivery_overlap = bool(groups[0]['delivery'] & groups[1]['delivery'])
    return delivery_overlap if case['relation'] == 'direct' else not delivery_overlap


def e4():
    cases = cgt_cases()
    report = {}
    for label, controlled in [('literal_gate', False), ('controlled_draft', True)]:
        rows = [{ 'id': c['id'], 'expected': c['expected'], 'eligible': eligible(c, controlled=controlled)} for c in cases]
        report[label] = {'cases': len(rows), 'positive_cases': sum(r['expected'] for r in rows),
                         'true_positive': sum(r['expected'] and r['eligible'] for r in rows),
                         'false_positive': sum(not r['expected'] and r['eligible'] for r in rows),
                         'false_negative': sum(r['expected'] and not r['eligible'] for r in rows), 'rows': rows}
    report['notice'] = 'Authored eligibility test only; no clinical truth, synonym-model accuracy, '
    report['notice'] += 'semantic reviewer or real CGT recall measured. Production gates are unchanged.'
    return report


def fixture_audit(path):
    cases = json.loads(Path(path).read_text(encoding='utf-8'))['cases']
    supported = [c for c in cases if c['gold']['support'] == 'supported']
    def values(side, dim):
        return {normal(f['value']) for f in side['facts'] if f['predicate'] == dim}
    def raw_values(side, dim):
        return {f['value'] for f in side['facts'] if f['predicate'] == dim}
    return {'cases': len(cases), 'gold_supported': len(supported),
            'supported_equal_use': sum(values(c['sides'][0], 'use_case') == values(c['sides'][1], 'use_case') for c in supported),
            'supported_equal_buyer': sum(values(c['sides'][0], 'buyer_type') == values(c['sides'][1], 'buyer_type') for c in supported),
            'supported_equal_delivery': sum(values(c['sides'][0], 'delivery') == values(c['sides'][1], 'delivery') for c in supported),
            'supported_raw_equal_use': sum(raw_values(c['sides'][0], 'use_case') == raw_values(c['sides'][1], 'use_case') for c in supported),
            'supported_raw_equal_buyer': sum(raw_values(c['sides'][0], 'buyer_type') == raw_values(c['sides'][1], 'buyer_type') for c in supported)}


def business_replay(frozen, output):
    """Run queued business dispatches up to the missing-response boundary.

    There are no saved business model replies in this baseline. Never substitute
    a made-up products=[] success for a real extraction. This is a blocked replay,
    not proof that a completed model job would have no products.
    """
    import socket
    from types import SimpleNamespace
    from unittest.mock import patch

    from project_watch.repository import ProjectRepository
    from project_watch.studio.competition_research import research_inputs
    from project_watch.studio.pipeline import Pipeline
    from project_watch.studio.settings import SettingsStore
    from project_watch.studio.store import StudioStore

    path = output / 'business-replay.sqlite3'
    with closing(sqlite3.connect(frozen.as_uri() + '?mode=ro', uri=True)) as original, closing(sqlite3.connect(path)) as copy:
        original.backup(copy)
    socket_attempts, missing_responses = [], []
    class MissingSavedResponse(RuntimeError):
        pass
    def deny_socket(*args, **kwargs):
        socket_attempts.append(True)
        raise RuntimeError('offline socket blocked')
    def no_model(*args, **kwargs):
        missing_responses.append(True)
        raise MissingSavedResponse('No business model response exists in frozen journal')
    with patch.object(socket.socket, 'connect', deny_socket), patch.object(socket.socket, 'connect_ex', deny_socket):
        store = StudioStore(path)
        pipeline = Pipeline(ProjectRepository(path), store, SettingsStore(output / 'empty-settings.json'))
        binding = 'offline-public-only-no-provider'
        pipeline.model_binding = lambda: binding
        pipeline.model_view = lambda llm=None: SimpleNamespace(binding=binding)
        pipeline.tools = lambda: (None, None, SimpleNamespace(ready=True, json=no_model), None)
        with closing(store._connect()) as db, db:
            # Only the disposable replay copy is altered; no scheduler/worker.
            db.execute("UPDATE studio_jobs SET status='paused' WHERE status='queued' AND kind!='business'")
        input_rows = []
        with closing(store._connect()) as db:
            projects = [r[0] for r in db.execute('SELECT project_id FROM studio_projects')]
        for pid in projects:
            inputs = pipeline.business_profiles.inputs(pid, binding=binding)
            parts = inputs['parts'] if inputs else []
            queries = research_inputs(pipeline, pid)
            input_rows.append({'project_id': pid, 'input_parts': len(parts),
                               'read_scopes': dict(Counter(p['read_scope'] for p in parts)),
                               'input_characters': sum(len(p['text']) for p in parts),
                               'competition_query_count': len(queries['queries'])})
        outcomes = []
        while job := store.claim():
            if job['kind'] != 'business':
                raise RuntimeError('unexpected replay job')
            try:
                pipeline.run(job)
            except MissingSavedResponse:
                outcomes.append({'project_id': job['project_id'], 'status': 'blocked_missing_saved_model_response'})
                store.finish(job['id'], error='offline replay: missing saved model response', lease_token=job['lease_token'])
            else:
                outcomes.append({'project_id': job['project_id'], 'status': 'returned_without_model'})
                store.finish(job['id'], lease_token=job['lease_token'])
        with closing(store._connect()) as db:
            counts = {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                      for t in ('studio_monitor_objects', 'studio_fact_records', 'studio_business_profiles')}
    return {'inputs': input_rows, 'outcomes': outcomes, 'counts_after': counts,
            'missing_saved_response_boundaries': len(missing_responses), 'socket_attempts': len(socket_attempts),
            'notice': 'No completed business extraction was replayable. Remaining objects are not evidence of '
                      'post-completion failure. Real CGT material is absent; E4 live extraction remains unmeasured.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if output.is_relative_to(root) or output.exists():
        raise ValueError('Use a NEW private output directory outside this checkout')
    source = args.source.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    frozen = output / 'frozen.sqlite3'
    # SQLite online backup is consistent, including WAL; copying just .sqlite3
    # would risk a mismatched checkpoint. Source is always opened read-only.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original, closing(sqlite3.connect(frozen)) as copy:
        original.backup(copy)
    before = hashlib.sha256(frozen.read_bytes()).hexdigest()
    with closing(sqlite3.connect(frozen.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        report = audit(db)
    report.update(business_replay=business_replay(frozen, output),
                  E4=e4(), fixture=fixture_audit(root / 'tests/fixtures/studio_eval/competition.json'),
                  freeze_sha256=before, frozen_unchanged=before == hashlib.sha256(frozen.read_bytes()).hexdigest(),
                  actual_calls={'search': 0, 'commercial': 0, 'model': 0}, workers_enabled=False)
    (output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(output), 'counts': report['counts'], 'frozen_unchanged': report['frozen_unchanged'],
                      'actual_calls': report['actual_calls']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
