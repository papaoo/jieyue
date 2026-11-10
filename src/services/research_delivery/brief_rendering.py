"""One frozen brief supplies both readable text and the approved Feishu card."""
import re
from collections import Counter

from src.services.research_delivery.aggregation import canonical
from src.services.research_delivery.render_formal import public_text, _material_risks, _warning
from src.services.research_delivery.research_brief import FORMAT_VERSION, validate_brief_explanation, brief_metric_ids

GAPS = {'valuation': '估值', 'growth': '成长', 'financial_report': '财报', 'stock_flow': '资金流',
    'institution': '机构披露', 'dividend': '分红', 'dragon_tiger': '龙虎榜', 'boards': '板块'}
IMPORTANT_GAPS = {
    'volume_breakout': ('stock_flow', 'boards'), 'shrink_pullback': ('stock_flow',),
    'momentum_quality': ('growth',), 'capital_heat': ('stock_flow', 'boards'),
    'oversold_reversal': ('growth',), 'balanced_alpha': ('valuation', 'growth'),
    'dual_low': ('valuation', 'growth', 'financial_report'),
    'quality_value': ('valuation', 'growth', 'financial_report'),
    'low_volatility_quality': ('financial_report',), 'blue_chip_income': ('dividend', 'growth'),
}
GAP_LABELS = {'valuation': '估值依据', 'growth': '盈利变化', 'financial_report': '财务质量',
    'stock_flow': '资金方向', 'boards': '板块背景', 'dividend': '分红依据'}
WATCH = {
    'volume_breakout': '后续观察量价能否继续配合；若突破回落且承接转弱，突破延续的解释失效。',
    'shrink_pullback': '后续观察回调能否企稳；若缩量后继续走弱，承接改善的解释失效。',
    'dual_low': '后续核验盈利与资产质量；若盈利下修或资产减值，便宜的估值解释需重评。',
    'quality_value': '后续核验财报质量与估值匹配；若盈利或现金流转弱，质量假设需重评。',
    'blue_chip_income': '后续核验分红实施与盈利持续性；若分红或盈利变化，收入预期需重评。',
    'etf_rotation': '后续观察同口径区间动量；若领先关系逆转，原轮动解释失效。权重仅供研究，非交易指令。',
    'market_radar': '后续观察行业均值与上涨家数是否同步；若均值与广度背离，强势扩散的解释需重评。',
    'announcement_risk': '后续核验公告原文与披露背景；标题线索不能直接判断金额、业绩或情绪。',
}
CANDIDATE_WATCH = {
    'volume_breakout': '若后续突破回落且量能转弱，需重评承接。',
    'shrink_pullback': '若缩量后仍持续走弱，需重评企稳可能。',
    'momentum_quality': '若区间动量转弱，需重评趋势延续。',
    'capital_heat': '后续核验量价热度能否得到资金方向支持。',
    'oversold_reversal': '后续观察价格能否企稳，修复失败则重评。',
    'balanced_alpha': '若趋势转弱或盈利下修，需重评技术估值匹配。',
    'dual_low': '若盈利下修或资产减值，需重评估值解释。',
    'quality_value': '若盈利或现金流转弱，需重评质量假设。',
    'low_volatility_quality': '若波动与回撤扩大，需重评历史稳定性。',
    'blue_chip_income': '后续核验分红实施与盈利能否支持收入预期。',
    'etf_rotation': '若同窗口动量领先关系逆转，需重评轮动线索。',
    'market_radar': '后续观察上涨家数能否配合均值改善。',
}


def _name(code, name):
    return f'{name}（{code}）' if name and name != code and name != f'未知名称（{code}）' else f'{code}（名称未知）'


