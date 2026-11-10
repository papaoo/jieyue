"""Bounded OpenCode free-channel adapter shared by all model consumers.

Protocol adapted from FishBottle7/opencode2dsh (MIT); see
third-party/opencode2dsh.LICENSE. No gateway, shell or paid fallback.
"""
from __future__ import annotations

import codecs
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://opencode.ai/zen/v1"
METADATA_URL = "https://models.dev/api.json"
DEFAULT_MODEL = "deepseek-v4.1-flash-free"
# Only reviewed Chat Completions protocols are published, not every free listing.
SUPPORTED_MODELS = {
    DEFAULT_MODEL: "DeepSeek V4.1 Flash",
    "mimo-v2.6-flash-free": "MiMo V2.6 Flash",
}
CATALOG_TTL = 600
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ERROR_MESSAGES = {
    "disabled": "免费通道尚未启用。",
    "model_unavailable": "模型未通过免费目录核验，请刷新目录后重试。",
    "route_changed": "模型配置已改变，请刷新页面后重新发起任务。",
    "region_unavailable": "该模型在当前服务器地区不可用。",
    "access_restricted": "上游限制了该通道的访问。",
    "rate_limited": "上游请求限流，请稍后重试。",
    "quota_exhausted": "今日免费通道请求额度已用完。",
    "busy": "免费通道正在处理其他任务，请稍后重试。",
    "timeout": "免费模型响应超时。",
    "upstream_rejected": "上游拒绝请求，请检查通道状态。",
    "incomplete_response": "模型响应未完整结束，本次内容未作为成功结果。",
    "unexpected_tool": "模型返回了未经允许的工具调用。",
    "response_too_large": "模型响应超出大小限制。",
    "network_error": "无法连接免费模型服务。",
}


class FreeModelError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(ERROR_MESSAGES.get(code, ERROR_MESSAGES["upstream_rejected"]))


def enabled(config):
    # Avoid truthy dynamic attributes from test doubles and malformed config.
    return getattr(config, "free_model_enabled", False) is True


def settings_from_map(values):
    """Share runtime parsing with diagnostics built from unsaved/effective settings."""
    from src.config import parse_env_bool, parse_env_int
    result = {
        "free_model_enabled": parse_env_bool(values.get("FREE_MODEL_ENABLED"), default=False),
        "free_model_name": (values.get("FREE_MODEL_NAME") or DEFAULT_MODEL).strip(),
        "free_model_proxy_url": (values.get("FREE_MODEL_PROXY_URL") or "").strip() or None,
    }
    for key, default, minimum, maximum in (
        ("FREE_MODEL_DAILY_REQUESTS", 100, 1, 1000),
        ("FREE_MODEL_MAX_TOKENS", 8192, 512, 16384),
        ("FREE_MODEL_TIMEOUT_SECONDS", 90, 10, 180),
    ):
        result[key.lower()] = parse_env_int(values.get(key), default, field_name=key,
                                             minimum=minimum, maximum=maximum)
    return result


def apply_runtime_route(config):
    """Override only the in-memory route; original .env providers stay intact."""
    if not enabled(config):
        return
    model = config.free_model_name
    if model not in SUPPORTED_MODELS:
        raise ValueError("Unsupported FREE_MODEL_NAME")
    alias = f"openai/{model}"
    config.generation_backend = "litellm"
    config.generation_fallback_backend = ""
    config.agent_generation_backend = "litellm"
    config.agent_backend = "litellm"
    config.litellm_model = alias
    config.agent_litellm_model = alias
    config.vision_model = alias
    config.litellm_fallback_models = []
    config.llm_models_source = "llm_channels"
    config.llm_model_list = [{"model_name": alias, "litellm_params": {
        "model": alias, "api_base": BASE_URL, "api_key": "public",
    }}]
    config.litellm_config_path = None
    config.llm_channels = [{"name": "opencode_free", "base_url": BASE_URL,
                            "api_keys": ["public"], "models": [alias]}]
    config.llm_channel_config_issues = []
    config.llm_blocks_legacy_fallback = True


