"""Pure allowlist adapter and text renderer for frozen current-day research."""
from copy import deepcopy
from datetime import datetime
import math
import re
from zoneinfo import ZoneInfo

from src.services.research_delivery.aggregation import METRICS, RISK_FIELDS, safe_text, safe_url
from src.services.research_delivery.timing import (
    PUBLICATION_WINDOW_END,
    PUBLICATION_WINDOW_START,
)


COVERAGE_KEYS = ('scope', 'independent_universe_count', 'daily_response_count', 'daily_basic_response_count',
    'contract_version', 'evaluation_scope_count', 'evaluated_count', 'pending_count',
    'daily_response_coverage', 'daily_basic_response_coverage', 'eligible_count', 'unknown_missing_count',
    'required_count', 'valid_count', 'technical_limit', 'snapshot_pass_count', 'technical_planned_count',
    'technical_not_evaluated_count', 'asset_count', 'valid_asset_count', 'history_years',
    'industry_available', 'concept_available', 'scope_count', 'scanned_count', 'unscanned_count',
    'failed_count', 'verified_count', 'excluded_record_count', 'window_days', 'limit_per_code')
EQUITY_METRICS = tuple(dict.fromkeys(('final_score', *METRICS)))
RADAR_METRICS = ('change_pct', 'rank', 'advancers', 'decliners', 'flat', 'observed_count', 'turnover_yuan')
METRIC_LABELS = {'final_score': '规则综合分', 'screen_score': '筛选分', 'score': '规则分',
    'price': '现价/元', 'close': '收盘价/元', 'change_pct': '涨跌幅/%', 'amount': '成交额/元',
    'total_mv': '总市值/元', 'turnover_rate': '换手率/%', 'volume_ratio': '量比/倍',
    'pe_ratio': '市盈率/倍', 'pb_ratio': '市净率/倍', 'change_60d': '区间涨跌/%',
    'momentum': '动量/小数比例', 'turnover_yuan': '观测成交额/元',
    'advancers': '上涨数', 'decliners': '下跌数', 'flat': '平盘数', 'observed_count': '观测数'}


def public_text(value, limit=4000):
    text = safe_text(value, limit)
    # Public text is data, not Markdown/HTML; do not leak local evidence/vault paths.
    if re.search(r'(?i)([a-z]:[/\\]|\\\\|(?:^|\s)/(?:[^\s/]+/)|file:|vault:|credential_ref)', text):
        return '[非公开文本已省略]'
    return text.replace('<', '‹').replace('>', '›').replace('`', "'")


def _numeric(row, keys):
    return {key: row.get(key) if type(row.get(key)) in (int, float) and math.isfinite(row[key]) else None
            for key in keys if key in row}


def _risks(row):
    result = []
    for key in RISK_FIELDS:
        value = row.get(key)
        for item in value if isinstance(value, list) else [value]:
            if item not in (None, '', []):
                result.append({'field': key, 'text': public_text(item), 'kind': 'frozen_risk'})
    return result


