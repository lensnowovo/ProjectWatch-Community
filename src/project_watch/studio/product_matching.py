"""Small, versioned eligibility vocabulary, never a clinical/market taxonomy.

Only whole quoted values are mapped. Unknown values retain literal comparison;
no token similarity, model memory, broad category inference or fact rewriting.
The first CGT entries come from the approved E4 design and remain provisional
until real source/user evaluation. Semantic review must still assess each pair.
"""

from __future__ import annotations

from project_watch.studio.normalize import normal

VERSION = "product-matching-v1"
DIMENSIONS = ("use_case", "buyer_type", "delivery")
CGT = {
    "use_case": {
        "cd19-b-all": ("CD19阳性B-ALL治疗", "CD19阳性B细胞急性淋巴细胞白血病治疗"),
        "bcma-mm": ("BCMA阳性多发性骨髓瘤治疗", "BCMA靶向多发性骨髓瘤治疗"),
        "cg-manufacturing": ("细胞治疗生产工艺开发", "细胞治疗制造工艺开发"),
    },
    "buyer_type": {
        "care-provider": ("医院", "医疗机构"),
        "biopharma": ("生物制药企业", "生物医药企业"),
    },
    "delivery": {
        "autologous-car-t": ("自体CAR-T细胞制剂", "患者自体CAR-T制剂"),
        "allogeneic-car-t": ("异体CAR-T细胞制剂", "供者来源CAR-T制剂"),
        "process-service": ("工艺开发服务", "生产工艺服务"),
    },
}


_ALIASES = {dim: {normal(alias): "cgt:" + code for code, aliases in groups.items()
                  for alias in aliases} for dim, groups in CGT.items()}


def cgt_scope(facts):
    # A hospital/industry word alone cannot activate CGT matching.
    return any(f["predicate"] in ("use_case", "delivery")
               and normal(f["value"]) in _ALIASES[f["predicate"]] for f in facts)


def term(predicate, value, *, cgt=False):
    code = _ALIASES.get(predicate, {}).get(normal(value)) if cgt else None
    return {"value": value, "key": code or "literal:" + normal(value),
            "mode": "controlled" if code else "literal"}


def groups(facts, *, cgt=False):
    return {dim: {term(dim, f["value"], cgt=cgt)["key"] for f in facts
                  if f["predicate"] == dim} for dim in DIMENSIONS}


def contexts(facts, dimensions=DIMENSIONS, *, cgt=False, singular_legacy=True):
    """Co-located dimensions, not a Cartesian claim across different passages.

    Co-location is only an eligibility bound, not proof that every combination
    within a long paragraph is valid. The reviewer receives all original facts.
    Singular legacy dimensions may span different quotes; multi-value dimensions
    require a complete shared located excerpt. Incomplete contexts remain facts.
    """
    located = {}
    for fact in facts:
        if fact["predicate"] not in dimensions:
            continue
        for ref in fact["refs"]:
            if normal(fact["value"]) not in normal(ref["quote"]):
                continue
            identity = (ref["evidence_version_id"], ref["part_index"], ref["start"], ref["end"])
            row = located.setdefault(identity, [])
            if fact not in row:
                row.append(fact)
    result = []
    for identity, rows in sorted(located.items()):
        mapped = groups(rows, cgt=cgt)
        if all(mapped[d] for d in dimensions):
            result.append({"location": list(identity), "groups": mapped,
                           "fact_ids": [f["id"] for f in rows]})
    mapped = groups(facts, cgt=cgt)
    if singular_legacy and not result and all(len(mapped[d]) == 1 for d in dimensions):
        result.append({"location": None, "groups": mapped,
                       "fact_ids": [f["id"] for f in facts if f["predicate"] in dimensions]})
    return result


def joint_overlap(left, right, relation_type):
    for a in left:
        for b in right:
            x, y = a["groups"], b["groups"]
            if not x["use_case"] & y["use_case"] or not x["buyer_type"] & y["buyer_type"]:
                continue
            delivery = bool(x["delivery"] & y["delivery"])
            if (relation_type == "direct" and delivery) or (relation_type == "substitute" and not delivery):
                return True
    return False


def suggested_matches(passages, sides, controlled, relation_type):
    """At most eight located candidates for review, not assertions of pairing."""
    matches = []
    by_id = [{f['id']: f for f in side} for side in sides]
    for a in passages[0]:
        for b in passages[1]:
            x, y = a['groups'], b['groups']
            uses, buyers = sorted(x['use_case'] & y['use_case']), sorted(x['buyer_type'] & y['buyer_type'])
            common = sorted(x['delivery'] & y['delivery'])
            if not uses or not buyers or (relation_type == 'direct' and not common):
                continue
            if relation_type == 'substitute' and (common or not x['delivery'] or not y['delivery']):
                continue
            deliveries = (common[0], common[0]) if relation_type == 'direct' else (
                sorted(x['delivery'])[0], sorted(y['delivery'])[0])
            selected = []
            for index, passage in enumerate((a, b)):
                keys = dict(zip(DIMENSIONS, (uses[0], buyers[0], deliveries[index]), strict=True))
                for dim in DIMENSIONS:
                    selected.append(next(fid for fid in passage['fact_ids']
                        if by_id[index][fid]['predicate'] == dim
                        and term(dim, by_id[index][fid]['value'], cgt=controlled[index])['key'] == keys[dim]))
            if selected not in matches:
                matches.append(selected)
            if len(matches) == 8:
                return matches
    return matches