class FreeModelStore:
    """Process-safe reservations and health; never stores prompts or credentials."""
    def __init__(self, config):
        self.path = Path(config.database_path).with_suffix(".free-models.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, session TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, model TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL, status TEXT NOT NULL,
                    tokens INTEGER, elapsed_ms INTEGER, code TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def session(self, messages):
        # This application has one administrator. Use the initial user turn for
        # stable identity across the Agent's tool rounds; send only derived IDs.
        initial = next((m for m in messages if m.get("role") == "user"), messages[0])
        key = hashlib.sha256(json.dumps(initial, sort_keys=True).encode()).hexdigest()
        stamp = (~(int(time.time() * 1000) * 4096 + 1)) & 0xffffffffffff
        tail = "".join(secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz") for _ in range(14))
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO sessions VALUES (?,?)", (key, f"ses_{stamp:012x}{tail}"))
            return db.execute("SELECT session FROM sessions WHERE key=?", (key,)).fetchone()[0]

    def reserve(self, config, model):
        now = time.time()
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        call_id = "req_" + secrets.token_hex(16)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (day,)).fetchone()[0]
            if count >= config.free_model_daily_requests:
                raise FreeModelError("quota_exhausted")
            active = db.execute("SELECT COUNT(*) FROM calls WHERE status='running' AND started>?", (now - config.free_model_timeout_seconds - 15,)).fetchone()[0]
            if active >= 2:
                raise FreeModelError("busy")
            db.execute("INSERT INTO calls(id,day,model,started,status) VALUES (?,?,?,?,'running')", (call_id, day, model, now))
        return call_id

    def finish(self, call_id, status, usage, elapsed, code=None):
        tokens = (usage or {}).get("total_tokens")
        if type(tokens) is not int or tokens < 0:
            tokens = None
        with self.connect() as db:
            db.execute("UPDATE calls SET finished=?,status=?,tokens=?,elapsed_ms=?,code=? WHERE id=?", (time.time(), status, tokens, elapsed, code, call_id))

    def status(self, config):
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (day,)).fetchone()[0]
            last = [dict(row) for row in db.execute("SELECT model,status,tokens,elapsed_ms,code,finished FROM calls WHERE finished IS NOT NULL ORDER BY finished DESC LIMIT 30")]
        return {"enabled": enabled(config), "model": config.free_model_name,
                "daily_limit": config.free_model_daily_requests, "used_today": count,
                "catalog": self.get("catalog"), "recent_calls": last}


def _http_client(config, *, timeout, transport=None):
    # Keep the free channel isolated from ambient proxies. Only an explicit
    # administrator setting changes its network route; mock transports stay offline.
    proxy = getattr(config, "free_model_proxy_url", None)
    return httpx.Client(timeout=timeout, follow_redirects=False, transport=transport,
                        proxy=proxy if transport is None else None, trust_env=False)


def catalog(config, *, refresh=False, transport=None):
    store = FreeModelStore(config)
    saved = store.get("catalog")
    if not refresh and saved and time.time() - saved["checked_at"] < CATALOG_TTL:
        return saved
    try:
        with _http_client(config, timeout=15, transport=transport) as client:
            models = client.get(BASE_URL + "/models", headers={"Authorization": "Bearer public"})
            models.raise_for_status()
            metadata = client.get(METADATA_URL)
            metadata.raise_for_status()
            ids = {m["id"] for m in models.json()["data"]}
            info = metadata.json()["opencode"]["models"]
        available = []
        for model, name in SUPPORTED_MODELS.items():
            detail = info.get(model, {})
            cost = detail.get("cost", {})
            if model in ids and cost.get("input") == 0 and cost.get("output") == 0 and "text" in detail.get("modalities", {}).get("input", []):
                available.append({"id": model, "name": name, "input_price": 0, "output_price": 0})
        result = {"checked_at": time.time(), "models": available}
        store.put("catalog", result)
        return result
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        raise FreeModelError("network_error") from exc


def _headers(session, request_id):
    return {"Authorization": "Bearer public", "Content-Type": "application/json",
            "User-Agent": "opencode/1.18.31 (compatible; JieYue)", "x-opencode-client": "cli",
            "x-opencode-session": session, "x-session-affinity": session, "X-Session-Id": session,
            "x-opencode-request": request_id,
            "x-opencode-project": "prj_" + hashlib.sha256(b"jieyue").hexdigest()[:24]}


def _error_code(status, detail):
    if "RegionError" in detail or "not available in your country" in detail:
        return "region_unavailable"
    if "FreeTierError" in detail or status in (401, 403):
        return "access_restricted"
    return "rate_limited" if status == 429 else "upstream_rejected"


