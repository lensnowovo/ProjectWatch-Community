"""Pinned official DSH subprocess, with journalled model traffic and public tools.

No provider keys are passed to DSH. This is a capability boundary, not an OS sandbox.
The local gateway blocks all further paid work after an unresolved response.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import closing, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

from project_watch.studio.dsh_bootstrap import PROMPT, TOOLS, PublicBootstrap, ToolValidationError
from project_watch.studio.http import Http, HttpError

DSH_VERSION = "0.2.1-rc.2"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MODEL_TIMEOUT_SECONDS = 300
# Gateway journals a complete non-streaming response before emitting SDK SSE.
# Do not let the SDK's idle timer race the upstream request's wait.
CLIENT_TIMEOUT_MS = (MODEL_TIMEOUT_SECONDS + 60) * 1000


def compact_context_messages(messages):
    # OpenAI-compatible adapters may retain a single text block instead of a
    # string. Process its JSON identically while preserving the wire envelope.
    normalized, wrapped = list(messages), {}
    for index, message in enumerate(messages):
        content = message.get("content")
        if (message.get("role") == "tool" and isinstance(content, list) and len(content) == 1
                and isinstance(content[0], dict) and content[0].get("type") == "text"
                and isinstance(content[0].get("text"), str)):
            normalized[index] = {**message, "content": content[0]["text"]}
            wrapped[index] = content[0]
    result = _compact_context_strings(normalized)
    for index, block in wrapped.items():
        result[index] = {**result[index], "content": [{**block, "text": result[index]["content"]}]}
    return result


def _compact_context_strings(messages):
    """Lossless forward references; appending a turn never rewrites its prefix.

    Retain first occurrences verbatim, citation numbers and all protocol slots.
    Only exact same-source excerpts/entries are shared. Unknown formats survive.
    This is transport-only: durable evidence and harness sessions remain intact.
    """
    result = list(messages)
    names, entries, excerpts, reads, maps = {}, {}, {}, {}, {}
    for index, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            names[call.get("id")] = call.get("function", {}).get("name")
        call_id = message.get("tool_call_id")
        name = names.get(call_id)
        if message.get("role") != "tool" or name not in ("pw_context", "pw_read"):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        replacement = data
        if name == "pw_context" and isinstance(data.get("numbered"), dict) and data.get("fingerprint"):
            map_identity = json.dumps(data["numbered"], sort_keys=True, ensure_ascii=False)
            if data["numbered"] and map_identity in maps:
                replacement = {k: v for k, v in data.items() if k != "numbered"}
                replacement["numbered_same_as"] = {"tool_call_id": maps[map_identity]}
                result[index] = {**message, "content": json.dumps(replacement, ensure_ascii=False)}
                continue
            maps[map_identity] = call_id
            numbered = {}
            for ref, entry in data["numbered"].items():
                if not isinstance(entry, dict) or "same_as" in entry or "context_same_as" in entry:
                    numbered[ref] = entry
                    continue
                identity = json.dumps(entry, sort_keys=True, ensure_ascii=False)
                pointer = {"tool_call_id": call_id, "ref": ref}
                if identity in entries:
                    numbered[ref] = {"same_as": entries[identity]}
                    continue
                entries[identity] = pointer
                numbered[ref] = entry
                context, url = entry.get("context"), entry.get("url")
                if isinstance(context, str) and len(context) > 200 and isinstance(url, str) and url:
                    key = (url, context)
                    if key in excerpts:
                        numbered[ref] = {k: v for k, v in entry.items() if k != "context"}
                        numbered[ref]["context_same_as"] = excerpts[key]
                    else:
                        excerpts[key] = pointer
            replacement = {**data, "numbered": numbered}
        elif name == "pw_read" and isinstance(data.get("excerpt"), str) and len(data["excerpt"]) > 200:
            # Compare the whole result, including scope/version/window/date.
            identity = json.dumps(data, sort_keys=True, ensure_ascii=False)
            if identity in reads:
                replacement = {k: v for k, v in data.items() if k != "excerpt"}
                replacement["excerpt_same_as"] = {"tool_call_id": reads[identity]}
            else:
                reads[identity] = call_id
        if replacement != data:
            result[index] = {**message, "content": json.dumps(replacement, ensure_ascii=False)}
    return compact_source_excerpts(result)


def compact_source_excerpts(messages):
    """Share exact same-URL substrings across search/read/context, with provenance.

    Never remove a protocol message or its metadata. Pointers only address an
    earlier verbatim text, so appended messages cannot rewrite the prefix.
    Durable source text and citation validation remain completely unchanged.
    """
    result, names, texts, source_urls = list(messages), {}, {}, {}
    for index, message in enumerate(messages):
        for call in message.get("tool_calls") or []:
            names[call.get("id")] = call.get("function", {}).get("name")
        call_id = message.get("tool_call_id")
        name = names.get(call_id)
        if message.get("role") != "tool" or name not in ("pw_search", "pw_explore", "pw_read", "pw_context", "pw_record"):
            continue
        if not isinstance(message.get("content"), str):
            continue
        try:
            data = json.loads(message["content"])
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        nodes = []
        if name in ("pw_search", "pw_explore") and isinstance(data.get("sources"), list):
            nodes = [(node, "excerpt", ["sources", n, "excerpt"]) for n, node in enumerate(data["sources"])]
            for source in data["sources"]:
                if isinstance(source, dict) and isinstance(source.get("id"), str):
                    # An ID is scoped to this transcript, not a global URL alias.
                    source_urls[source["id"]] = source.get("url")
        elif name == "pw_read":
            nodes = [(data, "excerpt", ["excerpt"])]
        elif name == "pw_context" and isinstance(data.get("numbered"), dict):
            for ref, node in data["numbered"].items():
                nodes.append((node, "context", ["numbered", ref, "context"]))
                if isinstance(node, dict):
                    nodes.extend((excerpt, "excerpt", ["numbered", ref, "association_excerpts", n, "excerpt"])
                                 for n, excerpt in enumerate(node.get("association_excerpts") or []))
        elif name == "pw_record" and data.get("error") == "record_validation":
            nodes = [(data, "suggested_quote", ["suggested_quote"])]
            nodes.extend((issue, "suggested_quote", ["issues", n, "suggested_quote"])
                         for n, issue in enumerate(data.get("issues") or []))
        changed = False
        for node, field, path in nodes:
            if not isinstance(node, dict):
                continue
            value, url = node.get(field), node.get("url")
            if name == "pw_record":
                url = source_urls.get(data.get("source_id"))
            if not isinstance(value, str) or len(value) <= 120 or not isinstance(url, str) or not url:
                continue
            found = next(((text, pointer, text.find(value)) for text, pointer in texts.get(url, [])
                          if value in text and (name != "pw_read" or pointer["path"][0] == "sources")), None)
            if found:
                _text, pointer, offset = found
                reference = {**pointer, "start": offset, "end": offset + len(value)}
                # A pointer should not cost more than the exact text it replaces.
                if len(json.dumps(reference, ensure_ascii=False)) + len("_same_as") >= len(json.dumps(value, ensure_ascii=False)):
                    continue
                node.pop(field)
                node[field + "_same_as"] = reference
                changed = True
            else:
                texts.setdefault(url, []).append((value, {"tool_call_id": call_id, "path": path}))
        if changed:
            result[index] = {**message, "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))}
    return result


def runtime_entry(root: Path) -> Path:
    package = root / "node_modules" / "@deepseek-ai" / "dsh"
    metadata = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if metadata.get("version") != DSH_VERSION:
        raise ValueError("dsh_runtime_version_mismatch")
    return package / "lib" / "bin.js"


def plugin_file_uri(path) -> str:
    r"""Node file URLs cannot carry Windows' extended-length device prefix.

    Embedded Python can resolve installed modules to \\?\ paths. Keep the
    filesystem path intact elsewhere, removing that prefix only for URL encoding.
    """
    if isinstance(path, PureWindowsPath):
        value = str(path)
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
            if len(value) < 3 or not value[0].isalpha() or value[1:3] != ':\\':
                raise ValueError('dsh_plugin_path_invalid')
        return PureWindowsPath(value).as_uri()
    return path.as_uri()


def profile_patch(port: int, model: str, output_tokens: int) -> str:
    plugin = plugin_file_uri(Path(__file__).with_name("dsh_tools.mjs").resolve())
    disabled = ["persistent-bash", "persistent-pwsh", "tool-fs", "tool-working-directory",
                "session-log-deepseek", "plugin-package-inventory-deepseek", "llm-deepseek"]
    # pi-ai and the generic retry executor share this explicit zero-retry policy.
    route = {"api": "openai-completions", "baseURL": f"http://127.0.0.1:{port}/v1",
             "apiKeyEnv": "PW_DSH_GATEWAY_TOKEN", "retryPolicy": {"mode": "normal", "maxRetries": 0},
             "timeoutMs": CLIENT_TIMEOUT_MS, "streamIdleTimeoutMs": CLIENT_TIMEOUT_MS,
             "models": [{"id": model, "maxTokens": output_tokens, "contextWindow": 1000000,
                         "reasoningEfforts": False}]}
    patch = [{"id": item, "disabled": True} for item in disabled]
    patch += [{"insert": [
        {"id": "pw-model", "name": "@deepseek-ai/dsh-llm-pi-ai",
         "config": {"providers": {"projectwatch": route}}},
        {"id": "pw-tools", "name": plugin}]}]
    return json.dumps(patch)


def stream_response(data):
    """Convert a completed, journalled Chat Completions response to SDK SSE."""
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0].get("message"), dict):
        raise ValueError("invalid_model_response")
    message = choices[0]["message"]
    delta = dict(message)
    if delta.get("tool_calls"):
        delta["tool_calls"] = [dict(call, index=index) for index, call in enumerate(delta["tool_calls"])]
    common = {"id": data.get("id", "pw"), "object": "chat.completion.chunk",
              "created": data.get("created", 0), "model": data.get("model", "configured")}
    frames = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": choices[0].get("finish_reason", "stop")}]}]
    if isinstance(data.get("usage"), dict):
        frames.append({**common, "choices": [], "usage": data["usage"]})
    return "".join("data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames).encode() + b"data: [DONE]\n\n"


class Gateway:
    def __init__(self, research: PublicBootstrap, model, http):
        if model.protocol != "openai" or not model.ready:
            raise ValueError("DSH bootstrap currently requires a configured OpenAI-compatible model")
        self.research, self.model, self.http = research, model, http
        self.token = secrets.token_urlsafe(32)
        self.model_requests = 0
        self.advertised_tools = set()
        self.server = None
        self.closing = threading.Event()
        research.judgment_reviewer = self.review_judgment
        research.record_reviewer = self.review_judgment

    def review_judgment(self, payload):
        from project_watch.studio.dsh_judgment_review import (
            CONSISTENCY_SYSTEM,
            OUTPUT_TOKENS,
            SYSTEM,
            ReviewFieldAliases,
        )
        from project_watch.studio.llm import structured_options

        with self.research.lock, self.research.store.write_scope(self.research.execution._guard):
            if self.closing.is_set():
                raise HttpError('execution_stopped')
            self.research.guard()
            system = CONSISTENCY_SYSTEM if payload.get('phase') == 'consistency' else SYSTEM
            if payload.get('purpose') == 'public_record_admission' and payload.get('phase') != 'consistency':
                from project_watch.studio.dsh_record_review import GUIDANCE
                system += GUIDANCE
            aliases = ReviewFieldAliases(payload)
            body = {'model': self.model.model, 'stream': False, 'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps(aliases.payload, ensure_ascii=False)}]}
            body['max_tokens'] = min(self.model.max_output_tokens, OUTPUT_TOKENS)
            options = structured_options(self.model)
            if 'reasoning_effort' in options:
                body['reasoning_effort'] = options['reasoning_effort']
            if 'thinking' in options:
                body['thinking'] = {'type': 'enabled' if options['thinking'] else 'disabled'}
            if options.get('json_object'):
                body['response_format'] = {'type': 'json_object'}
            self.research.store.set_stage(self.research.execution.job['id'], 'DSH 复核判断依据')
            data = self._send_model(body)
            try:
                choice = data['choices'][0]
                message = choice['message']
                if choice.get('finish_reason') != 'stop' or message.get('tool_calls'):
                    raise ValueError('review_response_incomplete')
                return aliases.decode(json.loads(message['content']))
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                self.research.hold('judgment_review_invalid')
                raise HttpError('judgment_review_invalid') from exc

    def model_call(self, payload):
        from project_watch.studio.recovery import dsh_model_continuation

        with self.research.lock, self.research.store.write_scope(self.research.execution._guard):
            slot = dsh_model_continuation(self.research.pipeline, self.research.execution)
            # Only model admission can use a pending slot. Tools remain blocked.
            self.research.recovery_admission = bool(slot)
            scope = self.research.execution.model_continuation(slot["previous_key"]) if slot else nullcontext()
            try:
                with scope:
                    result = self._model_call(payload)
                if slot:
                    self.research.execution.checkpoint("dsh_hold", {})
                    plan = getattr(self.research.pipeline, "monitoring_plan", None)
                    if plan:
                        plan.explorations.resume_collected(self.research.project_id, self.research.execution.job["id"])
                return result
            finally:
                self.research.recovery_admission = False

    def _model_call(self, payload):
        with self.research.lock, self.research.store.write_scope(self.research.execution._guard):
            if self.closing.is_set():
                raise HttpError("execution_stopped")
            self.research.guard()
            if self.research.state["finished"]:
                # DSH may ask for a final verbal response after pw_finish. Avoid
                # another billable call; structured submission is the product result.
                return {"choices": [{"message": {"role": "assistant", "content": "结果已保存。"},
                                     "finish_reason": "stop"}]}
            if not isinstance(payload, dict) or payload.get("model") != self.model.model:
                raise ValueError("model route mismatch")
            names = {tool.get("function", {}).get("name") for tool in payload.get("tools", [])}
            if not names <= set(self.research.tools):
                raise ValueError("unexpected runtime tool exposure")
            if not names:
                self.research.hold('dsh_tools_unavailable')
                raise HttpError('dsh_tools_unavailable')
            self.advertised_tools.update(names)
            from project_watch.studio.dsh_closeout import finish_saved_monitor
            if 'pw_finish' in names and finish_saved_monitor(self.research):
                return {"choices": [{"message": {"role": "assistant", "content": "结果已保存。"},
                                     "finish_reason": "stop"}]}
            body = {k: payload[k] for k in ("model", "messages", "tools", "tool_choice",
                                           "temperature", "max_tokens") if k in payload}
            from project_watch.studio.dsh_closeout import prepare_model
            body = prepare_model(self.research, body)
            original = body.get("messages", [])
            body["messages"] = compact_context_messages(original)
            before = len(json.dumps(original, ensure_ascii=False))
            after = len(json.dumps(body["messages"], ensure_ascii=False))
            counts = self.research.execution.read_checkpoint("dsh_context_transport") or {
                "requests": 0, "original_chars": 0, "sent_chars": 0}
            counts = {"requests": counts["requests"] + 1,
                      "original_chars": counts["original_chars"] + before,
                      "sent_chars": counts["sent_chars"] + after}
            self.research.execution.checkpoint("dsh_context_transport", counts)
            body["stream"] = False
            # A million-token configured ceiling means no app-imposed cap here;
            # let the upstream model apply its own default, rather than negotiate
            # by resending rejected requests. Smaller explicit ceilings survive.
            if self.model.max_output_tokens >= 1000000:
                body.pop("max_tokens", None)
            else:
                body["max_tokens"] = min(int(body.get("max_tokens") or self.model.max_output_tokens),
                                         self.model.max_output_tokens)
            self.research.store.set_stage(self.research.execution.job["id"], "DSH 研究中：选择下一步")
            return self._send_model(body)

    def _send_model(self, body):
        """Both generation and independent review share journal/budget/unknown handling."""
        with self.research.lock, self.research.store.write_scope(self.research.execution._guard):
            received = False
            try:
                self.model_requests += 1
                response = self.http.post_json(self.model.base_url.rstrip("/") + "/chat/completions", body,
                    headers={"Authorization": "Bearer " + self.model.api_key}, timeout=MODEL_TIMEOUT_SECONDS)
                received = True
                if response.status != 200:
                    self.research.hold("model_http_" + str(response.status))
                    raise HttpError("model_http_" + str(response.status))
                data = response.json()
                # Validate before handing a final response to the harness.
                stream_response(data)
                return data
            except Exception as exc:
                if any(u["unknown"] for u in self.research.execution.usage().values()):
                    self.research.hold("request_outcome_unknown")
                elif (self.research.execution.read_checkpoint("dsh_hold") or {}).get("code") in (None, "request_outcome_unknown"):
                    code = ("model_response_invalid" if received else
                            exc.code if isinstance(exc, HttpError) else "model_transport_failed")
                    self.research.hold(code)
                raise

    def start(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if gateway.closing.is_set():
                    self.reply(409, {"error": "execution_stopped", "terminal": True})
                    return
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + gateway.token):
                    self.reply(401, {"error": "unauthorized"})
                    return
                # Do not admit browser-originated requests, even if misconfigured.
                if self.headers.get("Origin"):
                    self.reply(403, {"error": "origin_denied"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_REQUEST_BYTES:
                        raise ValueError("invalid request size")
                    payload = json.loads(self.rfile.read(size))
                    if self.path == "/v1/chat/completions":
                        data = gateway.model_call(payload)
                        if payload.get("stream"):
                            self.reply(200, stream_response(data), "text/event-stream")
                        else:
                            self.reply(200, data)
                    elif self.path.startswith("/tools/") and self.path[7:] in gateway.research.tools:
                        self.reply(200, gateway.research.tool(self.path[7:], payload))
                    else:
                        self.reply(404, {"error": "route_denied"})
                except ValueError as exc:
                    # Only bounded local validation messages, no provider prose.
                    # Persist a bounded validation trace so rejected tool attempts
                    # cannot disappear behind a generic "thinking" stage.
                    if self.path.startswith("/tools/"):
                        research = gateway.research
                        try:
                            with research.lock, research.store.write_scope(research.execution._guard):
                                prior = research.execution.read_checkpoint("dsh_validation_errors") or []
                                feedback = exc.feedback() if isinstance(exc, ToolValidationError) else {
                                    "error": str(exc)[:160], "terminal": False}
                                research.execution.checkpoint("dsh_validation_errors", [*prior, {
                                    "tool": self.path[7:][:40], **feedback}][-100:])
                        except Exception:
                            self.reply(409, {"error": "execution_stopped", "terminal": True})
                            return
                    self.reply(400, exc.feedback() if isinstance(exc, ToolValidationError) else {
                        "error": str(exc)[:160], "terminal": False})
                except Exception as exc:
                    code = getattr(exc, "code", "execution_stopped")
                    self.reply(409, {"error": code, "terminal": True})

            def reply(self, status, value, content_type="application/json"):
                body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Upstream receipt remains journalled even if DSH disconnects.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server.server_port

    def close(self):
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()  # Drain in-flight requests; never kill them for UI timeouts.
        self.thread.join()


def drain_gateway(research, gateway):
    """Keep the existing lease alive while admitted calls finish journalling."""
    done = threading.Event()

    def heartbeat():
        while not done.is_set():
            if not research.store.heartbeat(research.execution.job["id"], research.execution.job["lease_token"]):
                return  # Lost ownership is never renewed with a new token.
            done.wait(10)

    keeper = threading.Thread(target=heartbeat, daemon=True)
    keeper.start()
    try:
        gateway.close()
    finally:
        done.set()
        keeper.join()


def settled_error(research, error):
    # Resolve only AFTER drain. A late receipt may supersede SDK idle/error.
    if any(row["unknown"] for row in research.execution.usage().values()):
        return "request_outcome_unknown"
    hold = research.execution.read_checkpoint("dsh_hold")
    if hold:
        return hold["code"]
    return error or (None if research.state["finished"] else "dsh_no_valid_submission")


def guarded_http(research, original):
    """Stop before *every* dispatch, including a collector's provider fallback."""
    def send(*args):
        research.guard()
        try:
            return original.transport(*args)
        except Exception:
            if any(row["unknown"] for row in research.execution.usage().values()):
                research.hold("request_outcome_unknown")
                raise HttpError("request_outcome_unknown") from None
            raise
    return Http(send)


