"""Free-provider protocol, budget, route and API integration regressions."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Config
from src.llm.free_models import (
    BASE_URL, DEFAULT_MODEL, FreeModelError, FreeModelStore, catalog, completion,
)


@pytest.fixture
def cfg(tmp_path):
    return Config(free_model_enabled=True, database_path=str(tmp_path / "test.db"))


def test_free_channel_uses_only_explicit_proxy(cfg, monkeypatch):
    from src.llm.free_models import _http_client, settings_from_map
    monkeypatch.setenv('HTTPS_PROXY', 'http://ambient.invalid:8080')
    with patch('src.llm.free_models.httpx.Client') as client:
        _http_client(cfg, timeout=15)
        assert client.call_args.kwargs['proxy'] is None
        assert client.call_args.kwargs['trust_env'] is False
        cfg.free_model_proxy_url = 'http://explicit.invalid:8080'
        _http_client(cfg, timeout=15)
        assert client.call_args.kwargs['proxy'] == cfg.free_model_proxy_url
        _http_client(cfg, timeout=15, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        assert client.call_args.kwargs['proxy'] is None
    assert settings_from_map({'FREE_MODEL_PROXY_URL': ' http://explicit.invalid:8080 '})['free_model_proxy_url'] == cfg.free_model_proxy_url


def wire(events, done=True):
    data = "".join("data: " + json.dumps(e, ensure_ascii=False) + "\r\n\r\n" for e in events)
    return (": heartbeat\r\n\r\n" + data + ("data: [DONE]\r\n\r\n" if done else "")).encode()


def event(delta=None, finish=None, usage=None):
    return {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
            "model": DEFAULT_MODEL, "choices": [] if usage else [
                {"index": 0, "delta": delta or {}, "finish_reason": finish}],
            **({"usage": usage} if usage else {})}


def transport(calls, *, data=None, status=200, price=0):
    def handle(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": DEFAULT_MODEL}]})
        if request.url.host == "models.dev":
            return httpx.Response(200, json={"opencode": {"models": {DEFAULT_MODEL: {
                "cost": {"input": price, "output": price}, "modalities": {"input": ["text"]}}}}})
        calls.append(request)
        return httpx.Response(status, headers={"content-type": "text/event-stream"},
            content=data if data is not None else wire([
                event({"content": "正常"}), event(finish="stop"),
                event(usage={"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10})]))
    return httpx.MockTransport(handle)


def test_enabled_route_is_free_only_and_disabled_preserves_paid(tmp_path):
    paid = Config(litellm_model="deepseek/paid", litellm_fallback_models=["openai/paid"])
    assert paid.litellm_model == "deepseek/paid"
    free = Config(free_model_enabled=True, litellm_model="deepseek/paid",
                  litellm_fallback_models=["openai/paid"], database_path=str(tmp_path / "free.db"))
    assert free.litellm_model == f"openai/{DEFAULT_MODEL}"
    assert free.agent_litellm_model == free.vision_model == free.litellm_model
    assert free.litellm_fallback_models == []
    assert free.generation_fallback_backend == ""
    assert {x["litellm_params"]["api_key"] for x in free.llm_model_list} == {"public"}


def test_sse_usage_session_and_headers(cfg):
    calls = []
    tx = transport(calls)
    for _ in range(2):
        result = completion(cfg, messages=[{"role": "user", "content": "test"}], transport=tx)
        assert result.choices[0].message.content == "正常"
        assert result.usage.total_tokens == 10
    a, b = calls
    assert re.fullmatch(r"ses_[0-9a-f]{12}[0-9A-Za-z]{14}", a.headers["x-opencode-session"])
    assert a.headers["x-opencode-session"] == b.headers["x-opencode-session"]
    assert a.headers["x-opencode-request"] != b.headers["x-opencode-request"]
    assert a.headers["authorization"] == "Bearer public"
    assert a.url == BASE_URL + "/chat/completions"
    payload = json.loads(a.content)
    assert payload["tool_choice"] == "none" and payload["stream"] is True
    assert FreeModelStore(cfg).status(cfg)["used_today"] == 2


def test_no_usage_stays_unknown(cfg):
    r = completion(cfg, messages=[{"role": "user", "content": "x"}],
                   transport=transport([], data=wire([event({"content": "OK"}, "stop")])))
    assert r.usage is None
    assert FreeModelStore(cfg).status(cfg)["recent_calls"][0]["tokens"] is None


@pytest.mark.parametrize("data,status,code", [
    (wire([event({"content": "partial"})], done=False), 200, "incomplete_response"),
    (wire([event({"content": "partial"}, "length")]), 200, "incomplete_response"),
    (wire([event({"tool_calls": [{"index": 0, "function": {"name": "bash", "arguments": "{}"}}]}, "tool_calls")]), 200, "unexpected_tool"),
    (b'{"error":"RegionError"}', 403, "region_unavailable"),
    (b'{"error":"FreeTierError"}', 403, "access_restricted"),
    (b'limited', 429, "rate_limited"),
    (wire([{"error": {"message": "RegionError"}}]), 200, "region_unavailable"),
])
def test_failures_no_retry_no_paid_fallback(cfg, data, status, code):
    calls = []
    with pytest.raises(FreeModelError) as err:
        completion(cfg, messages=[{"role": "user", "content": "test"}], transport=transport(calls, data=data, status=status))
    assert err.value.code == code
    assert len(calls) == 1
    assert FreeModelStore(cfg).status(cfg)["recent_calls"][0]["status"] == "failed"


def test_price_change_blocks_before_completion(cfg):
    calls = []
    with pytest.raises(FreeModelError, match="目录"):
        completion(cfg, messages=[{"role": "user", "content": "test"}], transport=transport(calls, price=1))
    assert calls == []


def test_quota_is_atomic(cfg):
    cfg.free_model_daily_requests = 1
    store = FreeModelStore(cfg)
    def reserve(_):
        try:
            return store.reserve(cfg, DEFAULT_MODEL)
        except FreeModelError as exc:
            return exc.code
    with ThreadPoolExecutor(5) as pool:
        results = list(pool.map(reserve, range(5)))
    assert results.count("quota_exhausted") == 4


def test_research_route_and_adapter(cfg):
    from src.services.research_delivery.explanation import resolve_route, make_completion
    route = resolve_route(cfg)
    assert route.status == "supported" and route.provider == "opencode_free"
    text, model, usage = make_completion(route, config=cfg, http_transport=transport([]))(
        "test", {"max_output_tokens": 512})
    assert text == "正常" and model == cfg.litellm_model
    assert usage["total_tokens"] == 10 and usage["finish_reason"] == "stop"


def test_agent_and_analyzer_dispatch_use_free_adapter(cfg):
    from src.analyzer import GeminiAnalyzer
    from src.agent.llm_adapter import LLMToolAdapter
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    with patch("src.llm.free_models.completion", return_value="sentinel") as free:
        result = analyzer._dispatch_litellm_completion(cfg.litellm_model, {"messages": []},
            config=cfg, use_channel_router=True, router_model_names=set())
        assert result == "sentinel"
        free.assert_called_once()
    adapter = LLMToolAdapter.__new__(LLMToolAdapter)
    adapter._config = cfg
    with patch.object(adapter, "_convert_messages", return_value=[]), \
         patch.object(adapter, "_parse_litellm_response", return_value="parsed"), \
         patch("src.llm.free_models.completion", return_value="sentinel") as free:
        assert adapter._call_litellm_model([], [], cfg.litellm_model) == "parsed"
        free.assert_called_once()


def test_api_auth_same_origin_and_smoke(cfg):
    from api.v1.endpoints import free_models as api
    from src import auth
    app = FastAPI()
    app.include_router(api.router, prefix="/free")
    client = TestClient(app)
    with patch.object(api, "get_config", return_value=cfg), \
         patch.object(auth, "is_auth_enabled", return_value=True), \
         patch.object(auth, "verify_session", return_value=True):
        assert client.get("/free/status").status_code == 401
        client.cookies.set(auth.COOKIE_NAME, "test")
        assert client.get("/free/status").status_code == 200
        assert client.post("/free/test", json={"model": DEFAULT_MODEL}).status_code == 403
        assert client.post("/free/test", headers={"origin": "http://evil.test"}, json={"model": DEFAULT_MODEL}).status_code == 403
        result = completion(cfg, messages=[{"role": "user", "content": "x"}], transport=transport([], data=wire([event({"content": "OK"}, "stop")])))
        with patch.object(api, "completion", return_value=result):
            response = client.post("/free/test", headers={"origin": "http://testserver"}, json={"model": DEFAULT_MODEL})
        assert response.status_code == 200 and response.json()["success"] is True


def test_diagnostic_config_uses_same_free_route(tmp_path):
    from src.services.generation_backend_status_service import GenerationBackendStatusService
    from src.services.agent_backend_status_service import AgentBackendStatusService
    values = {"FREE_MODEL_ENABLED": "true", "FREE_MODEL_NAME": DEFAULT_MODEL,
              "FREE_MODEL_DAILY_REQUESTS": "123", "DATABASE_PATH": str(tmp_path / "diag.db"),
              "LITELLM_MODEL": "openai/old", "LITELLM_FALLBACK_MODELS": "openai/paid-fallback",
              "AGENT_LITELLM_MODEL": "openai/old-agent", "AGENT_BACKEND": "auto"}
    service = GenerationBackendStatusService(effective_map=values)
    config = service.build_effective_config()
    assert config.free_model_enabled and config.free_model_daily_requests == 123
    assert config.database_path == values["DATABASE_PATH"]
    assert config.litellm_model == f"openai/{DEFAULT_MODEL}"
    assert config.litellm_fallback_models == []
    assert service._litellm_route_error(config) is None
    agent_config = AgentBackendStatusService(effective_map=values)._build_config()
    assert agent_config.agent_litellm_model == config.litellm_model


def test_tool_call_fragments_and_host_round_trip(cfg):
    calls = []
    tools = [{"type": "function", "function": {"name": "get_quote",
              "parameters": {"type": "object", "properties": {}}}}]
    data = wire([
        event({"tool_calls": [{"index": 0, "id": "call_test", "type": "function",
                               "function": {"name": "get_", "arguments": '{"symbol":'}}]}),
        event({"tool_calls": [{"index": 0, "function": {"name": "quote", "arguments": '"DEMO"}'}}]}),
        event(finish="tool_calls"),
    ])
    result = completion(cfg, messages=[{"role": "user", "content": "test"}], tools=tools,
                        transport=transport(calls, data=data))
    tool = result.choices[0].message.tool_calls[0]
    assert tool.id == "call_test" and tool.function.name == "get_quote"
    assert json.loads(tool.function.arguments) == {"symbol": "DEMO"}
    assert json.loads(calls[0].content)["tool_choice"] == "auto"
    assert {t["function"]["name"] for t in json.loads(calls[0].content)["tools"]} == {"get_quote", "bash", "read"}
    assert FreeModelStore(cfg).status(cfg)["recent_calls"][0]["status"] == "succeeded"


def test_unknown_assembled_tool_is_rejected(cfg):
    data = wire([event({"tool_calls": [{"index": 0, "id": "call_test", "type": "function",
                                       "function": {"name": "bash", "arguments": "{}"}}]}, "tool_calls")])
    with pytest.raises(FreeModelError, match="工具"):
        completion(cfg, messages=[{"role": "user", "content": "test"}],
                   tools=[{"type": "function", "function": {"name": "get_quote"}}],
                   transport=transport([], data=data))


def test_analyzer_does_not_repeat_failed_free_stream(cfg):
    from src.analyzer import GeminiAnalyzer, _AllModelsFailedError, _LiteLLMStreamError
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._router = None
    with patch.object(analyzer, "_get_runtime_config", return_value=cfg), \
         patch.object(analyzer, "_dispatch_litellm_completion", return_value=iter([])) as dispatch, \
         patch.object(analyzer, "_consume_litellm_stream",
                      side_effect=_LiteLLMStreamError("partial", partial_received=True)):
        with pytest.raises(_AllModelsFailedError):
            analyzer._call_litellm_impl("test", {}, stream=True)
    assert dispatch.call_count == 1


def test_sse_utf8_split_across_network_chunks(cfg):
    class Bytes(httpx.SyncByteStream):
        def __iter__(self):
            for byte in wire([event({"content": "中文正常"}, "stop")]):
                yield bytes([byte])
    catalog(cfg, transport=transport([]))
    tx = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Bytes()))
    result = completion(cfg, messages=[{"role": "user", "content": "x"}], transport=tx)
    assert result.choices[0].message.content == "中文正常"


def test_oversized_unterminated_sse_line_is_rejected(cfg):
    with pytest.raises(FreeModelError) as error:
        completion(cfg, messages=[{"role": "user", "content": "x"}],
                   transport=transport([], data=b"data: " + b"x" * (256 * 1024)))
    assert error.value.code == "response_too_large"