def formal_envelope(source, top_n, *, format_version=None):
    """Never copy raw provider objects, endpoint captures, paths or universe lists."""
    category, result = source['category_id'], source['result']
    coverage = {k: public_text(v, 150) if isinstance(v, str) else v for k, v in source['coverage'].items()
                if k in COVERAGE_KEYS and type(v) in (str, int, float, bool)}
    envelope = dict(strategy_id=category, ranking_mode='deterministic_source_order',
        business_date=source['business_date'], coverage=coverage, candidates=[], evidence=[],
        risks=[dict(field='producer_warning', text=public_text(v)) for v in source['warnings']], missing=[], blockers=[])
    records = []
    if category == 'announcement_risk':
        records = [{k: public_text(row[k], 600) for k in ('issuer_code', 'title', 'published_date', 'source', 'evidence_kind')}
                   | {'url': safe_url(row.get('url'))} for row in result['records'][:top_n]]
        for row in records:
            code = row['issuer_code']
            if code not in {r['code'] for r in envelope['candidates']}:
                envelope['candidates'].append(dict(code=code, name=code, rank=len(envelope['candidates']) + 1,
                    metrics={}, risks=[], missing=['metadata_only_fulltext_amount_sentiment_unknown'], ranking_reason='公告元数据源顺序，非风险排序'))
            envelope['evidence'].append(dict(id='announcement:' + str(len(envelope['evidence'])), issuer_code=code,
                issuer_status='exact_stored_symbol', date_status='stored_publication_date',
                title=row['title'], summary='', published_at=row['published_date'], source_name=row['source'], url=row['url']))
        envelope['missing'].append('标题元数据：正文、金额与情绪未经核实；未见记录不代表公司无风险')
        total = len(result['records'])
    else:
        stock = category not in ('etf_rotation', 'market_radar')
        rows = result['picks'] if stock else result['items']
        total = len(rows)
        if stock:
            envelope['ranking_mode'] = result['ranking_mode']
            if source['coverage'].get('contract_version') == 2:
                envelope['missing'].append('限定范围研究结果；主题因子缺少已核验板块证据，非完整全因子版本；权重未重归一化')
        for index, row in enumerate(rows[:top_n]):
            metrics = _numeric(row if stock else row['metrics'], EQUITY_METRICS if stock else
                ('momentum', 'close') if category == 'etf_rotation' else RADAR_METRICS)
            risks = _risks(row)
            envelope['candidates'].append(dict(code=row['code'] if stock else row['id'],
                name=public_text(row.get('name', ''), 120), rank=row['rank'] if stock else index + 1,
                metrics=metrics, risks=risks, missing=['unknown:' + k for k, v in metrics.items() if v is None]
                    + ([] if risks or not stock else ['risk_information_unknown']),
                ranking_reason=public_text(row.get('ranking_reason', ''), 600),
                source=public_text(result.get('snapshot_source', 'tushare') if stock else row['source'], 100),
                as_of=source['business_date'] if stock else row['as_of']))
        if category == 'etf_rotation':
            envelope['missing'].append('参考研究权重；未验证执行日历；非交易指令；不展示回测收益')
        elif category == 'market_radar':
            envelope['missing'].append('仅已观测快照或已核验主题子集，不代表全市场主题排名')
    evidence = [{k: public_text(entry[k], 120) for k in ('source', 'endpoint', 'dataset', 'business_date', 'fetched_at', 'sha256') if k in entry}
                for entry in source['evidence']]
    weights = result.get('target_weights', {}) if category == 'etf_rotation' else {}
    weights = {code: value for code, value in weights.items()
        if isinstance(code, str) and re.fullmatch('[0-9]{6}', code)
        and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1} if isinstance(weights, dict) else {}
    detail = dict(records=records, shown_count=min(top_n, total), total_count=total,
        sources=evidence, run_id=public_text(result.get('run_id', ''), 100),
        research_weights=weights)
    if format_version is not None:
        if format_version != 'research-brief-v1':
            raise ValueError('Unknown formal format')
        context = result.get('research_context', {})
        identities = context.get('instruments', {}) if context.get('business_date') == source['business_date'] else {}
        for row in envelope['candidates']:
            identity = identities.get(row['code'], {})
            if identity.get('as_of') == source['business_date'] and identity.get('name'):
                row['name'] = public_text(identity['name'], 120)
        detail['format_version'] = format_version
        detail['audit'] = dict(coverage=deepcopy(coverage), warnings=deepcopy(envelope['risks']))
    return envelope, detail


def prompt_envelope(envelope):
    """Small model view; the batch hash still binds the complete audit object."""
    compact = deepcopy(envelope)
    compact['risks'] = [{**row, 'text': public_text(row['text'], 250)} for row in compact['risks'][:12]]
    for row in compact['candidates']:
        row['risks'] = [{**risk, 'text': public_text(risk['text'], 250)} for risk in row['risks'][:12]]
    return compact