def child_environment(directory: Path, port: int, token: str, *, prompt=PROMPT, tools=TOOLS):
    keep = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(DSH_HOME=str(directory / "home"), DSH_TELEMETRY_MODE="DISABLED",
               DSH_SYSTEM_PROMPT=prompt, PW_DSH_TOOLS=json.dumps(tools),
               PW_DSH_GATEWAY=f"http://127.0.0.1:{port}",
               PW_DSH_GATEWAY_TOKEN=token, NO_PROXY="127.0.0.1,localhost")
    return env


def run_dsh(research, gateway, runtime_root: Path, directory: Path, *, timeout=3600):
    entry = runtime_entry(runtime_root)
    node = shutil.which("node")
    if not node:
        raise ValueError("Node.js runtime is required for DSH")
    directory.mkdir(parents=True, exist_ok=True)
    port = gateway.start()
    patch = directory / "bootstrap.patch.yml"
    patch.write_text(profile_patch(port, gateway.model.model, gateway.model.max_output_tokens), encoding="utf-8")
    lines = queue.Queue()
    events = []
    process = None
    error = None
    runtime_failure = None
    initialized = prompted = ended = False
    try:
        # Diagnostics and session logs can contain public research text; private dir only.
        with (directory / "stderr.log").open("a", encoding="utf-8") as diagnostics:
            process = subprocess.Popen([node, str(entry), "--profile", "sdk-minimal", "--patch", str(patch)],
                cwd=directory, env=child_environment(directory, port, gateway.token,
                    prompt=research.prompt, tools=research.tools),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            def receive():
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)

            threading.Thread(target=receive, daemon=True).start()

            def send(identifier, method, params=None):
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method,
                                                "params": params or {}}, ensure_ascii=False) + "\n")
                process.stdin.flush()

            send(1, "initialize", {"cwd": str(directory), "provider": "projectwatch", "model": gateway.model.model})
            deadline = time.monotonic() + timeout
            heartbeat = 0
            while time.monotonic() < deadline:
                if time.monotonic() > heartbeat:
                    if not research.store.heartbeat(research.execution.job["id"], research.execution.job["lease_token"]):
                        raise RuntimeError("job lease lost")
                    heartbeat = time.monotonic() + 10
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("id") == 1:
                    initialized = message.get("result", {}).get("serverInfo", {}).get("name") == "deepseek-harness-sdk-runtime"
                    if not initialized:
                        error = "dsh_initialization_failed"
                        break
                    send(2, "session/prompt", {"sessionId": research.execution.job["id"], "contentBlocks": [{
                        "type": "text", "text": "确认主体：" + research.project["legal_name"] + "。简称：" +
                        research.project["name"] + "。" + research.task}]})
                elif message.get("id") == 2:
                    prompted = bool(message.get("result", {}).get("messageId"))
                    if not prompted:
                        error = "dsh_prompt_rejected"
                        break
                elif message.get("method") == "session.event":
                    event = message.get("params", {}).get("event", {})
                    events.append(str(event.get("type", "unknown")))
                    if event.get("type") == "turn/end":
                        reason = (event.get("data") or {}).get("reason") or {}
                        if reason.get("kind") == "error":
                            code = (reason.get("error") or {}).get("code")
                            runtime_failure = "dsh_model_timeout" if code == "TIMEOUT" else "dsh_runtime_error"
                elif message.get("method") == "session.status" and prompted:
                    if message.get("params", {}).get("status") == "idle":
                        ended = True
                        break
            if not ended and not error:
                error = "dsh_interrupted_or_deadline"
            gateway.closing.set()
            try:
                send(3, "shutdown")
                process.wait(timeout=20)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                # DSH is our isolated child. Gateway requests are drained below.
                process.terminate()
                process.wait(timeout=20)
    finally:
        drain_gateway(research, gateway)
        if process is not None:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
            process.stdin.close()
            process.stdout.close()
    error = settled_error(research, error or runtime_failure)
    research.execution.checkpoint("dsh_runtime", {
        "version": DSH_VERSION, "initialized": initialized, "prompted": prompted, "idle": ended,
        "event_types": sorted(set(events)), "advertised_tools": sorted(gateway.advertised_tools),
        "gateway_model_requests": gateway.model_requests, "runtime_failure": runtime_failure,
        "drained": True, "error": error})
    if error:
        raise HttpError(error)
    return research.state["finished"]


