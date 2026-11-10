"""New presentation marker, exact grouping and old frozen replay regression."""
from copy import deepcopy
import json

import pytest

from tests.test_research_delivery_brief import frozen
from tests.test_research_delivery_signals import selection
from src.services.research_delivery.aggregation import content_hash
from src.services.research_delivery.brief_rendering import render_brief, frozen_payload
from src.services.research_delivery.render_formal import formal_envelope
from src.services.research_delivery.research_brief import build_brief_input, validate_brief_explanation


def rendered(source, model=False):
    env, detail = formal_envelope(source, 5, format_version='research-brief-v1')
    brief = build_brief_input(env, detail, source)
    llm = dict(status='succeeded' if model else 'off', explanation=
        validate_brief_explanation(json.dumps(selection(brief)), brief) if model else None)
    return dict(title='测试报告', envelope=env, detail=detail, brief_input=brief, llm=llm,
        **render_brief('测试报告', env, detail, llm, brief))


@pytest.mark.parametrize('kind', ['revision', 'formal', 'long_revision', 'other_date'])
def test_marked_heading_has_one_exact_business_date_without_mutating_title(kind):
    document = rendered(frozen.__wrapped__()[0])
    day = document['envelope']['business_date']
    titles = dict(revision=f'测试报告 · 修订版 · {day}', formal='测试报告',
        long_revision='研究' * 60 + f' · 修订版 · {day}', other_date='测试报告 · 2026-10-14')
    title = titles[kind]
    before = deepcopy(document)
    result = render_brief(title, document['envelope'], document['detail'], document['llm'], document['brief_input'])
    heading = result['rendered_content'].splitlines()[0]
    assert heading.count(day) == 1 and heading.endswith(' · ' + day)
    if kind == 'other_date':
        assert '2026-10-14' in heading
    if kind == 'long_revision':
        assert heading == '阶跃 · ' + '研究' * 50 + ' · ' + day
    assert document == before and titles[kind] == title
    assert result['card_payload']['card']['elements'][0]['text']['content'] == result['rendered_content']


@pytest.mark.parametrize('version', [None, 2, 3, 4])
def test_unmarked_heading_keeps_original_duplicate_date_replay(version):
    document = rendered(frozen.__wrapped__()[0])
    brief = document['brief_input']
    brief['strategy'].pop('display_revision')
    if version is None:
        brief['strategy'].pop('expression_version')
    else:
        brief['strategy']['expression_version'] = version
    day = document['envelope']['business_date']
    title = f'测试报告 · 修订版 · {day}'
    result = render_brief(title, document['envelope'], document['detail'], document['llm'], brief)
    assert result['rendered_content'].splitlines()[0] == f'阶跃 · {title} · {day}'


def announcement_source():
    source = frozen.__wrapped__()[0]
    source['category_id'] = 'announcement_risk'
    source['result']['records'] = [dict(issuer_code=str(600001 + i % 4), title=f'披露记录{i}',
        published_date='2026-10-15', source='CNINFO', evidence_kind='announcement_metadata',
        url=f'https://static.cninfo.com.cn/finalpage/2026-10-15/{i}.pdf') for i in range(5)]
    return source


@pytest.mark.parametrize('model', [False, True])
def test_announcements_group_identical_roles_with_explicit_issuers_and_true_adoption(model):
    source = announcement_source()
    document = rendered(source, model=model)
    text = document['rendered_content']
    assert text.count('解读：') == 1 and text.count('待观察：') == 1
    assert '600001' in text and '600004' in text
    assert [r['title'] for r in document['detail']['records']] == [r['title'] for r in source['result']['records']]
    for row in source['result']['records']:
        assert row['url'] in text
    adoption = document['model_adoption']
    assert adoption['candidate_rationale_count'] == (4 if model else 0)
    assert adoption['core_complete'] == model
    assert frozen_payload(document) == document['card_payload']


def test_old_unmarked_v4_announcement_replays_old_anonymous_layout():
    document = rendered(announcement_source())
    brief = document['brief_input']
    brief['strategy'].pop('announcement_grouping')
    brief['strategy'].pop('display_revision')
    document.update(render_brief(document['title'], document['envelope'], document['detail'], document['llm'], brief))
    assert document['rendered_content'].count('解读：') == 4
    digest = content_hash(document)
    assert frozen_payload(document) == document['card_payload']
    assert content_hash(document) == digest


def test_distinct_supported_announcement_interpretation_keeps_issuer_label():
    source = announcement_source()
    source['result']['research_context']['background']['600001'] = dict(
        issuer_code='600001', business_date=source['business_date'],
        growth=dict(status='available', source='fixture', data=dict(report_date='2026-06-30',
            announcement_date='2026-08-28', net_profit_yoy=-3.)))
    document = rendered(source, model=True)
    text = document['rendered_content']
    assert text.count('解读：') == 2
    assert '600001（名称未知） · 解读：披露期净利润同比为负' in text
    assert '600002（名称未知）、600003（名称未知）、600004（名称未知） · 解读：' in text
    assert document['model_adoption']['candidate_rationale_count'] == 4


@pytest.mark.parametrize('category', ['momentum_quality', 'oversold_reversal', 'capital_heat', 'balanced_alpha'])
def test_snapshot_only_missing_key_fields_stay_visible_on_new_inputs(category):
    source = frozen.__wrapped__()[0]
    source['category_id'] = category
    source['warnings'] = ['Optional daily refinements not evaluated; snapshot-only scoring branch used.']
    document = rendered(source)
    assert '本批使用快照评分' in document['rendered_content'] and '不等于完整策略确认' in document['rendered_content']
    brief = deepcopy(document['brief_input'])
    brief['strategy'].pop('display_revision')
    legacy = render_brief(document['title'], document['envelope'], document['detail'], document['llm'], brief)
    assert '本批使用快照评分' not in legacy['rendered_content']


@pytest.mark.parametrize('ratio,caveat', [(2.1, True), (1., True), (.7, False)])
def test_shrink_pullback_ratio_does_not_claim_contraction(ratio, caveat):
    source = frozen.__wrapped__()[0]
    source['category_id'] = 'shrink_pullback'
    source['result']['picks'][0]['volume_ratio_20d'] = ratio
    document = rendered(source)
    assert ('原规则入选不等于缩量形态已确认' in document['rendered_content']) == caveat
    assert document['envelope']['candidates'][0]['metrics']['volume_ratio_20d'] == ratio


def test_annualized_volatility_display_preserves_number_and_old_fact_replay():
    source = frozen.__wrapped__()[0]
    source['category_id'] = 'low_volatility_quality'
    source['result']['picks'][0]['volatility_20d_pct'] = 17.25
    document = rendered(source)
    assert '近20日收益率年化波动率 17.25%' in document['rendered_content']
    assert document['brief_input']['facts']['600001:price:volatility_20d_pct']['value'] == 17.25
    old = deepcopy(document)
    old['brief_input']['strategy'].pop('display_revision')
    old['brief_input']['facts']['600001:price:volatility_20d_pct']['display'] = '近20日波动 17.25%'
    old.update(render_brief(old['title'], old['envelope'], old['detail'], old['llm'], old['brief_input']))
    assert '近20日波动 17.25%' in old['rendered_content']
    assert frozen_payload(old) == old['card_payload']
