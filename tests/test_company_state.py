"""Compiled reading stays a cited interpretation, never a formal observation."""

import copy
import json
from contextlib import closing

import pytest

from project_watch.company_state import (
    READING_RULE_VERSION,
    CompanyState,
    input_packets,
    reading_prompt,
    validate_reading,
)
from project_watch.domain import FragmentInput
from project_watch.reading_jobs import ReadingJobs, ReadingLimits
from project_watch.repository import ProjectRepository


def material(repo, project, text, sha="a" * 64):
    source = repo.add_source(project.id, kind="document", reference="synthetic.txt", content_sha256=sha)
    job = repo.enqueue_intake(project.id, source.id)
    assert repo.claim_next_intake().id == job.id
    repo.complete_intake(job.id, [FragmentInput(1, "page:1", text)])
    return source


def sample(tmp_path):
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic optics")
    source = material(repo, project, "合成公司生产工业相机。2025年收入100万元，属于未审计实际数。")
    packet = input_packets(repo, source)[0]
    output = {"items": [{"topic": "business", "headline": "工业相机业务", "text": "公司生产工业相机。",
                          "evidence": [{"ref_id": packet["fragments"][0]["ref_id"], "quote": "合成公司生产工业相机。"}], "as_of": None}]}
    return repo, project, source, packet, output


def test_all_long_material_segments_retained_without_truncation(tmp_path):
    repo, project, _, _, _ = sample(tmp_path)
    text = "合成设备业务。" * 2200
    source = material(repo, project, text, "b" * 64)
    packets = input_packets(repo, source)
    assert len(packets) >= 2
    assert "".join(f["text"] for p in packets for f in p["fragments"]) == text
    assert all(sum(len(f["text"]) for f in p["fragments"]) <= 9000 for p in packets)
    assert len({f["ref_id"] for p in packets for f in p["fragments"]}) == sum(len(p["fragments"]) for p in packets)


def test_citation_validation_distinguishes_quote_from_interpretation(tmp_path):
    _, _, source, packet, output = sample(tmp_path)
    item = validate_reading(packet, output)["items"][0]
    assert item["origin"] == "ai_synthesis"
    assert item["review_status"] == "unreviewed"
    assert item["source_refs"][0]["version"] == source.content_sha256
    assert item["source_refs"][0]["quote"] == "合成公司生产工业相机。"
    assert item["support_status"] == "quote_verified_not_semantic_verification"


@pytest.mark.parametrize("mutation", ["foreign_ref", "invented_quote", "invented_number", "invented_date", "confirmed"])
def test_unsupported_output_never_publishes(tmp_path, mutation):
    _, _, _, packet, output = sample(tmp_path)
    item = output["items"][0]
    if mutation == "foreign_ref":
        item["evidence"][0]["ref_id"] = "other"
    elif mutation == "invented_quote":
        item["evidence"][0]["quote"] = "公司已经上市。"
    elif mutation == "invented_number":
        item["text"] = "公司收入200万元。"
    elif mutation == "invented_date":
        item["as_of"] = "2026年"
    else:
        item["review_status"] = "confirmed"
    with pytest.raises(ValueError):
        validate_reading(packet, output)


def publish(repo, source, packet, output):
    ledger = ReadingJobs(repo)
    state = CompanyState(repo)
    grant = ledger.authorize(source.project_id, config_id="b" * 64, model="fake", rule_version=READING_RULE_VERSION,
                             source_hashes=[source.content_sha256], include_future=False, limits=ReadingLimits(), actor="synthetic",
                             recipient={"base_url": "https://example.test", "protocol": "anthropic-messages", "model": "fake"})
    from project_watch.company_state import packet_digest
    job = ledger.enqueue(grant["id"], sources={source.id: source.content_sha256}, input_digest=packet_digest(packet))
    token = ledger.claim(job["id"])["lease_token"]
    ledger.before_send(job["id"], token, config_id="b" * 64)(json.dumps({"model": "fake", "stream": True, "max_tokens": 128, "messages": []}).encode())
    state.publish(job["id"], token, packet, output)
    return ledger, state, job


def test_atomic_versioned_publish_retains_human_judgment_and_historical_material(tmp_path):
    repo, project, source, packet, output = sample(tmp_path)
    repo.edit_monitor_profile(project.id, expected_version=0, fields={"thesis": "先核实客户验收。"})
    ledger, state, job = publish(repo, source, packet, output)
    assert ledger.get(job["id"])["state"] == "succeeded"
    first = state.read(project.id)
    assert len(first["batches"]) == 1
    assert first["batches"][0]["items"][0]["text"] == "公司生产工业相机。"
    later_source = material(repo, project, "公司新增光学检测模组业务。", "e" * 64)
    later_packet = input_packets(repo, later_source)[0]
    updated = copy.deepcopy(output)
    updated["items"][0].update(text="新增光学检测模组业务。", evidence=[{"ref_id": later_packet["fragments"][0]["ref_id"], "quote": "公司新增光学检测模组业务。"}])
    publish(repo, later_source, later_packet, updated)
    assert len(state.read(project.id)["batches"]) == 2
    assert repo.monitor_profile_history(project.id)[-1]["fields"]["thesis"] == "先核实客户验收。"
    assert repo.list_observations(project.id) == []
    assert state.read(repo.create_project("Other").id)["batches"] == []


