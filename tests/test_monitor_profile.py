"""Synthetic contracts for local monitoring preferences, not formal company facts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from project_watch.api import create_app
from project_watch.monitor_profile import project_profile
from project_watch.repository import ProjectRepository, ReviewConflictError


def sample(tmp_path):
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("Example optics")
    source = repo.add_source(
        project.id, kind="document", reference="synthetic.txt", content_sha256="a" * 64
    )
    observation = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="产品与技术",
        statement="材料描述用于产线检测的光学模组。",
        source_locator="page:2",
    )
    return repo, project, source, observation


def test_name_only_has_no_guessed_identity_or_industry(tmp_path):
    repo = ProjectRepository(tmp_path / "watch.sqlite3")
    project = repo.create_project("星光")
    profile = project_profile(repo, project.id)
    assert profile["name"] == "星光"
    assert profile["identity_status"] == "unverified"
    assert profile["version"] == 0
    assert profile["fields"]["business"]["text"] == ""
    assert profile["focus"] == []
    assert profile["peers"] == []
    assert profile["missing"] == ["business", "stage", "products", "customers"]
    assert repo.monitor_profile_history(project.id) == []  # GET never writes.


@pytest.mark.parametrize(
    "description,question",
    [
        ("合成光学模组进入客户产线试用。", "客户验收是否完成？"),
        ("合成影像设备正在注册检验。", "注册检验节点是否按期推进？"),
        ("合成分子管线正在一期试验。", "本期入组进度如何？"),
        ("合成软件在企业客户进行付费试点。", "试点是否转为年度合同？"),
    ],
)
def test_industry_sample_reuses_attributed_watch_not_guessed_template(
    tmp_path, description, question
):
    repo, project, source, _ = sample(tmp_path)
    observation = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="公司业务",
        statement=description,
        source_locator="page:4",
    )
    repo.confirm_observation(observation.id, reviewer="synthetic reviewer", expected_version=1)
    watch = repo.propose_watch_item(
        project.id, observation_id=observation.id, title="关键节点", question=question
    )
    profile = project_profile(repo, project.id)
    assert profile["fields"]["business"]["text"] == description
    assert profile["fields"]["stage"]["text"] == ""  # Never guess phase from adjacent text.
    assert profile["focus"][0]["text"] == question
    assert profile["focus"][0]["source_refs"][0]["observation_id"] == observation.id
    repo.edit_monitor_profile(project.id, expected_version=0, focus=[])
    assert project_profile(repo, project.id)["focus"] == []
    repo.edit_monitor_profile(project.id, expected_version=1, restore_focus=True)
    assert len(project_profile(repo, project.id)["focus"]) == 1
    repo.review_watch_item(
        project.id, watch.id, action="reject", expected_version=1, reviewer="synthetic reviewer"
    )
    assert project_profile(repo, project.id)["focus"] == []


def test_saved_material_attribution_and_human_edits_survive_refresh(tmp_path):
    repo, project, source, observation = sample(tmp_path)
    proposed = project_profile(repo, project.id)
    product = proposed["fields"]["products"]
    assert product["text"] == observation.statement
    assert product["origin"] == "saved_excerpt"
    assert product["source_refs"][0]["version"] == source.content_sha256
    repo.edit_monitor_profile(
        project.id,
        expected_version=0,
        fields={"products": "暂不采用材料中的产品描述"},
        focus=["核实客户验收进度"],
    )
    changed = repo.add_observation(
        project.id,
        source_id=source.id,
        subject="产品与技术",
        statement="新材料描述另一产品线。",
        source_locator="page:3",
    )
    reopened = ProjectRepository(repo.db_path)
    result = project_profile(reopened, project.id)
    assert result["version"] == 1
    assert result["fields"]["products"]["origin"] == "human"
    assert result["fields"]["products"]["text"] == "暂不采用材料中的产品描述"
    assert result["suggestions"]["products"]["text"] == changed.statement
    assert result["focus"][0]["text"] == "核实客户验收进度"
    assert reopened.get_observation(observation.id).status == "candidate"
    assert reopened.get_project(project.id).identity_status == "unverified"


def test_clear_is_a_durable_override_restore_is_explicit(tmp_path):
    repo, project, _, _ = sample(tmp_path)
    repo.edit_monitor_profile(project.id, expected_version=0, fields={"products": ""}, focus=[])
    assert project_profile(repo, project.id)["fields"]["products"]["text"] == ""
    repo.edit_monitor_profile(project.id, expected_version=1, fields={"business": "人的理解"})
    assert project_profile(repo, project.id)["fields"]["products"]["text"] == ""
    repo.edit_monitor_profile(project.id, expected_version=2, restore_fields=["products"])
    assert project_profile(repo, project.id)["fields"]["products"]["origin"] == "saved_excerpt"
    history = repo.monitor_profile_history(project.id)
    assert [r["version"] for r in history] == [1, 2, 3]
    assert history[0]["fields"] == {"products": ""}


def test_rejected_peer_not_reintroduced_and_cross_project_never_leaks(tmp_path):
    repo, project, _, observation = sample(tmp_path)
    peer = repo.propose_competitive_peer(project.id, name="Not a competitor")
    repo.review_competitive_peer(
        project.id, peer.id, action="reject", expected_version=1, reviewer="synthetic reviewer"
    )
    other = repo.create_project("Unrelated")
    repo.edit_monitor_profile(other.id, expected_version=0, fields={"business": "Private other"})
    result = project_profile(repo, project.id)
    assert result["peers"] == []
    assert "Private other" not in str(result)
    repo.reject_observation(observation.id, reviewer="synthetic", expected_version=1)
    assert project_profile(repo, project.id)["fields"]["products"]["text"] == ""


def test_concurrent_edit_has_one_winner_and_append_only_history(tmp_path):
    repo, project, _, _ = sample(tmp_path)

    def edit(text):
        try:
            repo.edit_monitor_profile(project.id, expected_version=0, fields={"business": text})
            return "ok"
        except ReviewConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(edit, ["A", "B"])) == ["conflict", "ok"]
    with sqlite3.connect(repo.db_path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM monitor_profile_revisions")
    assert len(repo.monitor_profile_history(project.id)) == 1


def test_local_api_versions_origin_guard_validation_and_history(tmp_path):
    repo, project, _, _ = sample(tmp_path)
    client = TestClient(create_app(repo.db_path))
    route = f"/api/workspace/projects/{project.id}/profile"
    payload = {"expected_version": 0, "fields": {"business": "人的业务认识"}}
    assert client.get(route).status_code == 200
    assert client.patch(route, json=payload).status_code == 403
    headers = {"Origin": "http://testserver"}
    assert client.patch(route, headers=headers, json=payload).json()["version"] == 1
    assert client.patch(route, headers=headers, json=payload).status_code == 409
    assert client.get(route + "/history").json()["items"][0]["fields"] == payload["fields"]
    invalid = {"expected_version": 1, "fields": {"legal_name": "Do not infer"}}
    assert client.patch(route, headers=headers, json=invalid).status_code == 422
    invalid = {"expected_version": 1, "fields": {"business": "a" * 2001}}
    assert client.patch(route, headers=headers, json=invalid).status_code == 422
    assert client.get("/api/workspace/projects/missing/profile").status_code == 404


def test_additive_sqlite_upgrade_and_backup_restore(tmp_path):
    from project_watch.backup import create_backup, restore_backup

    live = tmp_path / "live"
    live.mkdir()
    repo, project, _, observation = sample(live)
    # Simulate the preceding schema; restore backup can be opened before or after upgrade.
    with sqlite3.connect(repo.db_path) as c:
        c.execute("DROP TABLE monitor_profile_revisions")
    create_backup(repo.db_path, tmp_path / "before")
    upgraded = ProjectRepository(repo.db_path)
    assert upgraded.get_observation(observation.id) == observation
    upgraded.edit_monitor_profile(project.id, expected_version=0, fields={"business": "人工保留"})
    create_backup(upgraded.db_path, tmp_path / "after")
    restored = ProjectRepository(restore_backup(tmp_path / "after", tmp_path / "restored"))
    assert project_profile(restored, project.id)["fields"]["business"]["text"] == "人工保留"
    prior = ProjectRepository(restore_backup(tmp_path / "before", tmp_path / "rollback"))
    assert project_profile(prior, project.id)["version"] == 0
    assert prior.get_observation(observation.id) == observation