WARNING_TEXT = {
    'restricted_sample': '仅覆盖限定样本',
    'restricted_complete_core_metric_eligible_universe': '仅筛选核心指标完整的限定样本，并非全市场排名',
    'bounded_observed_bars_not_exchange_contiguous': '历史指标基于有限已观测日线，不保证交易日连续',
    'Board heat, industry, news and fundamental context not evaluated.': '板块热度、行业、新闻与基本面背景未评估',
    'Collector source/evidence/unit assertions are not cryptographic proof; original response/unit conversion and exchange-session evidence not evaluated.':
        '第三方原始响应、单位换算与交易所会话证据未经独立交叉核验；存证哈希不等于数据真实性认证',
    'theme_heat: unsupported optional evidence not evaluated; existing neutral fallback used.': '缺少可支持的主题热度证据，使用中性回退，未作热度评估',
    'Optional daily refinements not evaluated; snapshot-only scoring branch used.': '可选日线细化指标未评估，本次使用快照评分分支',
    '61-bar minimum warmup only; full MACD convergence and contiguous exchange sessions not evaluated.':
        '仅满足最低61条日线预热，未验证MACD充分收敛或交易日连续性',
    'concept_rankings_unavailable': '概念排名数据缺失', 'industry_rankings_unavailable': '行业排名数据缺失',
    'hotspot_source_failed': '热点来源获取失败', 'hotspot_stale_undated_or_invalid_rows_excluded': '已排除过期、无日期或无效热点',
    'hotspot_invalid_rows_excluded': '已排除无效热点数据',
    'one_year_history_signal_only': '仅使用一年历史生成研究信号',
    'rebalance_session_unverified_reference_only': '未验证调仓执行日历，仅供研究参考',
    'etf_default_single_tencent_provider': 'ETF采用单一腾讯数据源，未独立交叉核验',
    'etf_provider_attempt_rejected': '部分ETF来源尝试已被拒绝',
    'etf_historical_quotes_missing': '部分ETF历史报价缺失', 'etf_mixed_forward_adjusted_sources': 'ETF前复权数据来自多个来源',
    'announcement_scope_partially_unscanned': '公告范围中仍有公司未扫描',
    'bounded_sample_not_no_company_risk': '仅扫描有限样本，未见公告不代表公司无风险',
    'metadata_only_fulltext_amount_sentiment_unverified': '仅核验公告标题元数据，正文、金额与情绪未经核实',
    'announcement_partial_source_failure': '部分公告来源失败',
    'announcement_unverified_records_excluded': '已排除未经核验的公告记录',
}
RISK_TEXT = {'single_day_chase_risk': '单日追高风险', 'single_day_breakdown_risk': '单日大跌风险',
    'abnormal_volume_ratio': '量比异常', 'high_turnover': '换手率偏高', 'negative_or_invalid_pe': '市盈率为负或无效',
    'high_pb': '市净率偏高', 'weak_daily_signal': '日线信号偏弱', 'macd_bearish': 'MACD偏空',
    'rsi_overbought': 'RSI超买', 'low_llm_confidence': '历史模型判断置信不足', 'low_daily_quality': '日线数据质量偏低',
    'daily_fetch_failed': '日线获取失败', 'daily_stale_cache': '日线缓存过期',
    'daily_source_fallback_errors': '日线回退来源报错', 'bad_daily_quality_flags': '日线价格或成交数据异常'}
GAP_TEXT = {'valuation': '估值', 'growth': '成长', 'earnings': '盈利', 'capital_flow': '资金流', 'boards': '板块'}
STRATEGY_METRICS = {
    'volume_breakout': ('breakout_20d_pct', 'volume_ratio_20d'),
    'shrink_pullback': ('range_20d_pct', 'volume_ratio_20d'),
    'momentum_quality': ('change_60d', 'signal_score'),
    'capital_heat': ('board_heat_score', 'industry_heat_score', 'volume_ratio'),
    'oversold_reversal': ('change_60d', 'max_drawdown_20d_pct'),
    'balanced_alpha': ('signal_score', 'pe_ratio', 'pb_ratio'),
    'dual_low': ('pe_ratio', 'pb_ratio'), 'quality_value': ('pe_ratio', 'pb_ratio'),
    'low_volatility_quality': ('volatility_20d_pct', 'atr_20_pct', 'max_drawdown_20d_pct'),
    'blue_chip_income': ('pe_ratio', 'pb_ratio', 'total_mv'),
}


def _warning(text):
    if text in WARNING_TEXT:
        return WARNING_TEXT[text]
    match = re.fullmatch(r'technical_not_evaluated: ([0-9]+) candidates outside cap\.', text)
    return f'技术计算上限之外仍有{match[1]}只候选未评估' if match else public_text(text)


def _risk_text(text):
    if text.startswith('数据覆盖不足，未确认低风险：'):
        gaps = text.split('：', 1)[1].split(',')
        return '数据覆盖不足，未确认低风险：' + '、'.join(GAP_TEXT.get(v.strip(), public_text(v.strip())) for v in gaps)
    return RISK_TEXT.get(text, public_text(text))


def _material_risks(row):
    rendered = []
    levels = {'unknown': '风险评估未知或不完整', 'low': '冻结风险标签为低，不代表已核实低风险',
        'medium': '冻结风险标签为中等，仍有未核实风险', 'high': '冻结风险标签为高', 'extreme': '冻结风险标签为极高'}
    for risk in row['risks']:
        field, text = risk['field'], risk['text']
        if field == 'risk_level':
            rendered.append(levels.get(text, '风险标签：' + _risk_text(text)))
        elif field in ('risk_score', 'risk_penalty', 'portfolio_penalty'):
            try:
                value = float(text)
            except ValueError:
                rendered.append(_risk_text(text))
            else:
                if value != 0:
                    label = {'risk_score': '冻结风险评分', 'risk_penalty': '风险扣分', 'portfolio_penalty': '集中度扣分'}[field]
                    rendered.append(f'{label} {value:.2f}')
        elif field == 'excluded_by_risk' and text == 'False':
            continue
        else:
            rendered.append(_risk_text(text))
    return list(dict.fromkeys(rendered)) or ['风险信息未知，不代表已核实低风险']