def _segments(items, facts, visible_metrics, subjects=None, labels=True):
    output = []
    for item in items:
        text = item['text']
        placeholders = re.findall(r'\{\{([^{}]+)\}\}', text)
        if any(facts[ref]['subject'] in visible_metrics and type(facts[ref]['value']) in (int, float, bool)
                and ref not in visible_metrics[facts[ref]['subject']] for ref in placeholders):
            continue  # Valid model fact stays frozen, but cannot expand the public metric budget.
        for ref in item['fact_refs']:
            text = text.replace('{{' + ref + '}}', facts[ref]['display'])
        if subjects is not None:
            text = '、'.join(subjects.get(ref, '已引用主体') for ref in dict.fromkeys(item['entity_refs'])) + '：' + text
        output.append((('事实：' if item['kind'] == 'fact' else '观察：' if item['kind'] == 'condition' else '研判：') if labels else '') + text)
    return '；'.join(output)


def _risks(row):
    # Specific risks remain with their stock. Recognized assessment gaps are
    # summarized once, by relevance and exact affected candidates, below.
    adjusted = dict(row, risks=[])
    aliases = {'capital_flow': 'stock_flow', 'earnings': 'financial_report'}
    for risk in row['risks']:
        text = risk['text']
        if text.startswith('数据覆盖不足，未确认低风险：'):
            domains = [aliases.get(d.strip(), d.strip()) for d in text.split('：', 1)[1].split(',')]
            unknown = [d for d in domains if d not in GAPS]
            if unknown:
                adjusted['risks'].append(dict(risk, text='数据覆盖不足，未确认低风险：' + '、'.join(GAPS.get(d, d) for d in unknown)))
        elif risk['field'] != 'risk_level' or text != 'unknown':
            adjusted['risks'].append(risk)
    if not adjusted['risks']:
        return []
    rendered = _material_risks(adjusted)
    if rendered == ['风险信息未知，不代表已核实低风险']:
        return []  # Zero scores/false flags must not recreate generic per-stock gaps.
    return [text.replace('冻结风险标签为中等，仍有未核实风险', '原风险等级：中')
        .replace('冻结风险标签为低，不代表已核实低风险', '原风险等级：低（未核实完整风险）')
        for text in rendered]


def _warnings(envelope, brief_input):
    category = envelope['strategy_id']
    audit_only = {'restricted_sample', 'restricted_complete_core_metric_eligible_universe',
        'Board heat, industry, news and fundamental context not evaluated.',
        'Collector source/evidence/unit assertions are not cryptographic proof; original response/unit conversion and exchange-session evidence not evaluated.',
        'theme_heat: unsupported optional evidence not evaluated; existing neutral fallback used.',
        'Optional daily refinements not evaluated; snapshot-only scoring branch used.',
        'bounded_sample_not_no_company_risk', 'metadata_only_fulltext_amount_sentiment_unverified'}
    rendered = []
    for risk in envelope['risks']:
        text = risk['text']
        if (text == 'Optional daily refinements not evaluated; snapshot-only scoring branch used.'
                and brief_input['strategy'].get('display_revision') == 1):
            labels = {'momentum_quality': '区间动量等关键技术指标',
                'oversold_reversal': '超跌与反转确认指标', 'capital_heat': '板块热度与技术细化指标'}
            rendered.append('本批使用快照评分，' + labels.get(category, '策略关键日线细化指标') + '未完整评估，不等于完整策略确认')
            continue
        if text in audit_only or text.startswith('technical_not_evaluated: '):
            continue
        if text in ('concept_rankings_unavailable', 'industry_rankings_unavailable') and category not in ('capital_heat', 'market_radar'):
            continue
        if text == 'industry_rankings_unavailable' and brief_input['strategy'].get('industry_observation_verified'):
            rendered.append('原行业排名源不可用；已有Tushare行业分类观测成分等权均值，非行业指数')
            continue
        rendered.append(_warning(text))
    return list(dict.fromkeys(rendered))


