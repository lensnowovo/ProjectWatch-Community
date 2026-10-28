"""Synthetic order claims: title and body must preserve the quoted scope."""
import pytest

from project_watch.studio.dsh_bootstrap import RecordValidationError, validate_record

QUOTE = "合成公司的设备已售11套、终端使用6套，在手及意向订单超过80套。"


def source(quote=QUOTE):
    return {"id": "S1", "excerpt": quote, "published_at": "2026-01-01"}


@pytest.mark.parametrize("field", ["label", "text"])
@pytest.mark.parametrize("claim", [
    "设备已售11套，订单超80套",
    "设备已售11套，在手订单超过80套",
    "设备已售11套，已签约订单超过80套",
    "设备已售11套，意向订单超过80套",
    # An unrelated occurrence of 意向 must not satisfy this claim's scope.
    "意向客户待跟进，订单超过80套",
])
def test_mixed_order_claim_cannot_become_firm_or_all_intent(field, claim):
    values = {"text": QUOTE, "label": "订单概况"}
    values[field] = claim
    with pytest.raises(RecordValidationError) as error:
        validate_record(source(), QUOTE, **values)
    assert any(i["field"] == field and i["code"] == "order_qualification_mismatch"
               for i in error.value.issues)


@pytest.mark.parametrize("label", [
    "订单", "设备已售11套", "终端使用6套",
    "在手及意向订单超80套", "在手与意向订单超过80套",
])
def test_neutral_or_unrelated_titles_need_not_repeat_all_quote_qualifiers(label):
    validate_record(source(), QUOTE, QUOTE, label)


@pytest.mark.parametrize("quote,label", [
    ("合成公司意向订单为500万元。", "订单为500万元"),
    ("合成公司预计订单达90套。", "订单达90套"),
    ("合成公司计划订单达到90套。", "订单达到90套"),
    ("合成公司在手及意向订单合计80套。", "在手订单合计80套"),
])
def test_other_order_qualifiers_are_checked_in_the_title(quote, label):
    with pytest.raises(RecordValidationError) as error:
        validate_record(source(quote), quote, quote, label)
    assert any(i["field"] == "label" and i["code"] == "order_qualification_mismatch"
               for i in error.value.issues)


def test_separate_actual_and_intent_amounts_do_not_contaminate_each_other():
    quote = "合成公司已签约订单为30套，意向订单为80套。"
    validate_record(source(quote), quote, quote, "已签约订单为30套")


def test_same_amount_with_conflicting_scopes_requires_a_narrower_quote():
    quote = "甲公司在手订单80套，乙公司在手及意向订单80套。"
    with pytest.raises(RecordValidationError) as error:
        validate_record(source(quote), quote, quote, "订单80套")
    assert any(i["field"] == "label" and i["code"] == "order_qualification_mismatch"
               for i in error.value.issues)


def test_verbatim_mixed_scope_is_not_rewritten():
    validate_record(source(), QUOTE, QUOTE, "在手及意向订单超过80套")


@pytest.mark.parametrize("monitor", [False, True])
def test_real_tool_rejects_before_staging_then_accepts_explicit_correction(tmp_path, monitor):
    from project_watch.studio.app import create_studio_app
    from project_watch.studio.dsh_bootstrap import PublicBootstrap
    from project_watch.studio.dsh_monitor import PublicMonitor
    from project_watch.studio.execution import JobExecution

    class Search:
        def web_search(self, query, count=6):
            return [{"url": "https://example.com/synthetic-orders", "title": "合成公司订单",
                     "snippet": QUOTE, "source": "Synthetic", "published_at": "2026-01-01"}]

    pipeline = create_studio_app(tmp_path, start_worker=False).state.studio_pipeline
    project = pipeline.repository.create_project("合成公司")
    pipeline.store.upsert_project(project.id, {"legal_name": "合成公司"}, consent=True)
    pipeline.store.enqueue(project.id, "external")
    execution = JobExecution(pipeline.store, pipeline.store.claim())
    research = (PublicMonitor if monitor else PublicBootstrap)(pipeline, execution, Search(), None)
    from test_studio_dsh_monitor import synthetic_review
    research.record_reviewer = synthetic_review
    research.tool("pw_search", {"query": "合成公司订单", "category": "company"})
    args = {"kind": "business", "source_id": "S1", "quote": QUOTE, "text": QUOTE,
            "label": "订单超80套"}
    with pytest.raises(RecordValidationError) as error:
        research.tool("pw_record", args)
    assert error.value.feedback()["terminal"] is False
    assert not research.state["records"]
    # The tool does not silently fix a submitted title or initiate any retry.
    assert args["label"] == "订单超80套"
    args["label"] = "在手及意向订单超80套"
    research.tool("pw_record", args)
    assert research.state["records"][0]["label"] == args["label"]
    if not monitor:
        research.tool("pw_finish", {"gaps": {"people": "合成样本未覆盖"}})
        facts = pipeline.store.get_public_profile(project.id)["facts"]
        assert facts[0]["label"] == args["label"]
