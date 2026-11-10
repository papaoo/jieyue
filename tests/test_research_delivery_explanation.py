import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from tests.test_research_delivery_aggregation import research, saved_run
from src.config import Config
from src.services.research_delivery.explanation import resolve_route, ExplanationService, validate_explanation, make_completion, SYSTEM_PROMPT, PROMPT_VERSION


def model_config():
    return Config(litellm_model="friendly", llm_models_source="yaml", llm_model_list=[{"model_name": "friendly", "litellm_params": {"model": "openai/vendor/model", "api_key": "fake-key-123", "api_base": "https://gateway.example/v1", "extra_headers": {"X-Route": "preserved"}}}])


def test_explicit_brief_mode_uses_new_prompt_whole_input_once_key(research):
    from copy import deepcopy
    from tests.test_research_delivery_brief import reply
    from src.services.research_delivery.research_brief import FORMAT_VERSION, PROMPT_VERSION as BRIEF_PROMPT
    publisher, _, _ = research
    brief = dict(format_version=FORMAT_VERSION, strategy_id='volume_breakout', business_date='2026-10-15',
        strategy={'definition': '量价条件', 'expression_version': 4}, candidates=[dict(code='600001', name='示例')], facts={}, instruments={})
    calls = []
    def complete(prompt, *args, **kwargs):
        calls.append((prompt, kwargs['system_prompt']))
        return json.dumps(reply(brief)), 'openai/vendor/model', {}
    service = ExplanationService(publisher.repository, model_config(), completion=complete)
    one = service.explain(brief, 'same', True, 'explain_only')
    assert one['status'] == 'fallback' and one['reason'] == 'no_effective_model_paragraphs' and one['prompt_version'] == BRIEF_PROMPT
    assert service.explain(brief, 'same', True, 'explain_only') == one
    changed = deepcopy(brief)
    changed['strategy']['definition'] = '另一冻结定义'
    assert service.explain(changed, 'same', True, 'explain_only')['status'] == 'fallback'
    assert len(calls) == 2 and '不可信数据' in calls[0][1]
    assert '另一冻结定义' in calls[1][0]
    assert PROMPT_VERSION == 'research-explain-v3'


def test_rich_top_five_reaches_model_once_with_bounded_relevant_view(research):
    from copy import deepcopy
    from tests.test_research_delivery_brief import frozen, realistic_top_five, rich_backgrounds, reply
    from src.services.research_delivery.render_formal import formal_envelope
    from src.services.research_delivery.research_brief import build_brief_input
    source = rich_backgrounds(realistic_top_five(frozen.__wrapped__()[0]))
    env, detail = formal_envelope(source, 5, format_version='research-brief-v1')
    brief = build_brief_input(env, detail, source)
    complete_input = deepcopy(brief)
    prompts = []
    def complete(prompt, *args, **kwargs):
        prompts.append(prompt)
        projected = json.loads(prompt.removeprefix('<frozen_input>').removesuffix('</frozen_input>'))
        assert len(projected['candidates']) == 5
        assert '600001:growth:net_profit_yoy' in projected['facts']
        assert '600001:stock_flow:inflow_5d' in projected['facts']
        from tests.test_research_delivery_signals import selection
        generated = selection(brief)
        return json.dumps(generated), 'openai/vendor/model', {}
    publisher = research[0]
    service = ExplanationService(publisher.repository, model_config(), completion=complete)
    result = service.explain(brief, 'rich-same', True, 'explain_only')
    assert result['status'] == 'succeeded'
    assert len(prompts) == 1 and len(prompts[0]) < 48000
    assert service.explain(brief, 'rich-same', True, 'explain_only') == result
    assert len(prompts) == 1 and brief == complete_input
    assert '600001:institution:coverage' in brief['facts']
    changed = deepcopy(brief)
    changed['facts']['600001:institution:coverage']['value'] = '改变未进入提示词的冻结披露范围'
    assert service.explain(changed, 'rich-same', True, 'explain_only')['status'] == 'succeeded'
    assert len(prompts) == 2 and prompts[0] == prompts[1]