def _contradictions(candidate, facts, modern=False):
    values = {facts[i]['key']: facts[i]['value'] for i in candidate['fact_ids']}
    signals = []
    if values.get('net_profit_yoy', 0) < 0:
        signals.append('盈利同比为负，与入选信号存在矛盾，不能据此确认基本面改善')
    day, week = values.get('main_net_inflow'), values.get('inflow_5d')
    if day is not None and week is not None and day * week < 0:
        signals.append('当日净流出、近五日净流入，跨窗口方向分歧' if day < 0 else '当日净流入、近五日净流出，跨窗口方向分歧')
    elif modern and day is not None and day < 0:
        signals.append('当日主力净流出，入选信号尚未获得当日资金支持')
    if modern and values.get('operating_cash_flow', 0) < 0:
        signals.append('披露期经营现金流为负，需核验盈利质量')
    if modern and values.get('decliners', 0) > values.get('advancers', float('inf')):
        signals.append('观测范围下跌家数多于上涨家数，不能把均值表现视为普遍走强')
    if modern and values.get('momentum', 0) < 0:
        signals.append('冻结区间动量为负，相对入选不代表区间上涨')
    return signals


def normalize_brief_llm(llm, rendered):
    """Actual adoption, including size compaction, controls the public status."""
    if llm.get('status') == 'succeeded' and rendered.get('model_paragraphs_adopted') == 0:
        return dict(llm, status='fallback', reason='no_effective_model_paragraphs')
    if llm.get('status') == 'succeeded' and rendered.get('model_adoption', {}).get('core_complete') is False:
        return dict(llm, status='fallback', reason='incomplete_core_explanation')
    return llm


def _payload(text):
    return dict(msg_type='interactive', card=dict(config=dict(wide_screen_mode=True),
        elements=[dict(tag='div', text=dict(tag='plain_text', content=text))]))


