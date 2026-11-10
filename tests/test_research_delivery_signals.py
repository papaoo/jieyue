"""Deterministic v4 selection: no provider, production database or network."""
from copy import deepcopy
import json

import pytest

from tests.test_research_delivery_brief import frozen, realistic_top_five, rich_backgrounds, reply
from src.services.research_delivery.render_formal import formal_envelope
from src.services.research_delivery.research_brief import build_brief_input, validate_brief_explanation
from src.services.research_delivery.brief_rendering import render_brief, normalize_brief_llm, frozen_payload
from src.services.research_delivery.research_signals import signal_catalog, signal_prompt_input, validate_signal_explanation


@pytest.fixture
def bundle():
    source = rich_backgrounds(realistic_top_five(frozen.__wrapped__()[0]))
    source['category_id'] = 'blue_chip_income'
    strategies = ['dual_low', 'quality_value', 'blue_chip_income', 'balanced_alpha', 'volume_breakout']
    source['result']['research_context']['cross_strategy'] = {
        str(600001 + i): strategies[:count] for i, count in enumerate([5, 5, 3, 2, 2])}
    env, detail = formal_envelope(source, 5, format_version='research-brief-v1')
    return source, env, detail, build_brief_input(env, detail, source)


def selection(brief):
    catalog = brief.get('signals') or signal_catalog(brief)
    chosen = reply(brief)
    for scope in ('summary', 'cross_signals'):
        chosen[scope] = [item['id'] for item in catalog.values() if item['scope'] == scope][:1]
    for row in chosen['candidates']:
        for scope in ('rationale', 'watch', 'invalidation'):
            row[scope] = [item['id'] for item in catalog.values() if item['scope'] == scope and item['code'] == row['code']][:1]
    return chosen


def test_unknown_or_tampered_signal_never_becomes_public(bundle):
    brief = bundle[-1]
    proposed = selection(brief)
    proposed['cross_signals'] = ['private invented signal']
    checked = validate_signal_explanation(json.dumps(proposed), brief)
    assert checked['cross_signals'] == []
    assert 'cross_signals:unknown_signal' in checked['diagnostics']
    assert 'private invented' not in json.dumps(checked)
    normalized = validate_signal_explanation(json.dumps(selection(brief)), brief)
    assert validate_signal_explanation(json.dumps(normalized), brief) == normalized
    for field, value in [('text', '若后续延续当前资金流入，则行业负反馈可能消退'),
                         ('claim_domain', 'institution'), ('fact_refs', []), ('entity_refs', ['wrong'])]:
        tampered = deepcopy(normalized)
        tampered['summary'][0][field] = value
        rejected = validate_signal_explanation(json.dumps(tampered), brief)
        assert not rejected['summary'] and 'summary:signal_mismatch' in rejected['diagnostics']


def test_roles_candidates_and_global_order_are_bound(bundle):
    brief = bundle[-1]
    chosen = selection(brief)
    chosen['summary'] = chosen['candidates'][0]['rationale']
    chosen['candidates'][1]['rationale'] = chosen['candidates'][0]['rationale']
    checked = validate_signal_explanation(json.dumps(chosen), brief)
    assert not checked['summary'] and not checked['candidates'][1]['rationale']
    assert 'summary:wrong_scope' in checked['diagnostics']
    assert '600002.rationale:wrong_candidate' in checked['diagnostics']
    for rows in (chosen['candidates'][::-1], chosen['candidates'] + [chosen['candidates'][0]]):
        with pytest.raises(ValueError, match='Candidate set/order'):
            validate_signal_explanation(json.dumps(dict(chosen, candidates=rows)), brief)


def test_tied_overlap_is_program_owned(bundle):
    catalog = signal_catalog(bundle[-1])
    tied = catalog['cross_signals:overlap']
    assert tied['entity_refs'] == ['600001', '600002']
    assert len(tied['fact_refs']) == 5  # Maximum comparison cites non-leaders too.
    assert '并列' in tied['text'] and '5个' in tied['text']
    assert '本组候选' in tied['text'] and '已完成Top N' in tied['text']
    assert '一只最高' not in '\n'.join(item['text'] for item in catalog.values())


