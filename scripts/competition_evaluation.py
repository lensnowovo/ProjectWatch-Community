"""Opt-in synthetic bilateral replay, with fixed gold and failed-call denominators.

Offline review responses are injected test doubles, NOT measured model accuracy.
An optional reviewer runs the production semantic prompt, not discovery/extraction.
No private configuration or materials are loaded by this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from project_watch.repository import ProjectRepository
from project_watch.studio.application import StudioApplication
from project_watch.studio.competition import assess_relation
from project_watch.studio.monitoring_contracts import (
    EvidenceInput,
    EvidencePart,
    FactInput,
    LocatedRef,
    ObjectInput,
)
from project_watch.studio.store import StudioStore


def load_cases(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    cases = data['cases']
    if not cases or len(cases) > 1000 or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('evaluation requires 1..1000 unique cases')
    for case in cases:
        if len(case['sides']) != 2 or case['gold']['classification'] not in ('direct', 'substitute', 'unknown'):
            raise ValueError('invalid bilateral gold')
    return cases


def prepare_case(service, repository, case):
    pid = repository.create_project(case['id']).id
    service.store.upsert_project(pid, {'legal_name': case['sides'][0]['company']}, consent=False)
    companies, products = [], []
    for index, side in enumerate(case['sides']):
        evidence = service.evidence.save(EvidenceInput(project_id=pid, source_key=f'{pid}:side:{index}',
            visibility='public', source_type='public_page', read_scope=side['read_scope'], origin='company_self',
            propagation_family=side['propagation_family'], extraction_version='synthetic-fixed-v1',
            parts=(EvidencePart(locator_kind='stored_text', locator='synthetic original passage', text=side['quote']),)))
        ref = LocatedRef(evidence_version_id=evidence['id'], part_index=0, start=0, end=len(side['quote']), quote=side['quote'])
        company = service.objects.save(pid, ObjectInput(kind='company', name=side['company'], visibility='public', refs=(ref,)), actor='user')
        product = service.objects.save(pid, ObjectInput(kind='product', name=side['product'], parent_id=company['id'], visibility='public', refs=(ref,)), actor='user')
        for fact in side['facts']:
            service.objects.save_fact(pid, FactInput(subject_id=product['id'], predicate=fact['predicate'], value=fact['value'],
                text=fact['value'], basis=side['basis'], subject_attribution='explicit', visibility='public', refs=(ref,)))
        if side['withdraw']:
            service.evidence.dependencies.withdraw(pid, evidence['id'], 'synthetic withdrawal', actor='synthetic')
        companies.append(company['id'])
        products.append(product['id'])
    return pid, {'subject_id': companies[0], 'peer_id': companies[1],
                 'subject_product_id': products[0], 'peer_product_id': products[1]}


def score(rows):
    total = len(rows)
    metrics = {'case_runs': total, 'correct': sum(r['correct'] for r in rows),
        'failed_calls': sum(r['failed_calls'] for r in rows), 'failed_cases': sum(r['failed_case'] for r in rows),
        'review_attempts': sum(r['review_attempts'] for r in rows),
        'injected_failures': sum(r['injected_failures'] for r in rows),
        'hard_negative_promotions': sum(r['gold_class'] == 'unknown' and r['classification'] != 'unknown' for r in rows),
        'unknown_outputs': sum(r['classification'] == 'unknown' for r in rows),
        'refs_checked': sum(r['refs_checked'] for r in rows), 'invalid_refs': sum(r['invalid_refs'] for r in rows)}
    metrics['accuracy'] = metrics['correct'] / total if total else None
    metrics['unknown_output_rate'] = metrics['unknown_outputs'] / total if total else None
    for kind in ('direct', 'substitute'):
        predicted = sum(r['classification'] == kind for r in rows)
        gold = sum(r['gold_class'] == kind for r in rows)
        correct = sum(r['classification'] == r['gold_class'] == kind for r in rows)
        metrics[kind] = {'predicted': predicted, 'gold': gold, 'correct': correct,
                        'precision': correct / predicted if predicted else None, 'recall': correct / gold if gold else None}
    return metrics


def run(cases, workdir, *, repeat=3, reviewer=None):
    if not 1 <= repeat <= 10:
        raise ValueError('repeat must be 1..10')
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for round_id in range(1, repeat+1):
        # A fresh, explicitly isolated DB; never open an existing/private instance.
        path = workdir / f'competition-replay-{round_id}.sqlite3'
        if path.exists():
            raise ValueError('evaluation work directory must not contain an existing replay database')
        repository = ProjectRepository(path)
        service = StudioApplication(StudioStore(path))
        for case in cases:
            attempts = failures = injected = 0

            def review(context, case=case):
                nonlocal attempts, failures, injected
                attempts += 1
                if reviewer is not None:
                    try:
                        return reviewer(context)
                    except Exception:
                        failures += 1
                        raise
                if case['review'] == 'failed':
                    injected += 1
                    raise RuntimeError('synthetic provider failure')
                return {'verdict': 'disputed' if case['review'] == 'disputed' else 'supported',
                        'reason': 'Fixed synthetic transport; not model evaluation',
                        'fact_ids': ['foreign-fact'] if case['review'] == 'foreign' else [f['id'] for f in context['facts']]}

            row = {'id': case['id'], 'project': case['project'], 'sector': case['sector'], 'round': round_id,
                   'gold_class': case['gold']['classification'], 'classification': 'unknown', 'support': 'pending',
                   'correct': False, 'failed_case': False, 'refs_checked': 0, 'invalid_refs': 0}
            try:
                pid, scope = prepare_case(service, repository, case)
                result = assess_relation(service, pid, scope, case['relation_type'], review=review, include_internal=False)
                row.update(support=result['support'], missing=result['missing'])
                if result['support'] == 'supported':
                    row['classification'] = result['relation_type']
                row['correct'] = (row['support'] == case['gold']['support'] and row['classification'] == row['gold_class'])
                for fact_id in result['fact_ids']:
                    fact = next(f for product_id in (scope['subject_product_id'], scope['peer_product_id'])
                                for f in service.objects.facts(pid, product_id) if f['id'] == fact_id)
                    for ref in fact['refs']:
                        row['refs_checked'] += 1
                        try:
                            service.evidence.resolve(pid, LocatedRef.model_validate(ref), include_internal=False)
                        except (ValueError, KeyError):
                            row['invalid_refs'] += 1
                row['correct'] = row['correct'] and not row['invalid_refs'] and not failures
            except Exception as exc:
                # Never persist raw provider errors/headers/materials.
                row.update(failed_case=True, error_class=type(exc).__name__)
            row.update(review_attempts=attempts, failed_calls=failures, injected_failures=injected)
            rows.append(row)
    return {'schema_version': 1, 'mode': 'synthetic_semantic_review_only' if reviewer else 'offline_injected_gate_replay',
            'notice': 'Not general model accuracy or independent business verification; gold is an authored engineering expectation.',
            'case_count': len(cases), 'project_count': len({c['project'] for c in cases}),
            'sector_count': len({c['sector'] for c in cases}), 'repeat': repeat, 'summary': score(rows),
            'per_sector': {s: score([r for r in rows if r['sector'] == s]) for s in sorted({r['sector'] for r in rows})},
            'rounds': {str(n): score([r for r in rows if r['round'] == n]) for n in range(1, repeat+1)},
            'unmeasured': ['discovery_recall', 'business_extraction_accuracy', 'event_attribution_accuracy',
                           'field_error_rate', 'real_search_calls', 'actual_provider_cost', 'real_business_truth'],
            'rows': rows}
