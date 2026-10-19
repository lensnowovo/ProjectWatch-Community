from test_studio_agent_instance import make_service

from project_watch.studio.monitoring_contracts import ObjectInput
from project_watch.studio.objects import ObjectStore


def test_only_public_names_used_and_only_changed_object_queries_invalidated(tmp_path):
    service, pid = make_service(tmp_path / "queries.sqlite3")
    store = ObjectStore(service.store, service.evidence)
    public = store.save(pid, ObjectInput(kind="company", name="Public Co", purpose="subject", visibility="public"), actor="user")
    private = store.save(pid, ObjectInput(kind="person", name="Private Founder"), actor="user")
    first = store.query_inputs(pid)
    assert first["company_names"] == ["Public Co"]
    assert "Private Founder" not in str(first)
    before = first["versions"]
    store.save(pid, ObjectInput(kind="person", name="Changed private name"), actor="user", object_id=private["id"],
               expected_version=1)
    after = store.query_inputs(pid)["versions"]
    assert after == before
    store.save(pid, ObjectInput(kind="company", name="Public renamed", purpose="subject", visibility="public"), actor="user",
               object_id=public["id"], expected_version=1)
    assert store.query_inputs(pid)["versions"][public["id"]] == 2


def test_known_internal_names_removed_from_all_plan_query_types(tmp_path):
    service, pid = make_service(tmp_path / "safe-plan.sqlite3")
    store = ObjectStore(service.store, service.evidence)
    store.save(pid, ObjectInput(kind="company", name="Public Co", purpose="subject", visibility="public"), actor="user")
    store.save(pid, ObjectInput(kind="person", name="Secret Founder"), actor="user")
    plan = store.scoped_plan(pid, {"company_queries": ["Guess"], "people": ["Secret Founder"],
                                  "industry_queries": ["Secret Founder AI", "public industry"],
                                  "competitors": [{"name": "Secret Founder firm"}, {"name": "Public rival"}]})
    assert "Secret Founder" not in str(plan)
    assert plan["company_queries"] == ["Public Co"]
    assert plan["industry_queries"] == ["public industry"]
