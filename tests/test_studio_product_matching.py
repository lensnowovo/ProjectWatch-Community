"""Synthetic product matching journeys; not live CGT or medical truth acceptance."""

import pytest
from test_studio_agent_instance import make_service
from test_studio_bilateral_competition import reviewer
from test_studio_evidence_versions import sample

from project_watch.studio.competition import assess_relation
from project_watch.studio.monitoring_contracts import FactInput, LocatedRef, ObjectInput
from scripts.claude_review_offline_probe import cgt_cases


def product(service, pid, company, name, groups, *, source="source", visibility="public",
            read_scope="partial", family=None, basis="actual"):
    text = company["name"] + " builds " + name + ": " + "; ".join(
        value for values in groups.values() for value in values
    )
    evidence = service.evidence.save(sample(pid, text).model_copy(update={
        "source_key": source, "visibility": visibility, "read_scope": read_scope,
        "propagation_family": family or "",
    }))
    ref = LocatedRef(evidence_version_id=evidence["id"], part_index=0, start=0,
                     end=len(text), quote=text)
    # Keep exact ownership/identity excerpts while adding later products.
    item = ObjectInput.model_validate({k: company[k] for k in ObjectInput.model_fields})
    item = item.model_copy(update={"refs": (*item.refs, ref)})
    company = service.objects.save(pid, item, object_id=company["id"],
        expected_version=company["version"], actor="user")
    obj = service.objects.save(pid, ObjectInput(kind="product", name=name,
        parent_id=company["id"], visibility=visibility, refs=(ref,)), actor="user")
    rows = []
    for predicate, values in groups.items():
        for value in values:
            rows.append(service.objects.save_fact(pid, FactInput(subject_id=obj["id"],
                predicate=predicate, value=value, text=value, basis=basis,
                subject_attribution="explicit", visibility=visibility, refs=(ref,))))
    return company, obj, rows


LEFT = {"use_case": ["CD19阳性B-ALL治疗"], "buyer_type": ["医院"],
        "delivery": ["自体CAR-T细胞制剂"]}
PARAPHRASE = {"use_case": ["CD19阳性B细胞急性淋巴细胞白血病治疗"],
              "buyer_type": ["医疗机构"], "delivery": ["患者自体CAR-T制剂"]}


def scoped_reviewer(context):
    return {**reviewer(context), "matched_fact_ids": context['matching']['suggested_matches'][0]}


def test_cited_use_without_buyer_starts_discovery_not_supported_competition(tmp_path):
    from project_watch.studio.competition_research import research_inputs

    service, pid = make_service(tmp_path / 'use-only.sqlite3')
    company = service.objects.ensure_subject(pid)
    company, obj, _ = product(service, pid, company, 'Synthetic A',
        {'use_case': ['drug screening']}, source='use-only')
    inputs = research_inputs(service, pid)
    assert inputs['products'] == [obj['id']]
    assert inputs['queries'] == ['Synthetic A drug screening 竞品', 'drug screening 竞品', 'Synthetic Optics 竞争对手']
    assert inputs['query_contexts'][0]['buyers'] == []
    assert inputs['query_contexts'][0]['discovery_basis'] == 'product_use_only'
    peer = service.objects.save(pid, ObjectInput(kind='company', name='Synthetic Beta',
        visibility='public'), actor='user')
    _, other, _ = product(service, pid, peer, 'Synthetic B', LEFT, source='peer')
    result = assess_relation(service, pid, {'subject_id': company['id'], 'peer_id': peer['id'],
        'subject_product_id': obj['id'], 'peer_product_id': other['id']}, 'direct',
        review=scoped_reviewer, include_internal=False)
    assert result['support'] != 'supported' and 'buyer_overlap' in result['missing']


