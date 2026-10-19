"""The evaluation harness scores deterministically; checked here with scripted fake models."""

from __future__ import annotations

import re

from project_watch.studio import evaluation as ev


class Oracle:
    """Answers with the gold labels (optionally corrupted) so the scoring itself is tested."""

    def __init__(self, cases: dict, *, sloppy: bool = False) -> None:
        self.cases, self.sloppy = cases, sloppy

    def json(self, system: str, user: str, *, max_tokens: int = 0):
        if "正在阅读" in system:
            case = next(c for c in self.cases["extraction"] if f"({c['document']} · line 1) {c['lines'][0]}" in user)
            metrics = []
            for g in case["gold"]:
                value, unit = g["value"], g["unit"]
                if self.sloppy and unit == "亿元":
                    unit = "万元"  # lost the 亿 → off by 10^4
                metrics.append({"name": g["name"], "period": g["periods"][0], "value": value, "unit": unit,
                                "basis": "actual" if self.sloppy else g["basis"], "ref": "F1"})
            if self.sloppy:
                metrics = [*metrics[1:], {"name": "同比增长", "period": "2025", "value": 64, "unit": "%", "ref": "F1"}]
            return {"metrics": metrics}
        if "筛选" in system:
            out = []
            for item in self.cases["triage"]:
                g = item["gold"]
                relevant = True if self.sloppy else g["relevant"]
                out.append({"id": item["id"], "relevant": relevant, "category": g.get("category", item["category"]),
                            "importance": (g.get("importance") or ["routine"])[0]})
            return {"results": out}
        if "同一事件" in system:
            n = len(re.findall(r"^\[\d+\]", user, re.M))
            return {"results": [{"pair": i + 1, "same": True if self.sloppy else p["same"]}
                                for i, p in enumerate(self.cases["same_event"][:n])]}
        raise AssertionError(system[:20])


def test_cases_are_well_formed():
    cases = ev.load_cases()
    assert len(cases["extraction"]) >= 5 and len(cases["triage"]) >= 10 and len(cases["same_event"]) >= 5
    for case in cases["extraction"]:
        for g in case["gold"]:
            assert g["basis"] in ("actual", "forecast", "budget") and g["periods"]


def test_perfect_answers_score_full_marks():
    cases = ev.load_cases()
    report = ev.run(Oracle(cases), cases)
    s = report["summary"]
    assert s["extraction_recall"] == 1 and s["extraction_basis_accuracy"] == 1 and s["extraction_unit_errors"] == 0
    assert s["triage_relevance_accuracy"] == 1 and s["triage_importance_accuracy"] == 1
    assert s["triage_category_accuracy"] == 1 and s["triage_missed_major"] == []
    assert s["same_event_accuracy"] == 1 and s["same_event_false_merges"] == [] and s["failed_calls"] == 0
    assert "| 指标抽取召回 | 100% |" in ev.markdown(report, model="oracle")


def test_sloppy_answers_are_caught_by_kind():
    cases = ev.load_cases()
    s = ev.run(Oracle(cases, sloppy=True), cases)["summary"]
    assert s["extraction_recall"] < 1 and s["extraction_unit_errors"] >= 2  # 0.86亿/1.2亿 read as 万元
    assert s["extraction_basis_accuracy"] < 1 and s["extraction_unmatched"] >= 5
    assert s["triage_relevance_accuracy"] < 1
    assert set(s["same_event_false_merges"]) == {"two-rounds", "order-vs-partnership", "competitor-two-news"}


def test_model_failures_are_counted_not_raised():
    class Broken:
        def json(self, *a, **k):
            from project_watch.studio.llm import ModelError
            raise ModelError("timeout")

    report = ev.run(Broken())
    assert report["summary"]["failed_calls"] == len(ev.load_cases()["extraction"]) + 2
    assert report["summary"]["extraction_recall"] == 0
    assert report["summary"]["triage_relevance_accuracy"] == 0
    assert report["summary"]["triage_importance_accuracy"] == 0
    assert report["summary"]["same_event_accuracy"] == 0


def test_partial_failures_stay_in_the_scoring_denominator():
    cases = ev.load_cases()

    class Sometimes(Oracle):
        def json(self, system, user, **kwargs):
            if cases["extraction"][0]["lines"][0] in user:
                from project_watch.studio.llm import ModelError
                raise ModelError("timeout")
            return super().json(system, user, **kwargs)

    summary = ev.run(Sometimes(cases), cases)["summary"]
    assert summary["failed_calls"] == 1 and 0 < summary["extraction_recall"] < 1


def test_malformed_boolean_answers_are_not_scored_as_correct():
    cases = ev.load_cases()
    assert ev.score_triage(cases["triage"], [{"id": x["id"], "relevant": "false"}
                                           for x in cases["triage"]])["answered"] == 0
    assert ev.score_same_event(cases["same_event"], [{"pair": n, "same": "false"}
                                                    for n in range(1, 10)])["accuracy"] == 0


def test_missing_response_arrays_are_reported_as_failures():
    class Malformed:
        def json(self, *args, **kwargs):
            return {"results": "not an array"}

    summary = ev.run(Malformed())["summary"]
    assert summary["failed_calls"] == len(ev.load_cases()["extraction"]) + 2
    assert summary["extraction_recall"] == 0 and summary["same_event_accuracy"] == 0


def test_script_skips_without_credentials(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    for name in ("PW_MODEL_BASE_URL", "PW_MODEL_ID", "PW_MODEL_KEY"):
        monkeypatch.delenv(name, raising=False)
    path = Path(__file__).resolve().parents[1] / "scripts" / "studio_eval.py"
    spec = importlib.util.spec_from_file_location("studio_eval_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--out", str(tmp_path / "r")]) == 2
    assert not (tmp_path / "r.json").exists()
