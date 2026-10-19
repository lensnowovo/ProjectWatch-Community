"""Thin HTTP-backed Studio MCP/CLI client, never a database adapter."""

from __future__ import annotations

import json
import os
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from project_watch.studio.agent_result_contracts import MonitoringResults

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AgentClientError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a project secret to a redirect destination.
        return None


def _transport(method, url, headers, body):
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            status = response.status
    except HTTPError as exc:
        with exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            status = exc.code
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AgentClientError("response exceeds bounded size")
    return status, raw


class AgentClient:
    def __init__(self, base_url: str, instance_id: str, project_id: str, token: str, *, transport=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError("Studio Agent bridge requires an explicit loopback HTTP origin")
        if not instance_id or not project_id or not token.startswith("pwa_"):
            raise ValueError("instance, project and project grant are required")
        self.base_url = base_url.rstrip("/")
        self.instance_id = instance_id
        self.project_id = project_id
        self._token = token
        self._transport = transport or _transport
        self._project_path = "/api/studio/agent/projects/" + quote(project_id, safe="")

    def _request(self, method: str, path: str, payload: dict | bytes | None = None, *, content_type="application/json", envelope=True) -> dict:
        headers = {"Authorization": f"Bearer {self._token}", "X-ProjectWatch-Instance": self.instance_id,
                   "Content-Type": content_type}
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            code, raw = self._transport(method, self.base_url + path, headers, body)
        except (OSError, TimeoutError) as exc:
            raise AgentClientError("Studio instance unavailable; no request was retried") from exc
        if code >= 300:
            # Status only: do not echo an arbitrary remote body/secret into logs.
            raise AgentClientError(f"Studio returned HTTP {code}; read current state or check project grant")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise AgentClientError("invalid Studio response") from exc
        if (not isinstance(result, dict) or (envelope and (
                result.get("instance_id") != self.instance_id or result.get("schema_version") != 1))):
            raise AgentClientError("unexpected Studio instance or response schema")
        return result

    def runtime(self) -> dict:
        return self._request("GET", "/api/studio/agent/runtime")

    def project(self) -> dict:
        return self._request("GET", self._project_path)

    def search(self, query: str = "") -> dict:
        return self._request("GET", self._project_path + "/search?" + urlencode({"q": query}))

    def change_status(self, status: str, expected_version: int, idempotency_key: str) -> dict:
        return self._request("PATCH", self._project_path + "/status", {
            "status": status, "expected_version": expected_version, "idempotency_key": idempotency_key,
        })

    def receipt(self, operation_id: str) -> dict:
        return self._request("GET", self._project_path + "/operations/" + quote(operation_id, safe=""))

    def evidence(self, limit: int = 20, cursor: str | None = None) -> dict:
        query = {"limit": limit}
        if cursor is not None:
            query["cursor"] = cursor
        return self._request("GET", self._project_path + "/evidence?" + urlencode(query))

    def evidence_part(self, version_id: str, part_index: int) -> dict:
        return self._request("GET", self._project_path + "/evidence/" + quote(version_id, safe="")
                             + "/parts/" + str(part_index))

    def resolve_excerpt(self, version_id: str, part_index: int, start: int, end: int, quote_text: str) -> dict:
        return self._request("POST", self._project_path + "/evidence/resolve", {
            "evidence_version_id": version_id, "part_index": part_index, "start": start, "end": end,
            "quote": quote_text,
        })

    def objects(self, limit: int = 20, after: str = "") -> dict:
        return self._request("GET", self._project_path + "/objects?" + urlencode({"limit": limit, "after": after}))

    def object_detail(self, object_id: str) -> dict:
        return self._request("GET", self._project_path + "/objects/" + quote(object_id, safe=""))

    def object_facts(self, object_id: str) -> dict:
        return self._request("GET", self._project_path + "/objects/" + quote(object_id, safe="") + "/facts")

    def maintain_object(self, object_record: dict, expected_project_version: int, idempotency_key: str,
                        object_id: str | None = None, expected_object_version: int | None = None) -> dict:
        return self._request("POST", self._project_path + "/objects", {
            "object": object_record, "expected_project_version": expected_project_version,
            "object_id": object_id, "expected_object_version": expected_object_version, "idempotency_key": idempotency_key,
        })

    def changes(self, after: int = 0, limit: int = 20, through: int | None = None) -> dict:
        query = {"after": after, "limit": limit}
        if through is not None:
            query["through"] = through
        return self._request("GET", self._project_path + "/changes?" + urlencode(query))

    def submit_text(self, filename: str, text: str, expected_version: int, idempotency_key: str) -> dict:
        if not 1 <= len(text) <= 100000:
            raise ValueError("text submission must contain 1-100000 characters")
        return self._request("POST", self._project_path + "/text-sources", {
            "filename": filename, "text": text, "expected_version": expected_version, "idempotency_key": idempotency_key,
        })

    def submit_file(self, path, expected_version: int, idempotency_key: str) -> dict:
        # Only the explicit local CLI path is read. MCP has no server file-path tool.
        from pathlib import Path

        file_path = Path(path)
        with file_path.open("rb") as stream:
            data = stream.read()
        if not data:
            raise ValueError("file submission is empty")
        filename = file_path.name
        if any(c in filename for c in ('"', '\r', '\n')):
            raise ValueError("invalid filename")
        if type(expected_version) is not int or expected_version < 1 or not 1 <= len(idempotency_key) <= 100:
            raise ValueError("invalid version or idempotency key")
        boundary = "pw_" + uuid.uuid4().hex
        parts = []
        for name, value in (("expected_version", str(expected_version)), ("idempotency_key", idempotency_key)):
            if '\r' in value or '\n' in value:
                raise ValueError("invalid multipart field")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
                      data, f'\r\n--{boundary}--\r\n'.encode()])
        return self._request("POST", self._project_path + "/sources", b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}")

    def source_status(self, source_id: str) -> dict:
        return self._request("GET", self._project_path + "/sources/" + quote(source_id, safe=""))

    def relationships(self, limit: int = 20, after: str = "") -> dict:
        return self._request("GET", self._project_path + "/relationships?" + urlencode({"limit": limit, "after": after}))

    def correct_relationships(self, changes: list[dict], expected_version: int, idempotency_key: str) -> dict:
        return self._request("POST", self._project_path + "/relationships/corrections", {
            "changes": changes, "expected_version": expected_version, "idempotency_key": idempotency_key,
        })

    def health(self) -> dict:
        return self._request("GET", self._project_path + "/health")

    def material_package(self) -> dict:
        return self._request("GET", self._project_path + "/material-package")

    def observations(self, limit=50, after='') -> dict:
        return self._request('GET', self._project_path + '/observations?' + urlencode({'limit': limit, 'after': after}))

    def event_states(self) -> dict:
        return self._request('GET', self._project_path + '/event-states')

    def milestones(self) -> dict:
        return self._request('GET', self._project_path + '/milestones')

    def concerns(self) -> dict:
        return self._request('GET', self._project_path + '/concerns')

    def questions(self) -> dict:
        result = self._request('GET', self._project_path + '/questions', envelope=False)
        if not isinstance(result.get('questions'), list) or not isinstance(result.get('statuses'), dict):
            raise AgentClientError('unexpected question response schema')
        return result

    def submit_results(self, results: dict) -> dict:
        return self._request('POST', self._project_path + '/results', results)

    def result_history(self) -> dict:
        return self._request('GET', self._project_path + '/results')

    def current_result(self) -> dict:
        return self._request('GET', self._project_path + '/results/current')

    def assess_question(self, watch_id: str, status: str, note: str, idempotency_key: str,
                        event_ids: list[str] | None = None, material_ids: list[str] | None = None) -> dict:
        result = self._request('POST', self._project_path + '/questions/' + quote(watch_id, safe='') + '/assessments', {
            'status': status, 'note': note, 'idempotency_key': idempotency_key,
            'event_ids': event_ids or [], 'material_ids': material_ids or []}, envelope=False)
        if result.get('actor') != 'agent' or result.get('watch_id') != watch_id:
            raise AgentClientError('unexpected assessment response schema')
        return result

    def concern_feedback(self, concern_id, status, reason, expected_version, idempotency_key) -> dict:
        return self._request('POST', self._project_path + '/concern-feedback', {
            'concern_id': concern_id, 'status': status, 'reason': reason,
            'expected_version': expected_version, 'idempotency_key': idempotency_key})