@pytest.mark.parametrize('groups,scope,basis', [
    ({'buyer_type': ['医院']}, 'partial', 'actual'),
    ({'delivery': ['software']}, 'partial', 'actual'),
    ({'use_case': ['drug screening']}, 'summary', 'actual'),
    ({'use_case': ['drug screening']}, 'partial', 'forecast'),
])
def test_incomplete_discovery_still_requires_actual_original_product_use(tmp_path, groups, scope, basis):
    from project_watch.studio.competition_research import research_inputs

    service, pid = make_service(tmp_path / 'not-a-seed.sqlite3')
    company = service.objects.ensure_subject(pid)
    product(service, pid, company, 'Synthetic A', groups, read_scope=scope, basis=basis)
    inputs = research_inputs(service, pid)
    # No product-use seed; since research-v24 only unverified company-lead queries
    # (product/company names as retrieval hints) may exist, never a use context.
    assert inputs['query_contexts'] == [] and inputs['mode'] in ('company_hint', 'none')
    assert all('drug screening' not in q for q in inputs['queries'])


def pair(tmp_path, left=None, right=None):
    service, pid = make_service(tmp_path / "matching.sqlite3")
    service.relationships.assessment_validator = assess_relation
    company = service.objects.ensure_subject(pid)
    peer = service.objects.save(pid, ObjectInput(kind="company", name="Synthetic Beta",
        visibility="public"), actor="user")
    company, a, af = product(service, pid, company, "Synthetic A", left or LEFT, source="left")
    peer, b, bf = product(service, pid, peer, "Synthetic B", right or PARAPHRASE, source="right")
    return service, pid, {"subject_id": company["id"], "peer_id": peer["id"],
        "subject_product_id": a["id"], "peer_product_id": b["id"]}, af + bf


def test_cgt_aliases_match_without_rewriting_raw_assertions(tmp_path):
    service, pid, scope, rows = pair(tmp_path)
    seen = []
    result = assess_relation(service, pid, scope, "direct",
        review=lambda context: seen.append(context) or reviewer(context), include_internal=False)
    assert result["support"] == "supported"
    assert result["matching"]["version"] == "product-matching-v1"
    assert result["matching"]["controlled_terms"]
    assert {f["value"] for f in seen[0]["facts"]} == {f["value"] for f in rows}
    assert seen[0]["matching"] == result["matching"]
    saved = service.relationships.save_assessment(pid, result)
    assert saved["assessment_version"] == "bilateral-v2"
    assert saved["matching"] == result["matching"]


@pytest.mark.parametrize("right,relation,missing", [
    ({**PARAPHRASE, "use_case": ["BCMA靶向多发性骨髓瘤治疗"]}, "direct", "use_case_overlap"),
    ({**PARAPHRASE, "use_case": ["B细胞急性淋巴细胞白血病治疗"]}, "direct", "use_case_overlap"),
    ({**PARAPHRASE, "use_case": ["细胞治疗"]}, "direct", "use_case_overlap"),
    ({**PARAPHRASE, "buyer_type": ["生物制药企业"]}, "direct", "buyer_overlap"),
    ({**PARAPHRASE, "delivery": ["供者来源CAR-T制剂"]}, "direct", "compatible_delivery"),
    (PARAPHRASE, "substitute", "distinct_delivery"),
])
def test_nearby_terms_do_not_become_same_product(tmp_path, right, relation, missing):
    service, pid, scope, _ = pair(tmp_path, right=right)
    def forbidden(_):
        raise AssertionError("ineligible comparison must not call a paid reviewer")
    result = assess_relation(service, pid, scope, relation, review=forbidden)
    assert result["support"] == "pending" and missing in result["missing"]


def test_substitute_retains_distinct_cgt_delivery(tmp_path):
    service, pid, scope, _ = pair(tmp_path,
        right={**PARAPHRASE, "delivery": ["供者来源CAR-T制剂"]})
    assert assess_relation(service, pid, scope, "substitute", review=reviewer)["support"] == "supported"


def test_multiple_uses_buyers_and_unmapped_terms_are_retained(tmp_path):
    left = {**LEFT, "use_case": [*LEFT["use_case"], "BCMA阳性多发性骨髓瘤治疗", "合成未映射用途"],
            "buyer_type": ["医院", "生物制药企业"]}
    service, pid, scope, rows = pair(tmp_path, left=left)
    result = assess_relation(service, pid, scope, "direct", review=scoped_reviewer)
    assert result["support"] == "supported"
    assert set(result["fact_ids"]) == {f["id"] for f in rows}
    assert any(t["value"] == "合成未映射用途" and t["mode"] == "literal"
               for t in result["matching"]["terms"])
    from project_watch.studio.competition_research import research_inputs
    inputs = research_inputs(service, pid)
    assert scope["subject_product_id"] in inputs["products"]
    assert inputs["queries"] and len(inputs["queries"]) <= 3  # +company query (v28)
    assert "合成未映射用途" in str(inputs["query_contexts"])


