# 数据完整性与选股可信度修复计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 尽量补齐已有权限可取得的数据，纠正复权、新闻关联、AI 截断与缺失数据风险解释。

**Architecture:** 复用现有数据管理器和筛选引擎，不新增平行选股系统。Tushare 原生补充优先用于可验证的基础数据，保留现有免费备用链。数据必须保留来源和时点，缺失不等于零或低风险。

**Tech Stack:** Python、Pandas、现有 Tushare HTTP 客户端、LiteLLM、pytest。

## Global Constraints

- 用户已批准《功能策略核查.md》所述修复；本计划落实该范围，不购买服务、不部署、不下单、不启用调度。
- 保留用户工作树与配置，不 commit/tag/push，不输出密钥。
- 不用旧数据冒充实时数据，不编造缺失字段，不以降低策略条件换取候选。
- 兼容既有 API 字段；新增来源/缺失说明通过现有诊断结构传递。
- 使用现有专属虚拟环境，离线回归后再少量真实验收。

### Task 1: 日线复权与历史有效性

Files: `src/services/screening_service.py`, `src/services/screening/daily.py`, `tests/test_data_completeness.py`。

- [x] 测试 DSA 返回未复权/未知口径日线时不得作为已确认前复权数据。
- [x] 优先现有原生复权路径；已确认同口径的数据可复用；失败保留诊断，不冒充调整完成。
- [x] 复权因子必须按日期匹配，禁止向未来借因子填缺口；保留调整与末根日期元数据。
- [x] `python -m pytest tests/test_data_completeness.py tests/test_screening_daily_freshness.py`。

Assertions:
```python
assert history.attrs['price_adjustment'] == 'forward'
assert history.attrs['daily_source'] == 'tushare'
```

### Task 2: 已有 Tushare 权限下的基本面补齐

Files: `data_provider/tushare_fundamental_adapter.py`, `data_provider/tushare_fetcher.py`, `data_provider/base.py`, `tests/test_data_completeness.py`。

- [x] 用现有 Key 逐接口做只读小样本诊断，不把 timeout 归因于权限不足。
- [x] 先测试，再接入估值、行业、成长、财务、股东与个股资金流补充；逐块状态、来源、日期、错误，真实零值保留。
- [x] 使用预算内请求与已有缓存；不因一个失败接口丢弃其他成功数据；无 Tushare 时既有行为不变。
- [x] 单位按供应商合同归一；财务同报告期、披露日期不超当前日期；资金流注明日终口径。
- [x] 数据管理器在慢备用链之前使用补充成功块；缺失块继续走原备用路径。
- [x] `python -m pytest tests/test_data_completeness.py tests/test_fundamental_context.py tests/test_fundamental_timeout_guard.py`。

Assertions:
```python
assert context['valuation']['data']['total_mv'] == 10000 * raw_total_mv
assert context['growth']['data']['roe'] == 0
assert context['coverage']['growth'] != 'ok' if missing_required_field else True
```

### Task 3: 新闻、行业与缺失证据传播

Files: `src/search_service.py`, `src/services/screening_service.py`, `src/services/screening/snapshot.py`, `tests/test_data_completeness.py`。

- [x] 测试不相关 HDFC 新闻不能成为民生银行催化证据；只剩零关联结果时返回空结果与说明。
- [x] 补充主体行业、候选基本面覆盖警告；缺失关键字段不显示已确认低风险。
- [x] 对快照日期冲突保留明确警告，不能仅凭抓取成功标为今日有效。
- [x] `python -m pytest tests/test_data_completeness.py tests/test_search_news_freshness.py tests/test_screening_explanations.py tests/test_screening_event_context.py`。

Assertions:
```python
assert unrelated_news not in admitted.results
assert 'fundamental' in ' '.join(candidate['dsa_context']['warnings'])
```

### Task 4: AI 排序输出与真实验收

Files: `src/services/screening/ranker.py`, `src/services/screening/config.py`, `.env.example`, `docs/core-reliability.md`, `docs/CHANGELOG.md`。

- [x] 保留截断拒绝；缩短输出要求，合理提高默认输出预算但尊重显式上限。
- [x] 确认已配置模型支持的非思考排序参数，只用于短结构化重排，其他分析不变。
- [x] 用确定性测试验证调用参数、失败降级、未匹配候选不伪造模型分数。
- [x] 跑受影响后端回归；用真实数据复核覆盖率与一个完整选股任务，记录未解决项。
- [x] 更新文档，独立代码审查，再交付。未授权提交，保留本地改动与回滚备份。

Assertions:
```python
assert truncated_result.ranked is False
assert unreviewed_pick.llm_score is None
```

## Progress

- 已读既有诊断、调用链和仓库约束。现有分支不是 main/master；原工作树含用户未提交修改。
- 各任务共享 `screening_service.py` 的元数据结构，串行实施，避免覆盖。
- 当前缺少可加载的 test-driven-development / verification-before-completion 技能，按先失败回归、再修复、再真实验收执行。
- 不创建新 worktree：用户要求在当前二次开发基础修复，当前含必要的未提交功能；保留原目录并备份受影响文件。

- 完成：829 项回归通过，21 项 Windows 跨时区场景跳过；前端构建与 Python 编译通过。独立审查无剩余阻断。
- 真实验收：3 只基本面、2 只复权日线、质量价值完整流程两次成功；最终 run_id=47dffe808324，5559 只日终快照，473 只规则候选，3 只输出，LLM 输出代码覆盖 100%。覆盖率不等于数据齐全。
- 留有边界：行业资金排名、机构变化汇总、分红事件、多日龙虎榜和部分新闻仍可能缺失，详见 docs/data-completeness-repair.md。无新增服务购买，无提交或远端部署。