def test_route_exact_alias_and_native_deepseek():
    route = resolve_route(model_config())
    assert route.model == "openai/vendor/model" and route.api_base == "https://gateway.example/v1"
    config = Config(litellm_model="deepseek/configured-model", llm_models_source="legacy_env", deepseek_api_keys=["fake-key-123"], llm_model_list=[{"model_name": "__legacy_deepseek__", "litellm_params": {"model": "__legacy_deepseek__", "api_key": "fake-key-123"}}])
    assert resolve_route(config).model == "deepseek/configured-model"
    config.litellm_model = "missing-alias"
    config.llm_models_source = "yaml"
    assert resolve_route(config).status == "unsupported"


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.parametrize("status", [429, 500])
def test_physical_sdk_requests_one_without_recovery(provider, status):
    config = model_config()
    config.llm_model_list[0]["litellm_params"]["model"] = provider + "/configured"
    route = resolve_route(config)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "retryable"}})
    completion = make_completion(route, http_transport=httpx.MockTransport(handle))
    with pytest.raises(Exception):
        completion("frozen", {}, system_prompt="JSON", stream=False)
    assert len(calls) == 1
    assert json.loads(calls[0].content)["model"] == "configured"
    assert calls[0].headers["X-Route"] == "preserved"


def test_quota_atomic_persistent_and_failed_attempt_spent(research):
    publisher, sub, _ = research
    repo = publisher.repository
    with ThreadPoolExecutor(max_workers=5) as pool:
        attempts = list(pool.map(lambda i: repo.reserve_llm("key" + str(i), "2026-10-13"), range(25)))
    assert sum(a is not None for a in attempts) == 20
    assert repo.reserve_llm("key0", "2026-10-14")["created"] is False
    assert repo.reserve_llm("key-new", "2026-10-13") is None


def test_explain_same_facts_cache_usage_null_and_prompt_private_free(research):
    publisher, sub, dest = research
    saved_run(publisher, sub)
    sub = publisher.service.update_subscription(sub["id"], {"version": sub["version"], "llm_mode": "explain_only"})
    calls = []
    def completion(prompt, config, **kwargs):
        calls.append(prompt)
        return '{"candidates":[{"code":"600001","reasons":[]},{"code":"600002","reasons":[]}]}', "openai/vendor/model", {}
    publisher.explanations = ExplanationService(publisher.repository, model_config(), completion=completion)
    one = publisher.preview(sub["id"], "run-one", use_llm=True)
    two = publisher.preview(sub["id"], "run-one", use_llm=True)
    assert len(calls) == 1 and one["llm"]["status"] == "succeeded"
    assert one["llm"]["usage"]["total_tokens"] is None and one["llm"]["usage"]["cost"] is None
    assert two["llm"] == one["llm"]
    assert all(word not in calls[0] for word in ("private-target", "private_holdings", "never prompt", "fake-key"))


@pytest.mark.parametrize("reason", [
    {"kind": "fact", "text": "new 999", "metric_refs": ["score"], "evidence_ids": []},
    {"kind": "fact", "text": "claim", "metric_refs": [], "evidence_ids": ["invented"]},
    {"kind": "inference", "text": "保证收益", "metric_refs": [], "evidence_ids": []},
    {"kind": "fact", "text": "unsupported claim", "metric_refs": [], "evidence_ids": []},
])
def test_invalid_explanations_rejected(research, reason):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    envelope = publisher.preview(sub["id"], "run-one")["envelope"]
    text = json.dumps({"candidates": [{"code": "600001", "reasons": [reason]}, {"code": "600002", "reasons": []}]})
    with pytest.raises(ValueError): validate_explanation(text, envelope)


@pytest.mark.parametrize("text", ["价格可能达到５元", "可能达到三十元", "价格可能翻五倍", "百分之五的收益"])
def test_unicode_and_chinese_number_claims_rejected(research, text):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    envelope = publisher.preview(sub["id"], "run-one")["envelope"]
    reply = {"candidates": [{"code": "600001", "reasons": [{"kind": "inference", "text": text, "metric_refs": ["price"], "evidence_ids": []}]}, {"code": "600002", "reasons": []}]}
    with pytest.raises(ValueError): validate_explanation(json.dumps(reply), envelope)


def test_useful_chinese_inference_validated_and_separate_risks(research):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    report = publisher.preview(sub["id"], "run-one")
    reply = {"candidates": [{"code": "600001", "reasons": [{"kind": "inference", "text": "保存评分只能说明当时筛选信号，不能证明信号仍然持续；若后续指标转弱，该推断需要重新评估。", "metric_refs": ["score"], "evidence_ids": []}]}, {"code": "600002", "reasons": []}]}
    validated = validate_explanation(json.dumps(reply), report["envelope"])
    from src.services.research_delivery.rendering import render_draft
    rendered = render_draft(report["envelope"], {"status": "succeeded", "explanation": validated})
    assert "推断（待人工核实）" in rendered and "deep risk" in rendered