def render_brief(title, envelope, detail, llm, brief_input):
    """No I/O, model calls, ranking changes or unfrozen detail URLs."""
    if brief_input.get('format_version') != FORMAT_VERSION:
        raise ValueError('Unknown brief format')
    facts, category = brief_input['facts'], envelope['strategy_id']
    selection = brief_input['strategy'].get('expression_version') == 4
    modern = brief_input['strategy'].get('expression_version') in (2, 3, 4)
    if [r['code'] for r in envelope['candidates']] != [r['code'] for r in brief_input['candidates']]:
        raise ValueError('Candidate set/order changed')
    generated = llm.get('explanation')
    if generated:
        generated = validate_brief_explanation(canonical(generated), brief_input)
    generated = generated or dict(summary=[], cross_signals=[], candidates=[])
    interpretations = {r['code']: r for r in generated['candidates']}
    metric_ids = brief_metric_ids(brief_input)
    visible_metrics = {code: set(refs) | ({f'{code}:allocation:weight'} if category == 'etf_rotation' else set())
        for code, refs in metric_ids.items()}
    heading_title = title
    if brief_input['strategy'].get('display_revision') == 1 and isinstance(title, str):
        heading_title = title.removesuffix(' · ' + envelope['business_date'])
    lines = [f"阶跃 · {public_text(heading_title, 100)} · {envelope['business_date']}"]
    optional_lines, model_lines, model_roles, model_role_weights = {}, set(), {}, {}
    subjects = {r['code']: r['name'] if r['name'] != r['code'] else r['code'] for r in envelope['candidates']}
    for fact in facts.values():
        if fact['subject'].startswith('industry:'):
            subjects.setdefault(fact['subject'], fact['subject'].removeprefix('industry:'))
    subjects['market'] = '已观测市场'
    name_counts = Counter(row.get('name') for row in brief_input['candidates'])

    def short_name(row):
        name, code = row.get('name'), row['code']
        return _name(code, name) if name and name_counts[name] > 1 else name if name and name != code else code

    def optional(text, replacement=None, role=None, weight=1):
        optional_lines[len(lines)] = replacement
        model_lines.add(len(lines))
        model_roles[len(lines)] = role
        model_role_weights[len(lines)] = weight
        lines.append(text)
    def segments(items, with_subject=True):
        return _segments(items, facts, visible_metrics, subjects if modern and with_subject else None, labels=not selection)
    summary = segments(generated['summary'])
    if summary:
        model_lines.add(len(lines))
        model_roles[len(lines)] = 'summary'
    lines.append(summary or '核心判断：' + brief_input['strategy']['definition'] + '。')
    c = envelope['coverage']
    if category == 'announcement_risk':
        lines.append(f"近{c.get('window_days', 3)}日公告线索，按日期/来源展示，非风险排名。")
    else:
        lines.append('候选按原规则顺序展示，限定样本不代表全市场。')
    if c.get('contract_version') == 2 and category not in ('etf_rotation', 'market_radar', 'announcement_risk'):
        lines.append('限定研究结果：主题因子缺少已核验板块证据，非完整全因子版本；权重未重归一化。')
    if category == 'etf_rotation':
        window = brief_input['strategy']['etf_lookback_days']
        lines.append(f'动量窗口：{window}个交易日；以百分比展示区间变化。' if window else '动量窗口未知，不补作任意区间解释。')
    breadth = [facts[f'market:breadth:{key}'] for key in ('advancers', 'decliners', 'flat') if f'market:breadth:{key}' in facts]
    if breadth:
        lines.append('市场广度：' + '、'.join(f['display'] for f in breadth) + '（已观测样本）。')
    industry_subjects = sorted({f['subject'] for f in facts.values() if f.get('scope') == 'equal_weight_observed_constituents'},
        key=lambda subject: (facts.get(subject + ':industry:rank', {}).get('value', float('inf')), subject))
    # A short report-level context, not another industry ranking table.
    for subject in industry_subjects[:2]:
        values = {f['key']: f for f in facts.values() if f['subject'] == subject}
        keys = ('change_pct', 'advancers', 'decliners', 'flat')
        line = subject.removeprefix('industry:') + '：' + '、'.join(values[k]['display'] for k in keys if k in values)
        if 'advancers' in values and 'decliners' in values and values['decliners']['value'] > values['advancers']['value']:
            line += '；成分分化，均值不代表多数走强'
        lines.append(line + '（观测成分等权，非行业指数）。')
    cross_signals = segments(generated['cross_signals'])
    if cross_signals:
        optional('交叉观察：' + cross_signals, role='cross_signals')
    if category == 'announcement_risk':
        names = {r['code']: r['name'] for r in envelope['candidates']}
        for index, record in enumerate(detail['records'], 1):
            lines.extend(['', f"{index}. {_name(record['issuer_code'], names.get(record['issuer_code']))} · {record['published_date']}",
                record['title'], '来源：' + record['source']])
            if record.get('url'):
                lines.append('来源链接/公告索引：' + record['url'])
        lines.append('正文、金额与情绪未经核实；未见记录不代表公司无风险。')
        if selection:
            grouped = {}
            for candidate in brief_input['candidates']:
                generated_row = interpretations.get(candidate['code'], {})
                for key, label in (('rationale', '解读'), ('watch', '待观察'), ('invalidation', '失效条件')):
                    grouping = brief_input['strategy'].get('announcement_grouping') == 1
                    section = segments(generated_row.get(key, []), with_subject=not grouping)
                    fallback = ('解读：仅按冻结公告元数据展示，正文仍待核验。' if key == 'rationale' else
                        '待观察：后续核验公告原文与披露背景。' if key == 'watch' else None)
                    if grouping:
                        text = section or (fallback.split('：', 1)[1] if fallback else None)
                        if text:
                            group = grouped.setdefault((key, text, bool(section)), dict(names=[], fallback=fallback, label=label))
                            group['names'].append(_name(candidate['code'], names.get(candidate['code'])))
                    elif section:
                        optional(label + '：' + section, fallback, role=key)
                    elif fallback:
                        lines.append(fallback)
            for (key, text, adopted), group in grouped.items():
                prefix = '、'.join(group['names']) + ' · '
                if adopted:
                    optional(prefix + group['label'] + '：' + text,
                        prefix + group['fallback'] if group['fallback'] else None, role=key, weight=len(group['names']))
                else:
                    lines.append(prefix + group['label'] + '：' + text)
    else:
        for row, candidate in zip(envelope['candidates'], brief_input['candidates']):
            code = row['code']
            industry = facts.get(f'{code}:boards:industry', {})
            suffix = (' · ' + industry['value']) if (modern and category not in ('etf_rotation', 'market_radar')
                and industry.get('date') == envelope['business_date'] and industry.get('source') and industry.get('value')) else ''
            lines.extend(['', f"{row['rank']}. {_name(code, row['name'])}" + suffix])
            selected = [facts[ref] for ref in metric_ids[code]]
            lines.append('；'.join(f['display'] + (f"（{f['date']}）" if f['domain'] not in ('price', 'valuation', 'breadth', 'industry') else '') for f in selected))
            if category not in ('etf_rotation', 'market_radar'):
                reason = row.get('ranking_reason', '')
                # Quantitative source reasons remain in frozen detail; they
                # cannot bypass the shared three-field public metric budget.
                if not modern and reason and not re.search(r'\d|百分之|[零一二三四五六七八九十百千万亿]+(?:倍|元|%)', reason):
                    lines.append('入选线索：' + reason[:60] + ('…' if len(reason) > 60 else ''))
                risks = _risks(row)
                if risks:
                    lines.append('风险：' + '；'.join(risks))
            generated_row = interpretations.get(code, {})
            contradictions = _contradictions(candidate, facts, modern)
            interval_ratio = facts.get(f'{code}:price:volume_ratio_20d', {})
            if (category == 'shrink_pullback' and brief_input['strategy'].get('display_revision') == 1
                    and interval_ratio.get('id') in metric_ids[code]
                    and type(interval_ratio.get('value')) in (int, float) and interval_ratio['value'] >= 1):
                contradictions.append('所示区间量比未体现缩量，原规则入选不等于缩量形态已确认')
            reason = row.get('ranking_reason', '')
            if not reason or re.search(r'\d|百分之|[零一二三四五六七八九十百千万亿]+(?:倍|元|%)', reason):
                reason = brief_input['strategy']['definition']
            fallback_reason = '；'.join(contradictions) if contradictions else reason[:60]
            for key, label in (('rationale', '解读'), ('watch', '待观察'), ('invalidation', '失效条件')):
                section = segments(generated_row.get(key, []), with_subject=False)
                fallback_text = ('解读：' + fallback_reason if key == 'rationale' else
                    '待观察：' + CANDIDATE_WATCH.get(category, '后续核验同口径规则事实能否延续。') if key == 'watch' else None)
                if section:
                    optional(label + '：' + section, fallback_text if modern else None, role=key)
                elif modern and fallback_text:
                    lines.append(fallback_text)
            if contradictions and (not modern or segments(generated_row.get('rationale', []))):
                lines.append('交叉观察：' + '；'.join(contradictions) + '。')
            comparison = facts.get(f'{code}:cross_strategy:strategies')
            if comparison and not modern:
                lines.append('榜单重合：' + comparison['value'] + '（仅同日已完成Top N，非全策略共识）。')
    if modern:
        overlaps = {}
        for row in brief_input['candidates']:
            fact = facts.get(f"{row['code']}:cross_strategy:strategies")
            if fact:
                overlaps.setdefault(fact['value'], []).append(short_name(row))
        if overlaps:
            # Merge equal overlap sets; bound auxiliary prose, not candidate Top N.
            groups = list(overlaps.items())
            descriptions = ['、'.join(names) + '见于' + names_of_strategies for names_of_strategies, names in groups[:2]]
            if len(groups) > 2:
                descriptions.append(f'另有{sum(len(names) for _, names in groups[2:])}只存在榜单重合，明细已冻结')
            lines.extend(['', f'联合观察：{sum(len(names) for _, names in groups)}只候选存在重合，' + '；'.join(descriptions)
                + '。仅本数据日已完成Top N重合，非独立概率或全策略共识。'])
        industries = Counter(facts[f"{row['code']}:boards:industry"]['value'] for row in brief_input['candidates']
            if f"{row['code']}:boards:industry" in facts and facts[f"{row['code']}:boards:industry"].get('date') == envelope['business_date'])
        if industries and industries.most_common(1)[0][1] > 1:
            industry, count = industries.most_common(1)[0]
            lines.append(f'行业集中：已知行业的{sum(industries.values())}只候选中，{industry}占{count}只；未知行业不计入。')
    if detail.get('research_weights'):
        names = brief_input['instruments']
        lines.extend(['', '研究权重（不因展示切片重算，非交易指令）：' + '；'.join(
            _name(code, names.get(code, {}).get('name')) + f"{detail['research_weights'][code]:.2%}" for code in sorted(detail['research_weights']))])
    if category not in ('etf_rotation', 'market_radar', 'announcement_risk'):
        groups = []
        for domain in IMPORTANT_GAPS.get(category, ()):
            missing = [short_name(r) if modern else r['code'] for r in brief_input['candidates'] if domain in r['missing']]
            if missing:
                groups.append(GAP_LABELS[domain] + ('' if len(missing) == len(brief_input['candidates']) else '（' + '、'.join(missing) + '）'))
        if groups:
            lines.extend(['', '待核验：' + '、'.join(groups) + '尚未补齐，不能把规则入选当作这些方面已获确认。'])
    warnings = _warnings(envelope, brief_input)
    if warnings:
        lines.extend(['', '限制/警示：' + '；'.join(warnings)])
    lines.extend(['', '下一步：' + WATCH.get(category, '后续核验趋势、量价与披露证据；若方向背离，原规则信号的解释需重评。'),
        '仅供研究，非投资建议；未列出不等于风险已核实。来源、原始风险与审计明细见冻结报告。'])
    text = '\n'.join(lines)
    payload = _payload(text)
    if len(canonical(payload).encode('utf-8')) > 16 * 1024 and optional_lines:
        # Compact elaborations, never candidate identities, specific material
        # risks, contradictions, report judgment or conditional next-watch.
        text = '\n'.join(optional_lines[index] if index in optional_lines else line
            for index, line in enumerate(lines) if index not in optional_lines or optional_lines[index])
        model_lines.difference_update(optional_lines)
        text += '\n扩展解读与背景数值保留在冻结报告；卡片保留全部候选与具体风险。'
        payload = _payload(text)
    result = dict(format_version=FORMAT_VERSION, rendered_content=text, card_payload=payload)
    if modern:
        result['model_paragraphs_adopted'] = len(model_lines)
    if selection:
        counts = Counter(model_roles.get(index) for index in model_lines)
        rationale_count = sum(model_role_weights.get(index, 1) for index in model_lines if model_roles.get(index) == 'rationale')
        result['model_adoption'] = dict(summary=counts['summary'], cross_signals=counts['cross_signals'],
            candidate_rationale_count=rationale_count, watch=counts['watch'], invalidation=counts['invalidation'],
            watch_only=bool(counts['watch'] + counts['invalidation']) and not (counts['summary'] + counts['cross_signals'] + counts['rationale']),
            core_complete=bool(counts['summary']) and rationale_count == len(brief_input['candidates']))
    return result


def frozen_payload(document):
    """Approve the complete versioned payload against frozen content only."""
    if 'format_version' not in document:
        if document.get('card_payload'):
            raise ValueError('Unversioned card')
        return dict(msg_type='text', content=dict(text=document['rendered_content']))
    if document['format_version'] != FORMAT_VERSION:
        raise ValueError('Unknown brief format')
    approved = render_brief(document['title'], document['envelope'], document['detail'], document['llm'], document['brief_input'])
    if any(document.get(key) != value for key, value in approved.items()):
        raise ValueError('Frozen brief changed')
    return approved['card_payload']