@pytest.mark.parametrize("mode", ["omitted", "foreign", "all-values", "one-sided"])
def test_multi_value_review_requires_one_explicit_bilateral_match(tmp_path, mode):
    service, pid, scope, rows = pair(tmp_path,
        left={**LEFT, "buyer_type": ["医院", "生物制药企业"]})
    def invalid(context):
        answer = scoped_reviewer(context)
        if mode == "omitted":
            answer.pop("matched_fact_ids")
        elif mode == "foreign":
            answer['matched_fact_ids'] = ['foreign']
        elif mode == "all-values":
            answer['matched_fact_ids'] = [f['id'] for f in rows]
        else:
            answer['matched_fact_ids'] = [f['id'] for f in rows
                if f['subject_id'] == scope['subject_product_id']]
        return answer
    result = assess_relation(service, pid, scope, "direct", review=invalid)
    assert result["support"] == "pending" and "valid_semantic_review" in result['missing']


def test_business_extraction_retains_multiple_contexts_for_same_product(tmp_path):
    from project_watch.studio.business_profile import BusinessProfiles
    from project_watch.studio.competition_research import research_inputs
    service, pid = make_service(tmp_path / "business-multi.sqlite3")
    company = service.objects.ensure_subject(pid)
    statements = [
        f"{company['name']}的合成产品用于CD19阳性B-ALL治疗，买方是医院，交付自体CAR-T细胞制剂。",
        f"{company['name']}的合成产品用于BCMA阳性多发性骨髓瘤治疗，买方是医疗机构，交付患者自体CAR-T制剂。",
    ]
    service.evidence.save(sample(pid, '\n'.join(statements)).model_copy(update={
        'source_key': 'public-profile:multi', 'visibility': 'public'}))
    business = BusinessProfiles(service.store, service.objects)
    inputs = business.inputs(pid, binding='synthetic')
    rows = [{"company": company['name'], "name": "合成产品", "source": "B1", "quote": quote,
        "use_case": use, "buyer_type": buyer, "delivery": delivery, "basis": "actual", "polarity": "asserted"}
        for quote, use, buyer, delivery in zip(statements,
            ["CD19阳性B-ALL治疗", "BCMA阳性多发性骨髓瘤治疗"], ["医院", "医疗机构"],
            ["自体CAR-T细胞制剂", "患者自体CAR-T制剂"], strict=True)]
    result = business.apply(pid, inputs, {"products": rows})
    assert result['accepted'] == 2 and len(result['product_ids']) == 1
    facts = service.objects.facts(pid, result['product_ids'][0])
    assert len(facts) == 6 and all(f['dependency']['status'] == 'current' for f in facts)
    seeds = research_inputs(service, pid)
    assert len(seeds['queries']) == 3 and len(seeds['products']) == 1  # +company query (v28)
    assert not any('CD19' in q and '医疗机构' in q for q in seeds['queries'])


def test_multiple_contexts_cannot_cross_use_with_other_buyer(tmp_path):
    from project_watch.studio.competition_research import research_inputs
    service, pid, scope, _ = pair(tmp_path,
        left={**LEFT, "buyer_type": ["生物制药企业"]})
    # The same product ALSO serves hospitals, but only for this different use.
    text = "Synthetic A：BCMA阳性多发性骨髓瘤治疗；医院；自体CAR-T细胞制剂"
    ev = service.evidence.save(sample(pid, text).model_copy(update={
        "source_key": "other-use", "visibility": "public"}))
    ref = LocatedRef(evidence_version_id=ev["id"], part_index=0, start=0, end=len(text), quote=text)
    for predicate, value in {"use_case": "BCMA阳性多发性骨髓瘤治疗", "buyer_type": "医院",
                              "delivery": "自体CAR-T细胞制剂"}.items():
        service.objects.save_fact(pid, FactInput(subject_id=scope["subject_product_id"],
            predicate=predicate, value=value, text=value, basis="actual", visibility="public",
            subject_attribution="explicit", refs=(ref,)))
    result = assess_relation(service, pid, scope, "direct", review=reviewer)
    assert result["support"] == "pending" and "joint_use_buyer_delivery" in result["missing"]
    inputs = research_inputs(service, pid)
    assert any("CD19" in q and "生物制药企业" in q for q in inputs["queries"])
    assert not any("CD19" in q and "医院" in q for q in inputs["queries"])