def test_corrected_key_or_routing_header_new_budget_identity():
    config = model_config()
    before = resolve_route(config).identity
    config.llm_model_list[0]["litellm_params"]["api_key"] = "corrected-fake-key"
    assert resolve_route(config).identity != before
    before = resolve_route(config).identity
    config.llm_model_list[0]["litellm_params"]["extra_headers"]["X-Route"] = "changed"
    assert resolve_route(config).identity != before


@pytest.mark.parametrize("provider,explicit,expected", [
    ("deepseek", {}, {"thinking": {"type": "disabled"}}),
    ("deepseek", {"extra_body": {"thinking": {"type": "enabled"}}}, {"thinking": {"type": "enabled"}}),
    ("deepseek", {"extra_body": {"thinking": {"type": "disabled"}}}, {"thinking": {"type": "disabled"}}),
    ("deepseek", {"reasoning_effort": "none"}, {"reasoning_effort": "none"}),
    ("deepseek", {"reasoning_effort": "high"}, {"reasoning_effort": "high"}),
    ("deepseek", {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": "high"},
        {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ("openai", {}, {}),
    ("openai", {"extra_body": {"thinking": {"type": "enabled"}}}, {"thinking": {"type": "enabled"}}),
])
def test_sdk_wire_thinking_default_and_explicit_settings(provider, explicit, expected):
    from copy import deepcopy
    config = model_config()
    params = config.llm_model_list[0]["litellm_params"]
    params.update(model=provider + "/configured", **explicit)
    original = deepcopy(params)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
            "model": "configured", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"candidates":[]}'}}]})
    route = resolve_route(config)
    make_completion(route, http_transport=httpx.MockTransport(handle))("frozen", {})
    assert len(calls) == 1 and params == original
    body = json.loads(calls[0].content)
    assert {key: body[key] for key in ("thinking", "reasoning_effort") if key in body} == expected
    assert body["model"] == "configured" and body["max_tokens"] == 1800 and body["stream"] is False
    assert calls[0].extensions["timeout"] == dict(connect=45, read=45, write=45, pool=45)
    if provider == "deepseek" and not explicit:
        config.llm_model_list[0]["litellm_params"]["extra_body"] = {"thinking": {"type": "disabled"}}
        assert resolve_route(config).identity == route.identity


@pytest.mark.parametrize("finish,status", [("stop", "succeeded"), ("length", "fallback")])
def test_sdk_strict_valid_response_and_length_rejection_once(research, finish, status):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    envelope = publisher.preview(sub["id"], "run-one")["envelope"]
    reply = {"candidates": [{"code": row["code"], "reasons": [{"kind": "inference",
        "text": "规则信号仍需核验，后续变化可能否定该推断", "metric_refs": ["score"], "evidence_ids": []}]}
        for row in envelope["candidates"]]}
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "offline", "object": "chat.completion", "created": 0,
            "model": "configured", "choices": [{"index": 0, "finish_reason": finish,
                "message": {"role": "assistant", "content": json.dumps(reply, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}})
    config = model_config()
    config.llm_model_list[0]["litellm_params"]["model"] = "deepseek/configured"
    service = ExplanationService(publisher.repository, config,
        completion=make_completion(resolve_route(config), http_transport=httpx.MockTransport(handle)))
    result = service.explain(envelope, "sdk-" + finish, True, "explain_only")
    assert result["status"] == status
    assert result["usage"]["completion_tokens"] == 20
    if finish == "stop":
        assert result["explanation"] == reply
    else:
        assert result["reason"] == "model_output_truncated" and result["explanation"] is None
    assert service.explain(envelope, "sdk-" + finish, True, "explain_only") == result
    assert len(calls) == 1


def test_prompt_bounded_inference_and_banned_substring_contract():
    assert PROMPT_VERSION == "research-explain-v3"
    assert "at most one" in SYSTEM_PROMPT and "even when negated or quoted" in SYSTEM_PROMPT
    for phrase in ("仍需核验", "存在不确定性", "风险可控", "low risk", "buy", "sell"):
        assert phrase in SYSTEM_PROMPT


@pytest.mark.parametrize("text", ["不能保证信号持续", "不代表低风险", "cannot guarantee continuation"])
def test_negated_prohibited_substrings_still_rejected(research, text):
    publisher, sub, _ = research
    saved_run(publisher, sub)
    envelope = publisher.preview(sub["id"], "run-one")["envelope"]
    reply = {"candidates": [{"code": "600001", "reasons": [{"kind": "inference", "text": text,
        "metric_refs": ["score"], "evidence_ids": []}]}, {"code": "600002", "reasons": []}]}
    with pytest.raises(ValueError, match="Advice or risk modification"):
        validate_explanation(json.dumps(reply), envelope)


@pytest.mark.parametrize('finish,reason', [('length', 'model_output_truncated'), ('stop', 'no_effective_model_paragraphs'),
    ('provider-private-text', 'invalid_model_output')])
def test_brief_sdk_budget_finish_whitelist_usage_once_and_unchanged_thinking(research, finish, reason):
    from tests.test_research_delivery_brief import frozen, reply
    brief = frozen.__wrapped__()[-1]
    config = model_config()
    config.llm_model_list[0]['litellm_params']['extra_body'] = {'thinking': {'type': 'enabled'}}
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': 'configured',
            'choices': [{'index': 0, 'finish_reason': finish, 'message': {'role': 'assistant', 'content': json.dumps(reply(brief))}}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 4096, 'total_tokens': 4106}})
    service = ExplanationService(research[0].repository, config,
        completion=make_completion(resolve_route(config), http_transport=httpx.MockTransport(handle)))
    result = service.explain(brief, 'brief-sdk', True, 'explain_only')
    assert result['status'] == 'fallback' and result['reason'] == reason
    assert result['usage']['completion_tokens'] == 4096
    assert result['finish_reason'] == (finish if finish in ('stop', 'length') else None)
    assert 'provider-private-text' not in json.dumps(result)
    assert service.explain(brief, 'brief-sdk', True, 'explain_only') == result and len(calls) == 1
    body = json.loads(calls[0].content)
    assert body['max_tokens'] == 4096 and body['thinking'] == {'type': 'enabled'}
    assert calls[0].extensions['timeout'] == dict(connect=45, read=45, write=45, pool=45)


def test_brief_generation_budget_and_prompt_version_invalidate_once_key(research, monkeypatch):
    import src.services.research_delivery.explanation as module
    from tests.test_research_delivery_brief import frozen, reply
    brief, calls = frozen.__wrapped__()[-1], []
    brief['strategy']['expression_version'] = 4
    def completion(prompt, config, **kwargs):
        calls.append(config)
        return json.dumps(reply(brief)), 'openai/vendor/model', {}
    service = ExplanationService(research[0].repository, model_config(), completion=completion)
    first = service.explain(brief, 'same', True, 'explain_only')
    assert service.explain(brief, 'same', True, 'explain_only') == first and len(calls) == 1
    monkeypatch.setattr(module, 'BRIEF_MAX_OUTPUT_TOKENS', 4000)
    service.explain(brief, 'same', True, 'explain_only')
    monkeypatch.setattr(module, 'BRIEF_PROMPT_VERSION', 'test-next-prompt')
    service.explain(brief, 'same', True, 'explain_only')
    assert len(calls) == 3 and [call['max_output_tokens'] for call in calls] == [4096, 4000, 4000]


def test_v3_prompt_populated_array_example_and_legacy_version_unchanged():
    from src.services.research_delivery.research_brief import LEGACY_SYSTEM_PROMPT as BRIEF_SYSTEM, PROMPT_VERSION as BRIEF_VERSION
    from src.services.research_delivery.research_brief import validate_brief_explanation
    from tests.test_research_delivery_brief import frozen, reply, segment
    assert BRIEF_VERSION == 'research-brief-select-v4'
    assert PROMPT_VERSION == 'research-explain-v3'
    example = json.loads(next(line.rstrip('。') for line in BRIEF_SYSTEM.splitlines() if line.startswith('{"summary"')))
    row = example['candidates'][0]
    assert isinstance(example['summary'], list) and isinstance(example['cross_signals'], list)
    assert isinstance(row['rationale'], list) and isinstance(row['watch'], list) and row['invalidation'] == []
    assert row['rationale'][0]['kind'] == 'inference' and row['watch'][0]['kind'] == 'condition'
    assert set(row['rationale'][0]) == {'kind', 'text', 'fact_refs', 'claim_domain', 'entity_refs'}
    assert '不要null' in BRIEF_SYSTEM and '不要照抄ID' in BRIEF_SYSTEM
    brief = frozen.__wrapped__()[-1]
    adapted = json.dumps(example, ensure_ascii=False).replace('原代码', '600001').replace('原主体id', '600001').replace('原估值事实id', '600001:valuation:pe_ratio')
    checked = validate_brief_explanation(adapted, brief)
    assert checked['candidates'][0]['rationale'] and checked['candidates'][0]['watch'] and not checked['diagnostics']
    import re
    short_example = re.search('如“([^”]+)”', BRIEF_SYSTEM).group(1)
    checked = validate_brief_explanation(json.dumps(reply(brief, summary=[segment(short_example,
        domain='valuation', refs=['600001:valuation:pe_ratio'])])), brief)
    assert checked['summary'] and not checked['diagnostics']


def test_v3_wire_singleton_and_multi_segment_normalization_one_request(research):
    from tests.test_research_delivery_brief import frozen, reply, segment
    brief = frozen.__wrapped__()[-1]
    refs = ['600001:price:change_pct']
    proposed = reply(brief, summary=segment('量价配合体现规则信号，后续承接决定解释是否延续', refs=refs),
        cross_signals=[segment('保证上涨', refs=refs), segment('若后续成交配合转弱，需重新评估规则信号', kind='condition', refs=refs)])
    proposed['candidates'][0]['rationale'] = [segment('放量与价格表现相互配合，后续承接仍需观察', refs=refs)]
    calls, config = [], model_config()
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'id': 'offline', 'object': 'chat.completion', 'created': 0,
            'model': 'configured', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps(proposed, ensure_ascii=False)}}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 300, 'total_tokens': 312}})
    service = ExplanationService(research[0].repository, config,
        completion=make_completion(resolve_route(config), http_transport=httpx.MockTransport(handle)))
    result = service.explain(brief, 'v3-wire', True, 'explain_only')
    assert result['status'] == 'succeeded' and result['prompt_version'] == 'research-brief-explain-v3'
    assert result['explanation']['diagnostics'] == ['cross_signals:segment_rejected', 'summary:section_singleton_wrapped']
    assert result['usage']['total_tokens'] == 312
    assert service.explain(brief, 'v3-wire', True, 'explain_only') == result and len(calls) == 1
    body = json.loads(calls[0].content)
    assert body['max_tokens'] == 4096 and body['stream'] is False and 'response_format' not in body
    assert calls[0].extensions['timeout'] == dict(connect=45, read=45, write=45, pool=45)
    assert '不得写成字符串或对象' in body['messages'][0]['content']


