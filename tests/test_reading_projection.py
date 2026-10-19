from dataclasses import replace
from decimal import Decimal

from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.reading_projection import build_reading, can_display_reading_item
from project_watch.repository import ProjectRepository


def sample(tmp_path):
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Synthetic optics")
    source = repo.add_source(
        project.id, kind="document", reference="intro.txt", content_sha256="a" * 64
    )
    observation = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="产品与技术",
        statement="材料描述光学检测产品。",
        source_locator="page:2",
    )
    return repo, project, source, observation


def test_numeric_conflict_does_not_merge_forecast_with_actual(tmp_path):
    _, project, source, observation = sample(tmp_path)
    actual = replace(
        observation, number=Decimal("0"), unit="万元", period="2025", scope="公司", basis="actual"
    )
    second = replace(actual, id="second", number=Decimal("5"))
    forecast = replace(actual, id="forecast", number=Decimal("8"), basis="forecast")
    items = build_reading(project.id, [source], [actual, second, forecast])["sections"][0]["items"]
    assert next(i for i in items if i["id"] == actual.id)["number"] == "0"
    assert next(i for i in items if i["id"] == actual.id)["has_conflict"]
    assert not next(i for i in items if i["id"] == forecast.id)["has_conflict"]
    assert build_reading(project.id, [source], [actual])["summary"] is None


def test_different_metrics_are_not_numeric_conflicts(tmp_path):
    _, project, source, observation = sample(tmp_path)
    revenue = replace(observation, statement='营业收入', number=Decimal('100'),
                      unit='万元', period='2025', scope='合并', basis='actual')
    profit = replace(revenue, id='profit', statement='净利润', number=Decimal('10'))
    assert not any(i['has_conflict'] for i in build_reading(project.id, [source],
                   [revenue, profit])['sections'][0]['items'])


def test_readable_draft_is_attributed_without_confirming(tmp_path):
    repo, project, source, observation = sample(tmp_path)
    result = build_reading(project.id, [source], [observation])
    item = result["sections"][0]["items"][0]
    assert item["review_status"] == "unreviewed"
    assert item["source_refs"][0]["version"] == source.content_sha256
    assert item["as_of"] is None
    assert item["origin"] == "saved_excerpt"  # No recorded producer: don't invent provenance.
    assert can_display_reading_item(item, project_id=project.id, allowed_source_ids={source.id})
    assert not can_display_reading_item(item, project_id="other", allowed_source_ids={source.id})
    assert not can_display_reading_item(item, project_id=project.id, allowed_source_ids=set())
    assert repo.get_observation(observation.id).status == "candidate"


def test_rejected_or_orphan_or_other_project_never_appears(tmp_path):
    _, project, source, observation = sample(tmp_path)
    for invalid in [
        replace(observation, status="rejected"),
        replace(observation, project_id="other"),
        replace(observation, source_locator=""),
    ]:
        assert build_reading(project.id, [source], [invalid])["sections"] == []
    assert build_reading(project.id, [], [observation])["sections"] == []


def test_human_confirmation_has_priority_and_new_material_remains_separate(tmp_path):
    _, project, source, observation = sample(tmp_path)
    confirmed = replace(observation, id="human", status="confirmed", statement="人的结论")
    result = build_reading(project.id, [source], [observation, confirmed])
    assert result["sections"][0]["items"][0]["text"] == "人的结论"
    assert result["sections"][0]["items"][1]["review_status"] == "unreviewed"
    assert result["summary"]["text"] == "人的结论"


def test_get_reading_is_read_only_and_project_scoped(tmp_path):
    repo, project, _source, observation = sample(tmp_path)
    other = repo.create_project("Another company")
    client = TestClient(create_app(repo.db_path))
    route = f"/api/workspace/projects/{project.id}/reading"
    first = client.get(route)
    assert first.status_code == 200
    assert first.json() == client.get(route).json()
    assert client.get(f"/api/workspace/projects/{other.id}/reading").json()["sections"] == []
    assert client.get("/api/workspace/projects/missing/reading").status_code == 404
    assert repo.get_observation(observation.id).status == "candidate"
    assert repo.list_observations(project.id, status="confirmed") == []
    cards = client.get("/api/workspace/projects").json()["items"]
    assert (
        next(c for c in cards if c["id"] == project.id)["reading_summary"]["text"]
        == observation.statement
    )
