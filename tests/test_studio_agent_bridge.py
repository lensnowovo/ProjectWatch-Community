import asyncio
import json

import pytest
from test_studio_application_parity import setup_app

from project_watch.studio.agent_bridge import AgentClient, AgentClientError, build_studio_mcp_server


@pytest.mark.parametrize("url", ["https://remote.example", "http://10.0.0.1", "http://user:pass@localhost",
                                  "http://localhost?token=secret", "http://localhost#fragment"])
def test_only_loopback_no_secret_urls(url):
    with pytest.raises(ValueError):
        AgentClient(url, "instance", "project", "pwa_synthetic")


def test_adapter_round_trip_no_database_or_worker(tmp_path):
    app, web, pid, grant = setup_app(tmp_path)

    def transport(method, url, headers, body):
        response = web.request(method, url, headers=headers, content=body)
        return response.status_code, response.content

    client = AgentClient("http://127.0.0.1:8831", grant["instance_id"], pid, grant["token"], transport=transport)
    assert client.runtime()["instance_id"] == grant["instance_id"]
    assert client.search("Optics")["items"][0]["id"] == pid
    version = client.project()["config_version"]
    saved = client.change_status("paused", version, "bridge-pause")
    assert saved["job_status"] == "not_requested"
    assert client.receipt(saved["operation_id"]) == saved
    assert client.project()["status"] == "paused"
    assert not app.state.studio_store.jobs(pid)
    web.delete(f"/api/studio/projects/{pid}/agent-grants/{grant['id']}")
    with pytest.raises(AgentClientError):
        client.project()


def test_adapter_rejects_wrong_instance_and_response_schema():
    def other(*_args):
        return 200, json.dumps({"instance_id": "other", "schema_version": 1}).encode()

    client = AgentClient("http://localhost", "expected", "project", "pwa_synthetic", transport=other)
    with pytest.raises(AgentClientError):
        client.runtime()


def test_optional_mcp_tools_are_bounded(tmp_path):
    pytest.importorskip("mcp")
    from mcp import Client

    _, web, pid, grant = setup_app(tmp_path)

    def transport(method, url, headers, body):
        response = web.request(method, url, headers=headers, content=body)
        return response.status_code, response.content

    client = AgentClient("http://localhost", grant["instance_id"], pid, grant["token"], transport=transport)
    server = build_studio_mcp_server(client)

    async def exercise():
        async with Client(server) as connection:
            tools = {t.name: t for t in (await connection.list_tools()).tools}
            assert tools['project_overview'].annotations.read_only_hint is True
            assert tools['tracked_questions'].annotations.read_only_hint is True
            assert not tools['write_question_assessment'].annotations or not tools['write_question_assessment'].annotations.read_only_hint
            assert {t.name for t in (await connection.list_tools()).tools} == {
                "studio_runtime", "project_overview", "search_project", "set_monitoring_status", "operation_receipt",
                "evidence_versions", "evidence_part", "resolve_excerpt",
                "monitor_objects", "monitor_object", "object_facts", "maintain_object",
                "monitor_changes", "submit_text_source", "source_status", "monitor_relationships", "correct_relationships", "monitor_health", "material_package",
                "monitor_observations", "monitor_event_states", "monitor_milestones", "monitor_concerns", "maintain_concern",
                "tracked_questions", "write_question_assessment",
                "submit_monitoring_results", "monitoring_result_history", "current_monitoring_result",
            }
            overview = (await connection.call_tool("project_overview", {})).structured_content
            assert overview["id"] == pid
            versions = (await connection.call_tool("evidence_versions", {})).structured_content
            assert versions["items"] == [] and versions["project_id"] == pid
            packet = (await connection.call_tool("material_package", {})).structured_content
            assert packet['project_id'] == pid and packet['facts'] == [] and packet['disclosure']['raw_files'] is False
            saved = (await connection.call_tool("set_monitoring_status", {
                "status": "paused", "expected_version": overview["config_version"], "idempotency_key": "mcp-pause",
            })).structured_content
            assert saved["saved"] is True
            assert web.get(f"/api/studio/projects/{pid}").json()["project"]["status"] == "paused"
            web.delete(f"/api/studio/projects/{pid}/agent-grants/{grant['id']}")
            assert (await connection.call_tool("project_overview", {})).is_error

    asyncio.run(exercise())


def test_question_mcp_write_is_visible_without_overwriting_human(tmp_path):
    pytest.importorskip('mcp')
    from mcp import Client

    from project_watch.studio.questions import Assessment

    app, web, pid, grant = setup_app(tmp_path)
    pipeline = app.state.studio_pipeline
    watch = pipeline.monitor.add_watchpoint(pid, '客户交付是否推进', '交付', ['合成交付'], '交付情况',
                                           False, visibility='public')
    human = pipeline.questions.record(pid, watch['id'], 'human',
                                      Assessment(status='insufficient', note='人的原始看法', visibility='public'))

    def transport(method, url, headers, body):
        response = web.request(method, url, headers=headers, content=body)
        return response.status_code, response.content

    client = AgentClient('http://localhost', grant['instance_id'], pid, grant['token'], transport=transport)

    async def exercise():
        async with Client(build_studio_mcp_server(client)) as connection:
            questions = (await connection.call_tool('tracked_questions', {})).structured_content
            assert questions['questions'][0]['id'] == watch['id']
            args = {'watch_id': watch['id'], 'status': 'insufficient', 'note': '助手补充：尚无交付原文',
                    'idempotency_key': 'synthetic-codex-assessment'}
            saved = (await connection.call_tool('write_question_assessment', args)).structured_content
            assert saved['actor'] == 'agent'
            assert (await connection.call_tool('write_question_assessment', args)).structured_content == saved
            visible = pipeline.questions.view(pid)[0]['latest']
            assert visible['agent']['note'] == args['note']
            assert visible['human']['id'] == human['id']
            web.delete(f"/api/studio/projects/{pid}/agent-grants/{grant['id']}")
            assert (await connection.call_tool('tracked_questions', {})).is_error

    asyncio.run(exercise())


def test_private_connection_file_rejects_remote_endpoint(tmp_path, monkeypatch):
    from project_watch.studio.agent_bridge import client_from_env

    path = tmp_path / 'connection.json'
    path.write_text(json.dumps({'url': 'https://remote.example', 'instance_id': 'synthetic',
                               'project_id': 'synthetic', 'token': 'pwa_synthetic'}))
    monkeypatch.setenv('PROJECT_WATCH_AGENT_CONFIG', str(path))
    with pytest.raises(AgentClientError, match='missing or invalid'):
        client_from_env()
