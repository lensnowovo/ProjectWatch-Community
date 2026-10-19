"""Offline bilateral gates over shared identities and located source assertions.

The injected review is a semantic check, not independent verification of facts.
This module does not discover peers, authorize disclosure, or call providers.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from project_watch.studio.business_identity import (
    current_located_names,
    has_current_product_ownership,
    ownership_refs,
)
from project_watch.studio.monitoring_contracts import LocatedRef
from project_watch.studio.product_matching import (
    VERSION,
    cgt_scope,
    contexts,
    groups,
    joint_overlap,
    suggested_matches,
    term,
)
from project_watch.studio.product_matching import (
    normal as _normal,
)
from project_watch.studio.provenance import effective_family
from project_watch.studio.relationships import scope_fields


class SemanticReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verdict: Literal["supported", "disputed", "unknown"]
    reason: str = Field(min_length=1, max_length=1000)
    fact_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    matched_fact_ids: tuple[str, ...] = Field(default=(), max_length=100)


def assess_relation(service, project_id, scope, relation_type, *, review=None, include_internal=True):
    """Necessary gates, retaining unknown/conflicting/source-self-stated data.

    Comparison uses a small whole-value vocabulary and otherwise exact values.
    Missing dimensions are not filled from industry names or model memory. A caller must
    obtain model/provider permission before supplying a network-backed reviewer.
    """
    if relation_type not in ("direct", "substitute", "adjacent", "unknown", "unrelated"):
        raise ValueError("invalid relationship type")
    visibility, versions = service.relationships.validate_scope(project_id, scope, include_internal=include_internal)
    generation = service.evidence.dependencies.generation(project_id)
    result = {**scope_fields(scope), "project_id": project_id, "schema_version": 1,
              "relation_type": relation_type, "support": "pending", "visibility": visibility,
              "object_versions": versions, "input_generation": generation,
              "fact_ids": [], "evidence_ids": [], "missing": [], "review": None,
              "overlap_kind": "unknown", "source_independence": "unknown",
              "assertion_basis": "bilateral_source_assertions_not_independent_fact_verification"}
    missing, facts, evidence = result["missing"], [], {}
    objects = {key: service.objects.get(project_id, value, include_internal=include_internal)
               for key, value in scope_fields(scope).items() if value}
    if not all(k in objects for k in ("subject_product_id", "peer_product_id")):
        missing.append("product_scope")
        return result
    for key, obj in objects.items():
        if obj['kind'] == 'company':
            names, current_refs = current_located_names(service.objects.store, project_id, obj, obj['refs'], include_internal=include_internal)
            located = any(ownership_refs(obj['name'], obj['name'], r['quote'], names) is not None for r in current_refs)
        else:
            located = any(_normal(obj['name']) in _normal(r['quote']) for r in obj['refs'])
        if not located:
            missing.append("located_identity:" + key)
    sides = []
    day = date.today().isoformat()
    for product_key, company_key in (("subject_product_id", "subject_id"), ("peer_product_id", "peer_id")):
        product, company = objects[product_key], objects[company_key]
        if not has_current_product_ownership(service.objects.store, project_id, company, product,
                                             include_internal=include_internal):
            missing.append("located_product_ownership:" + product_key)
        side = []
        for fact in service.objects.facts(project_id, product["id"], include_internal=include_internal):
            if fact["predicate"] not in ("use_case", "buyer_type", "delivery") or fact["subject_attribution"] != "explicit":
                continue
            if (not fact.get("dependency") or fact["dependency"]["status"] != "current"
                    or fact.get("basis") != "actual"
                    or (fact.get("valid_from") and fact["valid_from"] > day)
                    or (fact.get("valid_to") and fact["valid_to"] < day)):
                continue
            if not fact["value"] or not any(_normal(fact["value"]) in _normal(r["quote"])
                    and _normal(product["name"]) in _normal(r["quote"]) for r in fact["refs"]):
                continue
            located = []
            for ref in fact["refs"]:
                service.evidence.resolve(project_id, LocatedRef.model_validate(ref), include_internal=include_internal)
                located.append(service.evidence.get(project_id, ref["evidence_version_id"], include_internal=include_internal))
            # Summaries remain useful discovery inputs, not product/buyer proof.
            if any(e["read_scope"] == "summary" for e in located):
                continue
            side.append(fact)
            evidence.update({e["id"]: e for e in located})
        if any(not any(f["predicate"] == key for f in side) for key in ("use_case", "buyer_type", "delivery")):
            missing.append("current_bilateral_facts")
        sides.append(side)
        facts.extend(side)
    result["fact_ids"] = [f["id"] for f in facts]
    result["evidence_ids"] = sorted(evidence)
    if any(e["visibility"] == "internal" for e in evidence.values()) or any(f["visibility"] == "internal" for f in facts):
        result["visibility"] = "internal"
    labels = {key: effective_family(item)['propagation_family'] for key, item in evidence.items()}
    families = [{labels[r['evidence_version_id']] for f in side for r in f['refs']
                 if labels[r['evidence_version_id']]} for side in sides]
    if families[0] & families[1]:
        missing.append("distinct_bilateral_sources")
        result["source_independence"] = "shared_propagation_family"
    controlled = [cgt_scope(side) for side in sides]
    values = [groups(side, cgt=is_cgt) for side, is_cgt in zip(sides, controlled, strict=True)]
    passages = [contexts(side, cgt=is_cgt) for side, is_cgt in zip(sides, controlled, strict=True)]
    result["matching"] = {"version": VERSION, "controlled_terms": any(controlled),
        "terms": [{"fact_id": f["id"], "predicate": f["predicate"],
                   **term(f["predicate"], f["value"], cgt=is_cgt)}
                  for side, is_cgt in zip(sides, controlled, strict=True) for f in side],
        "context_rule": "co_located_multi_values_not_semantic_pairing",
        "multi_value": any(len(v[d]) > 1 for v in values for d in ("use_case", "buyer_type", "delivery")),
        "suggested_matches": suggested_matches(passages, sides, controlled, relation_type)
            if relation_type in ("direct", "substitute") and len(facts) <= 100
            and all(len(p) <= 64 for p in passages) else []}
    def same(key):
        return bool(values[0][key] & values[1][key])
    if not same("use_case"):
        missing.append("use_case_overlap")
    if not same("buyer_type"):
        missing.append("buyer_overlap")
    else:
        result["overlap_kind"] = "buyer_type_not_same_customer"
    if relation_type == "direct" and not same("delivery"):
        missing.append("compatible_delivery")
    if relation_type == "substitute" and (same("delivery") or not all(v["delivery"] for v in values)):
        missing.append("distinct_delivery")
    if len(facts) > 100 or any(len(p) > 64 for p in passages):
        missing.append("comparison_bounds")
    elif not joint_overlap(*passages, relation_type):
        missing.append("joint_use_buyer_delivery")
    if relation_type not in ("direct", "substitute"):
        missing.append("competitive_relation_not_proposed")
    if missing:
        result["missing"] = list(dict.fromkeys(missing))
        return result
    if review is None:
        missing.append("semantic_review")
        return result
    try:
        checked = SemanticReview.model_validate(review({"scope": scope_fields(scope), "relation_type": relation_type,
            "facts": facts, "matching": result["matching"], "object_versions": versions,
            "assertion_basis": result["assertion_basis"]}))
        if set(checked.fact_ids) != set(result["fact_ids"]):
            raise ValueError("review must cite supplied bilateral facts, without foreign facts")
        if checked.verdict == "supported" and result["matching"]["multi_value"]:
            selected = set(checked.matched_fact_ids)
            if not selected or not selected <= set(result["fact_ids"]):
                raise ValueError("multi-value review needs explicit selected bilateral facts")
            selected_sides = [[f for f in side if f['id'] in selected] for side in sides]
            selected_values = [groups(side, cgt=is_cgt) for side, is_cgt in zip(selected_sides, controlled, strict=True)]
            if any(len(v[d]) != 1 for v in selected_values for d in ("use_case", "buyer_type", "delivery")):
                raise ValueError("selected facts must identify one concrete bilateral context")
            selected_passages = [contexts(side, cgt=is_cgt, singular_legacy=False)
                for side, is_cgt in zip(selected_sides, controlled, strict=True)]
            if not joint_overlap(*selected_passages, relation_type):
                raise ValueError("selected dimensions cannot be crossed between excerpts")
    except Exception:
        # Do not log raw provider output or silently retry a paid call.
        missing.append("valid_semantic_review")
        return result
    result["review"] = checked.model_dump(mode="json")
    if service.evidence.dependencies.generation(project_id) != generation:
        missing.append("inputs_changed_during_review")
    elif checked.verdict == "supported":
        result["support"] = "supported"
    elif checked.verdict == "disputed":
        result["support"] = "disputed"
    else:
        missing.append("semantic_support")
    return result