def test_v4_selection_does_not_reuse_v3_attempt(research):
    from tests.test_research_delivery_brief import frozen, reply, segment
    brief, calls = frozen.__wrapped__()[-1], []
    def complete(prompt, config, **kwargs):
        calls.append(prompt)
        if brief['strategy']['expression_version'] == 4:
            from tests.test_research_delivery_signals import selection
            return json.dumps(selection(brief)), 'openai/vendor/model', {}
        return json.dumps(reply(brief, summary=[segment('规则信号仍需后续承接验证', refs=['600001:price:change_pct'])])), 'openai/vendor/model', {}
    service = ExplanationService(research[0].repository, model_config(), completion=complete)
    first = service.explain(brief, 'same-input', True, 'explain_only')
    assert first['prompt_version'] == 'research-brief-explain-v3'
    brief['strategy']['expression_version'] = 4
    second = service.explain(brief, 'same-input', True, 'explain_only')
    assert service.explain(brief, 'same-input', True, 'explain_only') == second
    assert len(calls) == 2 and second['status'] == 'succeeded'
    assert second['prompt_version'] == 'research-brief-select-v4'


@pytest.mark.parametrize('core', [True, False])
def test_v4_sdk_core_vs_watch_only_selection_and_once(research, core):
    from tests.test_research_delivery_brief import frozen
    from tests.test_research_delivery_signals import selection
    brief = frozen.__wrapped__()[-1]
    brief['strategy']['expression_version'] = 4
    calls = []
    def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        projected = json.loads(body['messages'][1]['content'].removeprefix('<frozen_input>').removesuffix('</frozen_input>'))
        proposed = selection(projected)
        if not core:
            proposed['summary'], proposed['cross_signals'] = [], []
            for row in proposed['candidates']:
                row['rationale'] = []
        return httpx.Response(200, json={'id': 'offline', 'object': 'chat.completion', 'created': 0,
            'model': 'configured', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': json.dumps(proposed)}}]})
    config = model_config()
    service = ExplanationService(research[0].repository, config,
        completion=make_completion(resolve_route(config), http_transport=httpx.MockTransport(handle)))
    result = service.explain(brief, 'v4-wire', True, 'explain_only')
    assert result['status'] == ('succeeded' if core else 'fallback')
    assert result['reason'] == (None if core else 'incomplete_core_explanation')
    assert service.explain(brief, 'v4-wire', True, 'explain_only') == result and len(calls) == 1
    assert result['prompt_version'] == 'research-brief-select-v4'
    assert 'signal_id' in result['explanation']['candidates'][0]['watch'][0]
    assert calls[0].extensions['timeout'] == dict(connect=45, read=45, write=45, pool=45)