def test_withdrawn_source_still_invalidates_synonym_assessment(tmp_path):
    service, pid, scope, rows = pair(tmp_path)
    first = assess_relation(service, pid, scope, "direct", review=reviewer)
    service.relationships.save_assessment(pid, first)
    service.evidence.dependencies.withdraw(pid, rows[0]["refs"][0]["evidence_version_id"],
        "synthetic correction", actor="test")
    assert assess_relation(service, pid, scope, "direct", review=reviewer)["support"] == "pending"
    assert service.relationships.page(pid)[0]["status"] == "stale"


def test_second_target_product_can_be_selected_without_extra_review_budget(tmp_path):
    from project_watch.studio.competition_research import select_product_pairs
    service, pid, scope, _ = pair(tmp_path,
        left={**LEFT, "use_case": ["合成无关用途"]})
    company = service.objects.get(pid, scope["subject_id"])
    _, matching, _ = product(service, pid, company, "Synthetic A2", LEFT, source="left-two")
    selected = select_product_pairs(service, pid, scope["subject_id"], scope["peer_id"],
        [scope["subject_product_id"], matching["id"]], [scope["peer_product_id"]])
    assert len(selected) <= 2 and selected[0]["subject_product_id"] == matching["id"]


def test_leased_research_does_not_lose_seeded_product_when_target_read_returns_only_one(tmp_path):
    import json
    from types import SimpleNamespace

    from project_watch.studio.app import create_studio_app
    from project_watch.studio.http import Http, Response

    app = create_studio_app(tmp_path, start_worker=False)
    pipeline = app.state.studio_pipeline
    pid = pipeline.repository.create_project('Synthetic Optics').id
    pipeline.store.upsert_project(pid, {}, consent=True)
    pipeline.settings_store.update({'model': {'protocol': 'openai', 'base_url': 'https://synthetic.test/v1',
        'model': 'fixture', 'api_key': 'synthetic'}, 'search': {'provider': 'tavily', 'api_key': 'synthetic'}})
    company = pipeline.objects.ensure_subject(pid)
    def proposal(name, item, values):
        quote = f"{name} builds {item}: " + '; '.join(values[d][0] for d in LEFT)
        return {'company': name, 'name': item, 'source': 'B1', 'quote': quote,
                **{d: values[d][0] for d in LEFT}, 'basis': 'actual', 'polarity': 'asserted'}
    unrelated = proposal(company['name'], 'Unrelated', {
        'use_case': ['合成机械加工'], 'buyer_type': ['工业企业'], 'delivery': ['设备']})
    relevant = proposal(company['name'], 'CGT Seed', LEFT)
    peer = proposal('Synthetic Beta', 'CGT Peer', PARAPHRASE)
    seed = pipeline.evidence.save(sample(pid, unrelated['quote'] + '\n' + relevant['quote']).model_copy(
        update={'visibility': 'public', 'source_key': 'public-profile:seed'}))
    inputs = pipeline.business_profiles.inputs(pid, binding=pipeline.model_binding(), evidence_ids=[seed['id']])
    seeded = pipeline.business_profiles.apply(pid, inputs, {'products': [unrelated, relevant]})
    reviews = []
    def transport(method, url, headers, body, timeout):
        if 'clinicaltrials.gov' in url:  # registry stage (research-v19): no studies in this fixture
            return Response(200, {}, b'{"studies": []}')
        data = json.loads(body)
        if 'api.tavily.com' in url:
            is_target = data['query'].startswith(company['name'])
            hit = unrelated if is_target else peer
            result = {'results': [{'title': 'Synthetic product page', 'url':
                'https://target.test/product' if is_target else 'https://peer.test/product', 'content': hit['quote']}]}
        else:
            context = json.loads(data['messages'][1]['content'])
            if 'facts' in context:
                reviews.append(context)
                answer = scoped_reviewer(context)
            elif context['sources'][0]['source'].startswith('D'):
                answer = {'candidates': [{'name': 'Synthetic Beta', 'source': 'D1', 'quote': peer['quote']}]}
            else:
                answer = {'products': [unrelated if context['company'] == company['name'] else peer]}
            result = {'choices': [{'message': {'content': json.dumps(answer)}}]}
        return Response(200, {}, json.dumps(result).encode())
    pipeline.http_factory = lambda _: Http(transport)
    pipeline.article_reader = SimpleNamespace(read=lambda url: {'status': 'article_body',
        'text': unrelated['quote'] if 'target.test' in url else peer['quote']})
    assert pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    pipeline.run(job)
    pipeline.store.finish(job['id'], lease_token=job['lease_token'])
    assert len(reviews) == 1
    assert reviews[0]['scope']['subject_product_id'] in seeded['product_ids']
    assert pipeline.objects.get(pid, reviews[0]['scope']['subject_product_id'])['name'] == 'CGT Seed'
    assert pipeline.competition_research.saved(pid)['supported_count'] == 1
    assert pipeline.schedule_competition(pid) is None


