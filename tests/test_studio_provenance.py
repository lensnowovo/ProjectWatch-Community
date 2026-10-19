from project_watch.studio.provenance import provenance


def test_origin_is_not_just_a_domain_claim():
    assert provenance("https://www.cninfo.com.cn/report.pdf", "正文" * 300)["origin"] == "regulator"
    assert provenance("https://company.example/about", "正文" * 300,
                      company_website="https://company.example")["origin"] == "company_self"
    assert provenance("https://company.example.evil.invalid/about", "正文" * 300,
                      company_website="https://company.example")["origin"] == "unknown"
    assert provenance("https://partner.example/post", "合作披露", declared_origin="partner")["origin"] == "partner"


def test_same_release_shares_family_but_same_outlet_not_independent_interviews():
    original = "同一份公司通稿讲述技术开发进展以及产品发布时间。" * 40
    first = provenance("https://news.example/a", original, declared_origin="media")
    copied = provenance("https://different.example/b", original.replace("。", "。\n"), declared_origin="media")
    interview = provenance("https://news.example/c", "另一份独立采访介绍不同事实和业务数据。" * 40,
                           declared_origin="media")
    assert first["propagation_family"] == copied["propagation_family"]
    assert first["propagation_family"] != interview["propagation_family"]


def test_short_summary_is_not_an_independent_fulltext_family():
    data = provenance("https://news.example/a", "摘要", read_scope="summary")
    assert data["family_basis"] == "source_only"