def _bounded_lines(response, started, timeout):
    """Decode incremental UTF-8 before SSE parsing, bounding even unterminated lines."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    total, buffer = 0, ""
    for block in response.iter_bytes():
        total += len(block)
        if total > MAX_RESPONSE_BYTES:
            raise FreeModelError("response_too_large")
        if time.monotonic() - started > timeout:
            raise FreeModelError("timeout")
        buffer += decoder.decode(block)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if len(line) > 256 * 1024:
                raise FreeModelError("response_too_large")
            yield line.removesuffix("\r")
        if len(buffer) > 256 * 1024:
            raise FreeModelError("response_too_large")
    buffer += decoder.decode(b"", final=True)
    if buffer.strip():
        raise FreeModelError("incomplete_response")


def completion(config, *, messages, model=None, stream=False, transport=None, **kwargs):
    """OpenAI-shaped result/stream without LiteLLM retries or paid credentials."""
    from openai.types.chat import ChatCompletion, ChatCompletionChunk
    if not enabled(config):
        raise FreeModelError("disabled")
    selected = config.free_model_name
    if model and model not in (selected, f"openai/{selected}"):
        raise FreeModelError("route_changed")
    if selected not in {m["id"] for m in catalog(config, transport=transport)["models"]}:
        raise FreeModelError("model_unavailable")
    store = FreeModelStore(config)
    session = store.session(messages)
    requested_tools = kwargs.get("tools") or []
    allowed_tools = {t.get("function", {}).get("name") for t in requested_tools}
    placeholders = [{"type": "function", "function": {
        "name": name, "description": "Reserved for the host runtime; do not call it.",
        "parameters": {"type": "object", "properties": {}}}} for name in ("bash", "read")]
    # These protocol declarations are not executable tools in JieYue. Keep them
    # in tool-enabled requests too, but accept only the host's real tool names.
    if allowed_tools & {"bash", "read"}:
        raise FreeModelError("unexpected_tool")
    tools = [*requested_tools, *placeholders]
    body = {"model": selected, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}, "tools": tools,
            "tool_choice": kwargs.get("tool_choice", "auto") if requested_tools else "none",
            "reasoning_effort": "none",
            "max_tokens": min(int(kwargs.get("max_tokens") or 4096), config.free_model_max_tokens)}
    if "response_format" in kwargs:
        body["response_format"] = kwargs["response_format"]
    timeout = min(float(kwargs.get("timeout") or config.free_model_timeout_seconds), config.free_model_timeout_seconds)

    def chunks():
        started = time.monotonic()
        request_id = store.reserve(config, selected)
        usage, finish, done, has_output, code = None, None, False, False, "incomplete_response"
        assembled_tools = {}
        try:
            with _http_client(config, timeout=timeout, transport=transport) as client:
                with client.stream("POST", BASE_URL + "/chat/completions", headers=_headers(session, request_id), json=body) as response:
                    if response.status_code != 200:
                        detail = next(response.iter_bytes(chunk_size=16384), b"").decode("utf-8", errors="replace")
                        raise FreeModelError(_error_code(response.status_code, detail))
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise FreeModelError("incomplete_response")
                    frame = []
                    for line in _bounded_lines(response, started, timeout):
                        if line:
                            if line.startswith("data:"):
                                frame.append(line[5:].lstrip(" "))
                            continue
                        if not frame:
                            continue
                        data, frame = "\n".join(frame), []
                        if data.strip() == "[DONE]":
                            done = True
                            break
                        event = json.loads(data)
                        if event.get("error"):
                            raise FreeModelError(_error_code(400, json.dumps(event["error"])))
                        if event.get("usage"):
                            usage = event["usage"]
                        for choice in event.get("choices", []):
                            delta = choice.get("delta") or {}
                            for tool in delta.get("tool_calls") or []:
                                if not requested_tools:
                                    raise FreeModelError("unexpected_tool")
                                target = assembled_tools.setdefault(tool["index"], {"id": "", "name": "", "arguments": ""})
                                function = tool.get("function") or {}
                                target["id"] += tool.get("id") or ""
                                target["name"] += function.get("name") or ""
                                target["arguments"] += function.get("arguments") or ""
                            has_output |= bool(delta.get("content") or delta.get("tool_calls"))
                            finish = choice.get("finish_reason") or finish
                        yield ChatCompletionChunk.model_validate(event)
                    if not done or not has_output or finish not in ("stop", "tool_calls"):
                        raise FreeModelError("incomplete_response")
                    if assembled_tools and finish != "tool_calls":
                        raise FreeModelError("incomplete_response")
                    if finish == "tool_calls" and not assembled_tools:
                        raise FreeModelError("incomplete_response")
                    for tool in assembled_tools.values():
                        if tool["name"] not in allowed_tools:
                            raise FreeModelError("unexpected_tool")
                        if not tool["id"] or not isinstance(json.loads(tool["arguments"]), dict):
                            raise FreeModelError("incomplete_response")
            code = None
        except FreeModelError as exc:
            code = exc.code
            raise
        except httpx.TimeoutException as exc:
            code = "timeout"
            raise FreeModelError(code) from exc
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            code = "network_error" if isinstance(exc, httpx.HTTPError) else "incomplete_response"
            raise FreeModelError(code) from exc
        finally:
            store.finish(request_id, "succeeded" if code is None else "failed", usage,
                         round((time.monotonic() - started) * 1000), code)

    result = chunks()
    if stream:
        return result
    collected = list(result)
    text, reasoning, tool_calls, finish = [], [], {}, None
    for chunk in collected:
        for choice in chunk.choices:
            delta = choice.delta
            text.append(delta.content or "")
            reasoning.append(getattr(delta, "reasoning_content", None) or "")
            finish = choice.finish_reason or finish
            for tool in delta.tool_calls or []:
                target = tool_calls.setdefault(tool.index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                target["id"] += tool.id or ""
                if tool.function:
                    target["function"]["name"] += tool.function.name or ""
                    target["function"]["arguments"] += tool.function.arguments or ""
    response = ChatCompletion(id=collected[0].id, created=collected[0].created,
        object="chat.completion", model=selected, choices=[{"index": 0, "finish_reason": finish,
        "message": {"role": "assistant", "content": "".join(text),
                    "reasoning_content": "".join(reasoning),
                    "tool_calls": [tool_calls[i] for i in sorted(tool_calls)] or None}}])
    # Preserve unknown usage rather than substituting a tokenizer estimate.
    response.usage = next((chunk.usage for chunk in reversed(collected) if getattr(chunk, "usage", None)), None)
    return response