def _metric(key, value):
    labels = {'breakout_20d_pct': ('近20日突破幅度', '%'), 'range_20d_pct': ('近20日振幅', '%'),
        'volume_ratio_20d': ('区间量比', '倍'), 'signal_score': ('技术信号分', ''),
        'volatility_20d_pct': ('近20日波动', '%'), 'max_drawdown_20d_pct': ('近20日最大回撤', '%'),
        'atr_20_pct': ('ATR波动幅度', '%'), 'board_heat_score': ('板块热度分', ''), 'industry_heat_score': ('行业热度分', '')}
    if key in labels:
        label, unit = labels[key]
    else:
        label, _, unit = METRIC_LABELS.get(key, key).partition('/')
    if value is None:
        return label + ' 未知'
    if key in ('amount', 'total_mv', 'turnover_yuan'):
        return f'{label} {value / 100_000_000:.2f}亿元'
    if key == 'momentum':
        return f'动量 {value:.4f}（小数比例）'
    number = f'{value:+.2f}' if key == 'change_pct' else f'{value:.2f}'
    return f'{label} {number}{unit}'


def _coverage(category, c, detail):
    if category == 'announcement_risk':
        return (f"展示 {detail['shown_count']}/{detail['total_count']}条；扫描 {c['scanned_count']}/{c['scope_count']}家公司，"
            f"未扫描 {c['unscanned_count']}家，失败 {c['failed_count']}家。近{c.get('window_days', 3)}日公告元数据，按日期/来源顺序展示，非风险排名。")
    if category == 'etf_rotation':
        return f"展示 {detail['shown_count']}/{detail['total_count']}项；有效资产 {c['valid_asset_count']}/{c['asset_count']}；历史范围 {c['history_years']}年。按冻结动量源顺序展示，非交易指令。"
    if category == 'market_radar':
        return f"展示 {detail['shown_count']}/{detail['total_count']}项；仅为已观测快照或已核验主题子集，不代表全市场主题排名。"
    n = c['independent_universe_count']
    text = (f"展示 {detail['shown_count']}/{detail['total_count']}只；规则排序（评分不是胜率）。\n"
        f"覆盖：行情 {c['daily_response_count']}/{n}（{c['daily_response_coverage']:.1%}）；"
        f"基础指标 {c['daily_basic_response_count']}/{n}（{c['daily_basic_response_coverage']:.1%}）；"
        f"核心指标完整样本 {c['eligible_count']}只；当日源数据缺失 {c.get('unknown_missing_count', '未知')}只。\n"
        '仅筛选核心指标完整的限定样本，未对全市场全部股票完成排名。')
    if c.get('contract_version') == 2:
        text += (f"\n完整评估 {c['evaluated_count']}/{c['evaluation_scope_count']}只；每批预算 {c['technical_limit']}只，"
            f"待采 {c['pending_count']}只，失败 {c['failed_count']}只。主题因子证据受限，非完整全因子版本。")
    elif c.get('technical_planned_count', 0) or c.get('technical_not_evaluated_count', 0):
        text += (f"\n技术计划 {c.get('technical_planned_count', '未知')}只，上限 {c['technical_limit']}只，"
            f"未评估 {c.get('technical_not_evaluated_count', '未知')}只。")
    return text + f"\n必需字段校验：{c['valid_count']}/{c['required_count']}个字段有效（不是股票数量）。"