def test_withdrawn_or_mutated_evidence_hides_reading_without_deleting_history(tmp_path):
    repo, project, source, packet, output = sample(tmp_path)
    _, state, job = publish(repo, source, packet, output)
    with closing(repo._connect()) as db, db:
        db.execute("UPDATE document_fragments SET text='更正内容' WHERE source_id=?", (source.id,))
    result = state.read(project.id)
    assert result["batches"] == []
    assert result["stale_batches"] == 1
    with closing(repo._connect()) as db:
        assert db.execute("SELECT count(*) FROM company_reading_batches WHERE job_id=?", (job["id"],)).fetchone()[0] == 1


def test_compiled_profile_suggestions_do_not_override_human_or_confirmed_content(tmp_path):
    from project_watch.monitor_profile import project_profile
    from project_watch.reading_projection import project_reading
    repo, project, source, packet, output = sample(tmp_path)
    publish(repo, source, packet, output)
    profile = project_profile(repo, project.id)
    assert profile["fields"]["business"]["origin"] == "ai_synthesis"
    assert project_reading(repo, project.id)["compiled"]["batches"][0]["items"][0]["text"] == output["items"][0]["text"]
    observation = repo.add_observation(project.id, source_id=source.id, subject="公司业务",
                                      statement="已核对的业务描述", source_locator="page:1")
    repo.confirm_observation(observation.id, reviewer="synthetic", expected_version=1)
    assert project_profile(repo, project.id)["fields"]["business"]["text"] == "已核对的业务描述"
    repo.edit_monitor_profile(project.id, expected_version=0, fields={"business": "我的描述"})
    assert project_profile(repo, project.id)["fields"]["business"]["text"] == "我的描述"


def test_editorial_rules_address_live_probe_failures_without_changing_document(tmp_path):
    _, _, _, packet, _ = sample(tmp_path)
    prompt = reading_prompt(packet)
    rules, document = prompt.split("\nDOCUMENT:\n", 1)
    from project_watch.company_state import evidence_document
    assert json.loads(document) == evidence_document(packet)
    # These checks protect the generation contract, not model compliance or
    # semantic entailment. Those require a separately authorized live replay.
    for requirement in (
        "Every factual clause in both headline and text",
        "within that item's evidence",
        "narrow or split the item",
        "Missing evidence is not evidence of absence",
        "商业化进展尚待核实",
        "not an operating risk or a credibility finding",
        "do not repeat the same claim under stage and customers",
    ):
        assert requirement in rules
    assert "1-3 exact segment ID strings" in rules


def test_editorial_upgrade_does_not_claim_to_prove_semantics(tmp_path):
    _, _, _, packet, output = sample(tmp_path)
    output["items"][0]["text"] = "这是模型的业务解读，仍需核对。"
    item = validate_reading(packet, output)["items"][0]
    assert item["text"] == output["items"][0]["text"]
    assert item["support_status"] == "quote_verified_not_semantic_verification"
    assert item["review_status"] == "unreviewed"


def test_prompt_carries_exact_output_schema_not_just_field_descriptions(tmp_path):
    from project_watch.company_state import SelectedReadingOutput
    _, _, _, packet, _ = sample(tmp_path)
    prompt = reading_prompt(packet)
    schema = json.loads(prompt.split('\nOUTPUT_SCHEMA:\n', 1)[1].split('\nDOCUMENT:\n', 1)[0])
    assert schema == SelectedReadingOutput.model_json_schema()
    assert schema['$defs']['SelectedReadingItem']['properties']['evidence']['maxItems'] == 3
    assert schema['properties']['items']['maxItems'] == 8


def test_model_selects_original_evidence_instead_of_retyping_it(tmp_path):
    from project_watch.company_state import evidence_document, resolve_reading_selection
    _, _, _, packet, output = sample(tmp_path)
    selected = copy.deepcopy(output)
    selected['items'][0]['evidence'] = ['e1']
    document = evidence_document(packet)
    assert document['fragments'][0]['segments'][0]['text'] == packet['fragments'][0]['text']
    resolved = resolve_reading_selection(packet, selected)
    assert resolved['items'][0]['evidence'] == [{
        'ref_id': packet['fragments'][0]['ref_id'], 'quote': packet['fragments'][0]['text']}]
    item = validate_reading(packet, resolved)['items'][0]
    assert item['support_status'] == 'quote_verified_not_semantic_verification'
    assert selected['items'][0]['evidence'] == ['e1']
    for invalid in (['e999'], ['e1', 'e1'], [{'ref_id': '1:0', 'quote': '重写原文'}]):
        selected['items'][0]['evidence'] = invalid
        with pytest.raises(ValueError):
            resolve_reading_selection(packet, selected)


def test_evidence_segments_preserve_all_text_with_bounded_original_slices():
    from project_watch.company_state import evidence_document
    texts = ['短句。' * 500, 'x' * 1501, '金额3.14万元；' * 200, '短', '<script>不执行</script>']
    packet = {'fragments': [{'ref_id': f'{i}:0', 'text': text} for i, text in enumerate(texts)]}
    doc = evidence_document(packet)
    ids = []
    for original, fragment in zip(texts, doc['fragments'], strict=True):
        assert ''.join(s['text'] for s in fragment['segments']) == original
        assert all(0 < len(s['text']) <= 500 for s in fragment['segments'])
        ids.extend(s['id'] for s in fragment['segments'])
    assert len(ids) == len(set(ids))
    assert packet['fragments'][0]['text'] == texts[0]