def client_from_env() -> AgentClient:
    if os.environ.get('PROJECT_WATCH_AGENT_CONFIG'):
        from pathlib import Path

        try:
            config = json.loads(Path(os.environ['PROJECT_WATCH_AGENT_CONFIG']).read_text(encoding='utf-8'))
            return AgentClient(config['url'], config['instance_id'], config['project_id'], config['token'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise AgentClientError('Local Agent connection file is missing or invalid; reconnect this project') from exc
    return AgentClient(os.environ.get("PROJECT_WATCH_AGENT_URL", ""),
                       os.environ.get("PROJECT_WATCH_AGENT_INSTANCE", ""),
                       os.environ.get("PROJECT_WATCH_AGENT_PROJECT", ""),
                       os.environ.get("PROJECT_WATCH_AGENT_TOKEN", ""))


def build_studio_mcp_server(client: AgentClient):
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
    except ImportError as exc:
        raise RuntimeError('Install the optional "mcp" extra for the Studio bridge') from exc

    server = MCPServer("ProjectWatch Studio", instructions=(
        "Bound to one local Studio instance and project. Read configuration version before writing. "
        "Status changes are saved, not searches or completed collection. No arbitrary HTTP or SQL tools."
    ))

    def invoke(function, *args):
        try:
            return function(*args)
        except AgentClientError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def studio_runtime() -> dict[str, object]:
        """Read this instance's implemented capabilities. No network search."""
        return invoke(client.runtime)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_health() -> dict[str, object]:
        """Read saved source coverage and bounded task states; no retry or raw provider errors."""
        return invoke(client.health)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def material_package() -> dict[str, object]:
        """Read bounded facts/identities/relations/evidence metadata in one snapshot. No raw files, bodies or AI briefs."""
        return invoke(client.material_package)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def project_overview() -> dict[str, object]:
        """Read stable project identity and current configuration version."""
        return invoke(client.project)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search_project(query: str = "") -> dict[str, object]:
        """Search only the granted project, not other local projects."""
        return invoke(client.search, query)

    @server.tool()
    def set_monitoring_status(status: str, expected_version: int, idempotency_key: str) -> dict[str, object]:
        """Persist active/paused. Does not cancel in-flight jobs or run collection."""
        return invoke(client.change_status, status, expected_version, idempotency_key)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def operation_receipt(operation_id: str) -> dict[str, object]:
        """Read saved-operation receipt separately from background work."""
        return invoke(client.receipt, operation_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def evidence_versions(limit: int = 20, cursor: str | None = None) -> dict[str, object]:
        """List immutable version metadata in a snapshot; internal material requires explicit grant scope."""
        return invoke(client.evidence, limit, cursor)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def evidence_part(version_id: str, part_index: int) -> dict[str, object]:
        """Read one exact stored fragment, preserving its original locator and scope."""
        return invoke(client.evidence_part, version_id, part_index)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def resolve_excerpt(version_id: str, part_index: int, start: int, end: int, quote_text: str) -> dict[str, object]:
        """Validate a verbatim quote against immutable version offsets; paraphrases are rejected."""
        return invoke(client.resolve_excerpt, version_id, part_index, start, end, quote_text)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_objects(limit: int = 20, after: str = "") -> dict[str, object]:
        """List permitted company/product/person identities; similar names are not merged."""
        return invoke(client.objects, limit, after)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_object(object_id: str) -> dict[str, object]:
        """Read identity version, aliases, provenance and scope."""
        return invoke(client.object_detail, object_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def object_facts(object_id: str) -> dict[str, object]:
        """Read source assertions scoped to this exact subject, not aggregated to its parent."""
        return invoke(client.object_facts, object_id)

    @server.tool()
    def maintain_object(object_record: dict[str, object], expected_project_version: int, idempotency_key: str,
                        object_id: str | None = None, expected_object_version: int | None = None) -> dict[str, object]:
        """Persist an explicit user identity correction. New identity is user-specified, not regulatory-verified.

        Read versions first. Internal data needs explicit internal grant. No collection is run by this write.
        """
        return invoke(client.maintain_object, object_record, expected_project_version, idempotency_key,
                      object_id, expected_object_version)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_changes(after: int = 0, limit: int = 20, through: int | None = None) -> dict[str, object]:
        """Read saved evidence lifecycle changes without refreshing sources. Carry snapshot_through while paging.

        A source-read failure is coverage loss, not company deterioration or business withdrawal.
        """
        return invoke(client.changes, after, limit, through)

    @server.tool()
    def submit_text_source(filename: str, text: str, expected_version: int, idempotency_key: str) -> dict[str, object]:
        """Save a bounded TXT document for local parsing. Internal write grant required.

        This does NOT authorize model disclosure; owner authorization for the current provider is separate.
        Read operation receipt and source_status; queued parsing is not completed analysis.
        """
        return invoke(client.submit_text, filename, text, expected_version, idempotency_key)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def source_status(source_id: str) -> dict[str, object]:
        """Read actual parser state and current model permission. No provider call."""
        return invoke(client.source_status, source_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_relationships(limit: int = 20, after: str = "") -> dict[str, object]:
        """Read scoped candidates/user-selected relations and explicit exclusions. Not investment judgements."""
        return invoke(client.relationships, limit, after)

    @server.tool()
    def correct_relationships(changes: list[dict[str, object]], expected_version: int, idempotency_key: str) -> dict[str, object]:
        """Atomically include/exclude/reset scoped company/product relationships. Read stable object IDs first.

        User-selected is not evidence-verified. Exclusion survives research and aliases; reset preserves history.
        Does not start paid research or silently refresh sources.
        """
        return invoke(client.correct_relationships, changes, expected_version, idempotency_key)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_observations(limit: int = 50, after: str = '') -> dict[str, object]:
        """Read saved scoped commercial roles/policy conditions, never implied sales or legal/investment conclusions."""
        return invoke(client.observations, limit, after)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_event_states() -> dict[str, object]:
        """Read saved attributed event stages, dated changes and uncertainty; no paid calls or assumed stage order."""
        return invoke(client.event_states)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_milestones() -> dict[str, object]:
        """Read saved original/adjusted/actual/delay source assertions and history, not cash-gap estimates."""
        return invoke(client.milestones)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitor_concerns() -> dict[str, object]:
        """Read cited neutral changes and human feedback; source failures are not business decline."""
        return invoke(client.concerns)

    @server.tool()
    def maintain_concern(concern_id: str, status: str, reason: str, expected_version: int, idempotency_key: str) -> dict[str, object]:
        """Save explicit human concern feedback (open/resolved/dismissed) with CAS/idempotency, no paid calls."""
        return invoke(client.concern_feedback, concern_id, status, reason, expected_version, idempotency_key)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def tracked_questions() -> dict[str, object]:
        """Read permitted tracked questions, source references and separate human/system/Agent histories."""
        return invoke(client.questions)

    @server.tool()
    def submit_monitoring_results(results: MonitoringResults) -> dict[str, object]:
        """Publish an attributed, cited news/company/brief/deep bundle. No provider calls.

        Read project_overview for expected_version; use a stable idempotency_key.
        The /results OpenAPI contract defines sources, news, company, brief and deep.
        Internal sources need allow_internal and an existing document locator.
        Never submit vendor records as public news. Human notes remain untouched.
        """
        return invoke(client.submit_results, results.model_dump(mode='json'))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def monitoring_result_history() -> dict[str, object]:
        """List permitted external Agent batch metadata. No internal content export or I/O."""
        return invoke(client.result_history)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def current_monitoring_result() -> dict[str, object]:
        """Read the latest permitted Agent report and its source catalog to continue/revise a conversation."""
        return invoke(client.current_result)

    @server.tool()
    def write_question_assessment(watch_id: str, status: str, note: str, idempotency_key: str,
                                  event_ids: list[str] | None = None, material_ids: list[str] | None = None) -> dict[str, object]:
        """Append an Agent judgement shown on the project page. Never overwrite the person's judgement.

        Read tracked_questions first; use its status codes and permitted evidence IDs. This does not run a model.
        """
        return invoke(client.assess_question, watch_id, status, note, idempotency_key, event_ids, material_ids)

    return server


def main() -> None:
    build_studio_mcp_server(client_from_env()).run(transport="stdio")


if __name__ == "__main__":
    main()