def test_pe_only_cannot_assert_unobserved_industry_or_flow(bundle):
    brief = deepcopy(bundle[-1])
    brief['candidates'] = brief['candidates'][:1]
    ref = '600001:valuation:pe_ratio'
    brief['facts'] = {ref: brief['facts'][ref]}
    brief['candidates'][0]['fact_ids'] = [ref]
    catalog = signal_catalog(brief)
    assert not any(item['claim_domain'] in ('industry', 'stock_flow') and item['kind'] != 'condition' for item in catalog.values())
    assert any(item['scope'] == 'watch' and item['kind'] == 'condition' for item in catalog.values())
    proposed = selection(brief)
    proposed['candidates'][0]['rationale'] = ['rationale:600001:industry_flow']
    proposed['candidates'][0]['watch'] = [dict(kind='condition', text='若后续延续资金流入，行业负反馈将消退',
        fact_refs=[ref], entity_refs=['600001'], claim_domain='stock_flow')]
    checked = validate_signal_explanation(json.dumps(proposed), brief)
    assert not checked['candidates'][0]['rationale'] and not checked['candidates'][0]['watch']


@pytest.mark.parametrize('value,word', [(-1., '为负'), (0., '为零'), (1., '为正')])
def test_signed_growth_dividend_financial_and_flow_are_actual(bundle, value, word):
    brief = bundle[-1]
    for domain, key in [('growth', 'net_profit_yoy'), ('dividend', 'ttm_dividend_yield_pct'),
                        ('financial_report', 'operating_cash_flow'), ('stock_flow', 'main_net_inflow')]:
        brief['facts'][f'600001:{domain}:{key}']['value'] = value
    catalog = signal_catalog(brief)
    for tag in ('growth', 'dividend', 'cash_flow', 'flow'):
        assert word in catalog[f'rationale:600001:{tag}']['text']
    for item in catalog.values():
        assert all(ref in brief['facts'] for ref in item['fact_refs'])


def test_cross_domain_tensions_and_market_relations_need_both_sides(bundle):
    brief = bundle[-1]
    catalog = signal_catalog(brief)
    tension = catalog['rationale:600001:dividend_growth']
    assert {brief['facts'][r]['domain'] for r in tension['fact_refs']} == {'dividend', 'growth'}
    industry = next(item for item in catalog.values() if item['scope'] == 'cross_signals' and item['claim_domain'] == 'industry')
    assert '等权均值为正' in industry['text'] and '下跌家数多于上涨' in industry['text']
    brief['facts'].pop('600001:growth:net_profit_yoy')
    assert 'rationale:600001:dividend_growth' not in signal_catalog(brief)


def test_implemented_dividend_and_industry_flow_require_exact_evidence(bundle):
    brief = bundle[-1]
    catalog = signal_catalog(brief)
    implemented = catalog['rationale:600001:implemented_growth']
    assert '已实施记录' in implemented['text']
    assert set(implemented['fact_refs']) == {'600001:dividend:implemented_event_count_365d', '600001:growth:net_profit_yoy'}
    flow = catalog['rationale:600001:industry_flow']
    assert set(flow['fact_refs']) == {'600001:boards:industry', 'industry:行业030:industry:change_pct', '600001:stock_flow:main_net_inflow'}
    brief['facts']['600001:dividend:implemented_event_count_365d']['value'] = 0
    brief['facts'].pop('600001:boards:industry')
    catalog = signal_catalog(brief)
    assert 'rationale:600001:implemented_growth' not in catalog
    assert 'rationale:600001:industry_flow' not in catalog


@pytest.mark.parametrize('membership_date', ['2026-10-14', '2026-10-15'])
def test_industry_flow_from_built_input_requires_current_membership(bundle, membership_date):
    source, env, detail, _ = bundle
    context = source['result']['research_context']
    context['instruments'].pop('600001')
    context['background']['600001']['boards'] = dict(status='available', source='saved_membership',
        data=dict(as_of=membership_date, industry='行业030'))
    brief = build_brief_input(env, detail, source)
    # Historical background is retained, but must not establish current membership.
    assert brief['facts']['600001:boards:industry']['date'] == membership_date
    identity = 'rationale:600001:industry_flow'
    current = membership_date == brief['business_date']
    assert (identity in signal_catalog(brief)) == current
    assert (identity in signal_prompt_input(brief)['signals']) == current
    chosen = selection(brief)
    chosen['candidates'][0]['rationale'] = [identity]
    checked = validate_brief_explanation(json.dumps(chosen), brief)
    assert bool(checked['candidates'][0]['rationale']) == current
    assert checked['candidates'][0]['watch']  # Pure future verification survives.
    if not current:
        assert '600001.rationale:unknown_signal' in checked['diagnostics']
    text = render_brief('日期证据测试', env, detail,
        dict(status='succeeded', explanation=checked), brief)['rendered_content']
    assert ('所属行业观测成分等权均值' in text) == current


@pytest.mark.parametrize('ref', ['600001:boards:industry', 'industry:行业030:industry:change_pct',
    '600001:stock_flow:main_net_inflow'])