def recover_authorized_site(pipeline, execution):
    """Settle an exact owner-approved legacy GET before any DSH model dispatch."""
    from project_watch.studio import monitor, recovery

    slot = recovery.dsh_site_recovery(pipeline, execution)
    if not slot:
        with closing(pipeline.store._connect()) as db:
            pending_site = db.execute('''SELECT 1 FROM studio_call_recoveries r
                JOIN studio_job_calls c ON c.job_id=r.job_id AND c.call_key=r.previous_key
                WHERE r.job_id=? AND r.consumed_at IS NULL AND c.kind='site' AND c.status='unknown' ''',
                (execution.job['id'],)).fetchone()
            blockers = recovery.other_pending_requests(db, execution.job['project_id'], execution.job['id'])
        if pending_site and blockers:
            execution.checkpoint('dsh_site_recovery_blocked', {'blocking_requests': blockers})
            raise HttpError('request_outcome_unknown', detail='site_recovery_blocked: ' + recovery.pending_reason(blockers))
        return
    project = pipeline.store.get_project(execution.job['project_id'])
    transport = getattr(pipeline.reader(), 'transport', None)
    if transport is None or not hasattr(transport, 'get_response'):
        raise HttpError('recovery_reader_unavailable')
    pipeline.store.set_stage(execution.job['id'], '阅读官网（已授权恢复）')
    with execution.site_recovery(pipeline, slot):
        report = monitor.check_sites(pipeline.monitor, transport, execution.job['project_id'],
                                     project.get('website'), execution=execution, recovery_key=slot['base_key'])
    execution.checkpoint('dsh_site_recovery', {'previous_key': slot['previous_key'],
                         'next_key': slot['next_key'], 'response_status': report['response_status']})
    hold = execution.read_checkpoint('dsh_hold')
    if (hold or {}).get('code') == 'request_outcome_unknown' and not any(
            value['unknown'] for value in execution.usage().values()):
        execution.checkpoint('dsh_hold', {})