def test_old_checkpoint_is_held_without_replaying_any_provider(tmp_path):
    from test_studio_competition_research import research_pipeline

    from project_watch.studio.execution import JobExecution
    pipeline, pid, calls, reads = research_pipeline(tmp_path)
    pipeline.schedule_competition(pid)
    job = pipeline.store.claim()
    execution = JobExecution(pipeline.store, job)
    execution.checkpoint('competition_inputs', {'queries': ['old'], 'prompt_version': 'old'})
    with pytest.raises(ValueError, match='old matching contract'):
        pipeline.run(job)
    assert calls == [] and reads == []


def test_disputed_review_does_not_get_overridden_by_controlled_matching(tmp_path):
    service, pid, scope, _ = pair(tmp_path)
    result = assess_relation(service, pid, scope, "direct", review=lambda c: {
        **reviewer(c), "verdict": "disputed", "reason": "Synthetic conflicting source wording"})
    assert result["support"] == "disputed"


def test_non_cgt_buyers_are_not_mapped_from_industry_or_name(tmp_path):
    service, pid, scope, _ = pair(tmp_path,
        left={"use_case": ["drug screening"], "buyer_type": ["医院"], "delivery": ["software"]},
        right={"use_case": ["drug screening"], "buyer_type": ["医疗机构"], "delivery": ["software"]})
    result = assess_relation(service, pid, scope, "direct", review=reviewer)
    assert result["support"] == "pending" and "buyer_overlap" in result["missing"]
    assert not result["matching"]["controlled_terms"]


@pytest.mark.parametrize("case", cgt_cases(), ids=lambda case: case['id'])
def test_e4_cases_now_exercise_real_fact_store_and_production_gates(tmp_path, case):
    service, pid = make_service(tmp_path / "e4.sqlite3")
    companies, products = [], []
    for index, side in enumerate(case['sides']):
        company = service.objects.ensure_subject(pid) if index == 0 else service.objects.save(pid,
            ObjectInput(kind="company", name="Synthetic E4 Beta", visibility="public"), actor="user")
        company, obj, _ = product(service, pid, company, "Synthetic E4 " + str(index), side['facts'],
            source=str(index), read_scope=side['read_scope'], family=side['family'], basis=side['basis'])
        companies.append(company)
        products.append(obj)
    scope = {"subject_id": companies[0]['id'], "peer_id": companies[1]['id'],
             "subject_product_id": products[0]['id'], "peer_product_id": products[1]['id']}
    result = assess_relation(service, pid, scope, case['relation'], review=scoped_reviewer)
    assert (result['support'] == 'supported') is case['expected']