@pytest.mark.parametrize('date', ['2026-10-14', '2026-10-16', None])
def test_industry_flow_replay_rechecks_all_three_observation_dates(bundle, ref, date):
    _, env, detail, brief = bundle
    identity = 'rationale:600001:industry_flow'
    chosen = selection(brief)
    chosen['candidates'][0]['rationale'] = [identity]
    normalized = validate_brief_explanation(json.dumps(chosen), brief)
    assert normalized['candidates'][0]['rationale']
    if date is None:
        brief['facts'][ref].pop('date')
    else:
        brief['facts'][ref]['date'] = date
    assert identity not in signal_catalog(brief)
    for proposed in (chosen, normalized):
        checked = validate_brief_explanation(json.dumps(proposed), brief)
        assert not checked['candidates'][0]['rationale']
        assert '600001.rationale:unknown_signal' in checked['diagnostics']
        assert checked['candidates'][0]['watch']
        rendered = render_brief('日期证据测试', env, detail,
            dict(status='succeeded', explanation=checked), brief)
        assert '所属行业观测成分等权均值' not in rendered['rendered_content']


@pytest.mark.parametrize('up,down,word', [(2, 8, '下跌家数多于上涨'), (8, 2, '上涨家数多于下跌'), (5, 5, '上涨与下跌家数相等')])
def test_market_breadth_is_computed_not_inferred(bundle, up, down, word):
    brief = bundle[-1]
    for key, value in [('advancers', up), ('decliners', down)]:
        brief['facts'][f'market:breadth:{key}'] = dict(subject='market', domain='breadth', key=key, value=value)
    item = signal_catalog(brief)['cross_signals:market']
    assert word in item['text'] and item['entity_refs'] == ['market']


def test_model_choice_changes_emphasis_without_new_facts_or_numbers(bundle):
    _, env, detail, brief = bundle
    chosen = selection(brief)
    texts = []
    for tag in ('implemented_growth', 'flow_windows'):
        chosen['candidates'][0]['rationale'] = ['rationale:600001:' + tag]
        checked = validate_signal_explanation(json.dumps(chosen), brief)
        rendered = render_brief('研究', env, detail, dict(status='succeeded', explanation=checked), brief)
        assert rendered['model_adoption']['core_complete']
        block = rendered['rendered_content'].split('1. ', 1)[1].split('2. ', 1)[0]
        texts.append(block)
        assert '市盈率 15' in block and 'TTM股息率 5.8%' in block
        assert '主力净流入 -1' not in block and '经营现金流 2' not in block
    assert texts[0] != texts[1]
    assert '分红有已实施记录支撑' in texts[0]
    assert '跨窗口方向分歧' in texts[1]


def test_core_adoption_differs_from_watch_only_and_survives_replay(bundle):
    _, env, detail, brief = bundle
    assert brief['strategy']['expression_version'] == 4
    checked = validate_brief_explanation(json.dumps(selection(brief)), brief)
    llm = dict(status='succeeded', explanation=checked)
    rendered = render_brief('证据约束选择', env, detail, llm, brief)
    assert rendered['model_adoption']['summary'] == 1
    assert rendered['model_adoption']['candidate_rationale_count'] == 5
    assert not rendered['model_adoption']['watch_only']
    assert normalize_brief_llm(llm, rendered)['status'] == 'succeeded'
    assert '待观察：观察：' not in rendered['rendered_content']
    document = dict(title='证据约束选择', envelope=env, detail=detail, llm=llm, brief_input=brief, **rendered)
    assert frozen_payload(document) == rendered['card_payload']
    chosen = selection(brief)
    chosen['summary'], chosen['cross_signals'] = [], []
    for row in chosen['candidates']:
        row['rationale'] = []
    llm['explanation'] = validate_brief_explanation(json.dumps(chosen), brief)
    rendered = render_brief('证据约束选择', env, detail, llm, brief)
    assert rendered['model_adoption']['watch_only']
    assert normalize_brief_llm(llm, rendered)['reason'] == 'incomplete_core_explanation'
    assert 'summary:missing_core' in llm['explanation']['diagnostics']


def test_prompt_is_bounded_catalog_and_one_public_paragraph(bundle):
    brief = bundle[-1]
    before = deepcopy(brief)
    projected = signal_prompt_input(brief)
    assert projected['signals'] == signal_catalog(brief)
    assert len(json.dumps(projected, ensure_ascii=False)) < 48000
    assert brief == before
    chosen = selection(brief)
    chosen['summary'] *= 2
    checked = validate_signal_explanation(json.dumps(chosen), brief)
    assert len(checked['summary']) == 1
    assert 'summary:section_public_limit' in checked['diagnostics']