def run_bootstrap(pipeline, execution, runtime_root: Path):
    from project_watch.studio import collection_host

    admission = collection_host.admit(execution, mode='first')
    if admission['enabled']:
        collection_host.limit_budgets(execution, admission['mode'])
    recover_authorized_site(pipeline, execution)
    settings, http, _llm, collectors = pipeline.tools()
    research = PublicBootstrap(pipeline, execution, collectors, pipeline.reader())
    if research.state["finished"]:
        research.guard()
        return research.state["finished"]
    http = guarded_http(research, http)
    collectors.http = http
    gateway = Gateway(research, settings.model, http)
    collection_host.prepare(research)
    directory = session_directory(research)
    return run_dsh(research, gateway, runtime_root, directory)


def run_monitor(pipeline, execution, runtime_root: Path, *, analysis_only=False):
    from project_watch.studio import collection_host
    from project_watch.studio.dsh_monitor import PublicMonitor

    research_class = PublicMonitor
    if not analysis_only:
        request = execution.read_checkpoint('collection_request_v1') or {}
        mode = request.get('mode', 'incremental')
        if mode not in ('incremental', 'refresh', 'topic'):
            raise ValueError('invalid admitted collection mode')
        if mode == 'topic':
            from project_watch.studio.dsh_topic import PublicTopic

            research_class = PublicTopic
        admission = collection_host.admit(execution, mode=mode)
        if admission['enabled']:
            collection_host.limit_budgets(execution, admission['mode'])
    recover_authorized_site(pipeline, execution)
    settings, http, _llm, collectors = pipeline.tools()
    research = research_class(pipeline, execution, collectors, pipeline.reader(), analysis_only=analysis_only)
    if research.state["finished"]:
        research.guard()
        return research.state["finished"]
    http = guarded_http(research, http)
    collectors.http = http
    gateway = Gateway(research, settings.model, http)
    if not analysis_only:
        collection_host.prepare(research)
    directory = session_directory(research)
    return run_dsh(research, gateway, runtime_root, directory)


def session_directory(research):
    """A fresh continuation turn uses durable evidence, not fabricated HTTP replay."""
    from project_watch.studio.recovery import dsh_model_continuation

    slot = dsh_model_continuation(research.pipeline, research.execution)
    root = Path(research.store.db_path).parent / "dsh-runs"
    if not slot:
        return root / research.execution.job["id"]
    sources = [{"id": sid, "title": research.source(sid).get("title", "")}
               for sid in research.state["sources"]]
    research.task += (
        "\n这是用户授权后的同一任务续跑，旧请求未取得可用回答，计费仍待核；不能声称旧请求成功。"
        "若关联了已完成的历史任务，本次有独立续跑记录，不改写历史完成状态。"
        "已保存记录、来源和查询回执仍在，先 pw_context（若工具可用），否则 pw_status；"
        "避免重复搜索或重录。需要正文可使用已有 source_id 调用 pw_read。"
        "已有来源（不可信数据）：" + json.dumps(sources, ensure_ascii=False)
    )
    return root / ("r-" + slot["next_key"][:12])