def render_formal(title, envelope, detail, llm, batch_id):
    category = envelope['strategy_id']
    stock = category not in ('etf_rotation', 'market_radar', 'announcement_risk')
    lines = [f"阶跃 · {title} · {envelope['business_date']}", _coverage(category, envelope['coverage'], detail),
        f'发布窗口：当日{PUBLICATION_WINDOW_START}–{PUBLICATION_WINDOW_END}（上海时间）；数据日见标题。']
    risks = [_material_risks(row) for row in envelope['candidates']] if stock else []
    # Unattributed consolidation is safe only when every displayed candidate
    # shares the gap. Subset gaps and unique flags stay with their candidates.
    shared = {text for row in risks for text in row if text.startswith('数据覆盖不足，未确认低风险：')
        and len(risks) > 1 and all(text in other for other in risks)}
    if category == 'announcement_risk':
        for index, row in enumerate(detail['records'], 1):
            lines.append(f"{index}. {row['issuer_code']} | {row['published_date']} | {row['title']} | {row['source']}")
            if row['url']:
                lines.append('来源链接/公告索引：' + row['url'])
    else:
        for index, row in enumerate(envelope['candidates']):
            metrics = row['metrics']
            head = f"{row['rank']}. {row['name']}（{row['code']}）"
            if stock:
                lines.append(head + f" · 规则分 {metrics['final_score']:.2f} / 筛选分 {metrics['screen_score']:.2f}")
                lines.append('；'.join(_metric(k, metrics.get(k)) for k in ('price', 'change_pct', 'amount', 'volume_ratio', 'turnover_rate')))
                selected = [_metric(k, metrics[k]) for k in STRATEGY_METRICS.get(category, ()) if metrics.get(k) is not None]
                if selected:
                    lines.append('规则观察：' + '；'.join(selected) + '。仅为冻结指标，不证明信号将持续。')
                if row['ranking_reason']:
                    lines.append('规则原因：' + row['ranking_reason'])
                lines.append('风险：' + '；'.join(text for text in risks[index] if text not in shared))
            elif category == 'etf_rotation':
                lines.extend([head, '；'.join(_metric(k, metrics.get(k)) for k in ('close', 'momentum'))])
            else:
                lines.append(head + ' · ' + '；'.join(_metric(k, v) if k == 'turnover_yuan' or k == 'change_pct'
                    else f"{METRIC_LABELS.get(k, '来源序号' if k == 'rank' else k)} {v:g}" for k, v in metrics.items()))
            lines.append(f"来源：{row['source']}；截至：{row['as_of']}")
    if shared:
        lines.append('共同评估缺口：' + '；'.join(sorted(shared)))
    if stock and any(row['missing'] for row in envelope['candidates']):
        lines.append('其他未知：部分可选行业、板块或财务指标缺失，未展示不等于已核实；完整缺失项保留在冻结报告。')
    if detail['research_weights']:
        lines.append('原始研究权重（展示切片不重新归一，非交易指令）：' + '；'.join(f'{k} {v:.2%}' for k, v in detail['research_weights'].items()))
    if envelope['risks']:
        lines.append('限制/警示：' + '；'.join(dict.fromkeys(_warning(r['text']) for r in envelope['risks'])))
    if envelope['missing']:
        lines.append('缺失/未知：' + '；'.join(envelope['missing']))
    groups = {}
    for source in detail['sources']:
        key = '/'.join(str(source.get(k, '')) for k in ('source', 'endpoint', 'dataset')).strip('/')
        stamp = source.get('fetched_at', '')
        try:
            stamp = datetime.fromisoformat(stamp).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S')
        except ValueError:
            stamp = public_text(stamp)
        groups.setdefault(key, []).append(stamp)
    lines.append('证据来源/采集时间（上海时间）：' + '；'.join(
        f'{key} {len(stamps)}份 [{min(stamps)}~{max(stamps)}]' for key, stamps in groups.items()))
    status = {'off': '已关闭', 'fallback': '未采用，保留规则事实', 'succeeded': '已通过严格引用校验'}.get(llm['status'], llm['status'])
    reason = {'model_timeout': '模型调用超时', 'invalid_model_output': '模型输出未通过校验',
        'unsupported': '当前模型路由不支持', 'not_configured': '未配置可用模型', 'no_supported_evidence': '缺少可支持的解释证据',
        'attempt_in_progress_or_unknown': '解释请求进行中或结果未知', 'model_request_failed': '模型请求失败',
        'daily_budget_exhausted': '当日模型预算已用尽'}.get(llm.get('reason'), llm.get('reason'))
    lines.append('AI解释状态：' + status + ((' / ' + str(reason)) if reason else ''))
    for row in (llm.get('explanation') or {}).get('candidates', []):
        for reason in row['reasons']:
            lines.append(f"{row['code']} · {'推断' if reason['kind'] == 'inference' else '事实'}：{public_text(reason['text'])}")
    lines.extend([f"运行ID：{detail['run_id'] or batch_id}；批次：{batch_id}",
        '仅供研究，非投资建议。数据缺失与样本限制仍然存在；平台接受不代表群成员已阅读。'])
    return '\n'.join(lines)
