# 飞书研究推送第一批：数据就绪核心 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 每个任务完成后做规格与代码审查；本仓库禁止未获明确确认的 commit/tag/push，故本计划用“审查并记录差异”代替自动提交。

**Goal:** 实现可离线测试的数据依赖、冻结评估范围、严格就绪判断和发布时窗核心，作为后续真实采集/定时任务共用的基础。

**Architecture:** 使用不可变小型数据结构和纯函数，不在导入时加载秘密，不调用网络、模型、数据库或 sender。复用现有策略元数据；之后由 W1 的真实采集适配器生成逐字段凭据，再接 W3 调度和 W5 投递。

**Tech Stack:** 现有 Python 3.10+ 语法、dataclasses、zoneinfo、pytest；复用项目内的 screening StrategyInfo，不新增依赖。

## Global Constraints

- 时区 `Asia/Shanghai`；正式发布不能用未知交易日历或未知股票池。
- “声明范围”分层冻结：快照验收覆盖订阅股票池；技术验收覆盖按已声明规则选出的前 N 初筛池。
- 未进入前 N 的股票标记“未做技术评估”，不能记作“不满足技术条件”。
- 数据缺失不能当作有效空结果；可选信息缺失只产生明确降级说明。
- 第一批不实际补采、不创建在线定时、不改服务运行状态、不发飞书、不调用 LLM。
- 下列核心文件与测试已按先失败测试再实现完成，18 项定向测试通过且独立审查通过；扩展适配与最终合并验收单独记录。
- 本批门禁只验证输入质量凭据与覆盖，不能凭凭据证明原始数据数值真实。真实适配器必须在 W1 完成单位/复权/有效 K 线数/字段有限值/证据对应校验后才能出具 `valid`。
- 依赖元信息当前主要来自硬筛规则；评分因子、ETF、市场雷达的依赖在各自 W1 适配任务显式补充，不能将本批完成称为整套发布门禁上线。
- 保留现有工作区变更；不 commit/tag/push；不修改旧策略阈值、旧手动查询降级语义。

---

## 文件边界与顺序

仓库根目录：`E:/work/机器学习/daily_stock_analysis`。以下路径均相对该根目录，命令都在根目录执行。

| 文件 | 职责 |
| --- | --- |
| 新建 `src/services/research_delivery/__init__.py` | 无副作用包入口 |
| 新建 `src/services/research_delivery/contracts.py` | 不可变结构与类型 |
| 新建 `src/services/research_delivery/dependencies.py` | 从现有 StrategyInfo 生成依赖 |
| 新建 `src/services/research_delivery/readiness.py` | 冻结范围、检查质量凭据 |
| 新建 `src/services/research_delivery/timing.py` | 纯发布时窗判断，不注册任务 |
| 新建 `tests/test_research_delivery_dependencies.py` | 十策略元数据复用 |
| 新建 `tests/test_research_delivery_readiness.py` | 缺数、旧日期、范围与凭据 |
| 新建 `tests/test_research_delivery_timing.py` | 时区、截止、日历未知 |

任务 1 → 任务 2；任务 3 可与任务 2 并行；任务 4 汇总离线验证与文档。业务入口、API、sender 不在本批修改范围。

## Task 1：复用现有策略依赖，建立不可变契约

**Files:** Create `contracts.py`、`dependencies.py`、包 `__init__.py`；Test `tests/test_research_delivery_dependencies.py`。

**Interfaces:** Consumes 既有 `StrategyInfo`；Produces `DependencyManifest`、`CellKey`、`FieldReceipt`、`FrozenScope`、`ReadinessResult` 和 `build_manifest(info: StrategyInfo) -> DependencyManifest`，供 Task 2 使用。

- [x] **Step 1：写失败测试。** 新建测试文件，完整内容如下。给 `list_strategies` 传显式路径，不调用 `Config.from_env()`。

```python
from dataclasses import replace
from pathlib import Path

import pytest

from src.services.research_delivery.dependencies import build_manifest
from src.services.screening.strategy import list_strategies


STRATEGIES = Path(__file__).resolve().parents[1] / "src/services/screening/strategies"


def test_all_ten_strategy_manifests_reuse_existing_fields():
    infos = list_strategies(STRATEGIES)
    assert len(infos) == 10
    for info in infos:
        manifest = build_manifest(info)
        assert manifest.strategy_id == info.name
        assert manifest.strategy_version == info.version
        assert manifest.snapshot_fields == tuple(sorted(set(info.required_snapshot_fields)))
        assert manifest.daily_fields == tuple(sorted(set(info.required_daily_fields)))


def test_technical_and_snapshot_strategies_are_distinct():
    manifests = {info.name: build_manifest(info) for info in list_strategies(STRATEGIES)}
    assert "signal_score" in manifests["volume_breakout"].daily_fields
    assert manifests["dual_low"].daily_fields == ()
    assert {"pe_ratio", "pb_ratio"} <= set(manifests["dual_low"].snapshot_fields)


def test_missing_identity_or_dependency_is_rejected():
    info = list_strategies(STRATEGIES)[0]
    for changed in (
        replace(info, name=""),
        replace(info, version=""),
        replace(info, required_snapshot_fields=[]),
    ):
        with pytest.raises(ValueError):
            build_manifest(changed)
```

- [x] **Step 2：确认失败。**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_research_delivery_dependencies.py -q
```

预期：因新模块不存在而失败；若失败原因是环境/旧依赖，先记录并处理基线，不能当作正确的红灯测试。

- [x] **Step 3：添加完整契约文件。** `__init__.py` 仅包含 `"""Research subscription delivery primitives; no startup side effects."""`；`contracts.py` 内容如下：

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal


FieldStatus = Literal["valid", "not_applicable", "missing", "denied", "timeout", "invalid"]


@dataclass(frozen=True)
class DependencyManifest:
    strategy_id: str
    strategy_version: str
    snapshot_fields: tuple[str, ...]
    daily_fields: tuple[str, ...]


@dataclass(frozen=True, order=True)
class CellKey:
    stage: Literal["snapshot", "daily"]
    code: str
    field: str


@dataclass(frozen=True)
class FieldReceipt:
    key: CellKey
    status: FieldStatus
    as_of: date | None
    source: str = ""
    evidence_id: str = ""
    reason_code: str = ""


@dataclass(frozen=True)
class FrozenScope:
    universe: tuple[str, ...]
    snapshot_pass: tuple[str, ...]
    technical_planned: tuple[str, ...]
    technical_not_evaluated: tuple[str, ...]
    required_cells: tuple[CellKey, ...]


@dataclass(frozen=True)
class ReadinessResult:
    state: Literal["ready", "degraded", "blocked"]
    required_count: int
    valid_count: int
    not_applicable_count: int
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
```

`dependencies.py` 完整内容：

```python
from src.services.research_delivery.contracts import DependencyManifest
from src.services.screening.models import StrategyInfo


def build_manifest(info: StrategyInfo) -> DependencyManifest:
    if not info.name.strip() or not info.version.strip():
        raise ValueError("strategy identity and version are required")
    snapshot = tuple(sorted(set(info.required_snapshot_fields)))
    daily = tuple(sorted(set(info.required_daily_fields)))
    if not snapshot or any(not field.strip() for field in (*snapshot, *daily)):
        raise ValueError("non-empty snapshot dependencies are required")
    if info.requires_daily_features and not daily:
        raise ValueError("daily strategy has no declared daily dependencies")
    return DependencyManifest(info.name, info.version, snapshot, daily)
```

- [x] **Step 4：运行同一测试，预期 3 passed。** 再读生成依赖与实际 YAML，确认不是通过复制十策略字段凑测试。
- [x] **Step 5：审查本任务差异。** 无网络、无 `.env` 读取、无导入启动动作；记录测试结果，不提交 Git。

## Task 2：冻结范围，阻断伪完整与伪空结果

**Files:** Create `readiness.py`；Test `tests/test_research_delivery_readiness.py`。

**Interfaces:** Consumes Task 1 的全部契约；Produces `freeze_scope(manifest, universe, snapshot_pass, *, technical_limit=100) -> FrozenScope` 和 `assess_readiness(scope, receipts, *, trade_date, calendar_verified, universe_verified, optional_gaps=()) -> ReadinessResult`。

`snapshot_pass` 是已按冻结规则评分顺序排列的初筛池；本函数不重新排序或取随机样本。第一阶段只用快照字段验收通过后才能执行初筛；第二阶段带实际初筛池和日线凭据再验。真正有效空榜由后续适配器组合“就绪通过 + 实际规则结果零条”判定，本函数不声称已选股。

- [x] **Step 1：写完整边界测试。**

```python
from dataclasses import replace
from datetime import date, timedelta

import pytest

from src.services.research_delivery.contracts import DependencyManifest, FieldReceipt
from src.services.research_delivery.readiness import assess_readiness, freeze_scope


DAY = date(2026, 10, 13)
MANIFEST = DependencyManifest("fixture", "1", ("price",), ("ma20",))


def scope():
    return freeze_scope(MANIFEST, ("A", "B", "C"), ("B", "A"), technical_limit=1)


def receipts(frozen):
    return tuple(FieldReceipt(key, "valid", DAY, "fixture", f"proof-{i}")
                 for i, key in enumerate(frozen.required_cells))


def assess(frozen, items, **overrides):
    options = dict(trade_date=DAY, calendar_verified=True, universe_verified=True)
    options.update(overrides)
    return assess_readiness(frozen, items, **options)


def test_cap_is_not_a_failure_and_does_not_change_universe():
    frozen = scope()
    assert frozen.technical_planned == ("B",)
    assert frozen.technical_not_evaluated == ("A",)
    assert frozen.universe == ("A", "B", "C")
    assert len(frozen.required_cells) == 4
    result = assess(frozen, receipts(frozen))
    assert result.state == "ready"
    assert result.valid_count == result.required_count == 4


@pytest.mark.parametrize("status", ["missing", "denied", "timeout", "invalid"])
def test_missing_mandatory_data_blocks(status):
    frozen = scope()
    items = list(receipts(frozen))
    items[-1] = replace(items[-1], status=status)
    result = assess(frozen, items)
    assert result.state == "blocked"
    assert any(status in reason for reason in result.blockers)


def test_stale_unknown_date_and_absent_receipt_block():
    frozen = scope()
    good = receipts(frozen)
    for bad_date in (None, DAY - timedelta(days=1), DAY + timedelta(days=1)):
        items = (replace(good[0], as_of=bad_date), *good[1:])
        assert assess(frozen, items).state == "blocked"
    assert assess(frozen, good[:-1]).state == "blocked"


def test_no_source_or_evidence_is_not_valid():
    frozen = scope()
    good = receipts(frozen)
    for change in ({"source": ""}, {"evidence_id": ""}):
        bad = (replace(good[0], **change), *good[1:])
        assert assess(frozen, bad).state == "blocked"


def test_unknown_calendar_or_universe_blocks_even_with_receipts():
    frozen = scope()
    for change in ({"calendar_verified": False}, {"universe_verified": False}):
        assert assess(frozen, receipts(frozen), **change).state == "blocked"


def test_not_applicable_requires_reason_and_remains_visible():
    frozen = scope()
    good = receipts(frozen)
    item = replace(good[0], status="not_applicable")
    assert assess(frozen, (item, *good[1:])).state == "blocked"
    item = replace(item, reason_code="confirmed_suspended")
    result = assess(frozen, (item, *good[1:]))
    assert result.state == "ready"
    assert result.not_applicable_count == 1
    assert result.valid_count == 3


def test_optional_gaps_degrade_but_do_not_erase_core_blockers():
    frozen = scope()
    gaps = ("announcement_not_verified",)
    result = assess(frozen, receipts(frozen), optional_gaps=gaps)
    assert result.state == "degraded" and result.warnings == gaps
    assert assess(frozen, (), optional_gaps=gaps).state == "blocked"


def test_zero_snapshot_candidates_still_requires_full_snapshot():
    frozen = freeze_scope(MANIFEST, ("A", "B"), ())
    assert frozen.technical_planned == ()
    assert assess(frozen, receipts(frozen)).state == "ready"
    assert assess(frozen, ()).state == "blocked"


def test_duplicate_receipts_and_invalid_scope_are_rejected():
    frozen = scope()
    good = receipts(frozen)
    with pytest.raises(ValueError, match="duplicate"):
        assess(frozen, (*good, good[0]))
    for universe, candidates in (((), ()), (("A", "A"), ()), (("A",), ("B",))):
        with pytest.raises(ValueError):
            freeze_scope(MANIFEST, universe, candidates)
```

- [x] **Step 2：运行失败测试。**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_research_delivery_readiness.py -q
```

预期：模块不存在或接口未定义；不是网络连接错误。

- [x] **Step 3：实现完整纯函数。** `readiness.py`：

```python
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date

from src.services.research_delivery.contracts import (
    CellKey, DependencyManifest, FieldReceipt, FrozenScope, ReadinessResult,
)


def freeze_scope(
    manifest: DependencyManifest,
    universe: Sequence[str],
    snapshot_pass: Sequence[str],
    *,
    technical_limit: int = 100,
) -> FrozenScope:
    all_codes, passed = tuple(universe), tuple(snapshot_pass)
    if not all_codes or not manifest.snapshot_fields:
        raise ValueError("verified non-empty universe and snapshot requirements are required")
    if technical_limit < 1:
        raise ValueError("technical_limit must be positive")
    for values in (all_codes, passed):
        if len(set(values)) != len(values) or any(not code.strip() for code in values):
            raise ValueError("duplicate or empty code in scope")
    if not set(passed) <= set(all_codes):
        raise ValueError("snapshot candidates must belong to the frozen universe")
    technical = passed[:technical_limit] if manifest.daily_fields else ()
    not_evaluated = passed[technical_limit:] if manifest.daily_fields else ()
    keys = {
        CellKey("snapshot", code, field)
        for code in all_codes for field in manifest.snapshot_fields
    }
    keys.update(CellKey("daily", code, field) for code in technical for field in manifest.daily_fields)
    return FrozenScope(all_codes, passed, technical, not_evaluated, tuple(sorted(keys)))


def assess_readiness(
    scope: FrozenScope,
    receipts: Iterable[FieldReceipt],
    *,
    trade_date: date,
    calendar_verified: bool,
    universe_verified: bool,
    optional_gaps: Sequence[str] = (),
) -> ReadinessResult:
    blockers: list[str] = []
    warnings = tuple(dict.fromkeys(optional_gaps))
    if not calendar_verified:
        blockers.append("calendar_unknown")
    if not universe_verified or not scope.universe or not scope.required_cells:
        blockers.append("universe_or_requirements_unknown")
    indexed: dict[CellKey, FieldReceipt] = {}
    expected = set(scope.required_cells)
    for item in receipts:
        if item.key in indexed:
            raise ValueError("duplicate receipt")
        if item.key not in expected:
            raise ValueError("receipt outside declared scope")
        indexed[item.key] = item
    valid = not_applicable = 0
    for key in scope.required_cells:
        prefix = f"{key.stage}:{key.code}:{key.field}"
        item = indexed.get(key)
        if item is None:
            blockers.append(f"{prefix}:missing")
        elif item.status not in {"valid", "not_applicable"}:
            blockers.append(f"{prefix}:{item.status}")
        elif item.as_of != trade_date:
            blockers.append(f"{prefix}:wrong_or_unknown_business_date")
        elif not item.source.strip() or not item.evidence_id.strip():
            blockers.append(f"{prefix}:unproven")
        elif item.status == "not_applicable" and not item.reason_code.strip():
            blockers.append(f"{prefix}:unexplained_not_applicable")
        elif item.status == "not_applicable":
            not_applicable += 1
        else:
            valid += 1
    state = "blocked" if blockers else "degraded" if warnings else "ready"
    return ReadinessResult(state, len(scope.required_cells), valid, not_applicable,
                           tuple(blockers), warnings)
```

`not_applicable` 仅是有来源的业务排除凭据，不代表该股票可以通过策略规则。后续 adapter 校验其证据和允许的 reason_code，缺失 PE 不能自行贴“亏损”标签；UI 要分别显示 valid 和 not_applicable，不合成“100% 数据齐全”。

- [x] **Step 4：重跑 Task 1、2 测试，预期全部通过。** 检查只有预先冻结范围内的凭据被接受；不同策略可共享原数据，但必须按自己的范围投影凭据。
- [x] **Step 5：审查并记录。** 明确本批不验证真实 source/证据是否存在，不能据测试凭据发布市场结果；检查未更改旧管线。

## Task 3：发布时窗纯函数，不注册线上定时

**Files:** Create `timing.py`；Test `tests/test_research_delivery_timing.py`。

**Interfaces:** Produces `publication_window_state(now, opens_at, not_after, *, trading_day: bool | None) -> Literal["blocked", "closed", "waiting", "open", "expired"]`；W3 调度和 W5 每次投递前都复用。时点由订阅配置传入，不在函数里写死十个群的时刻。

- [x] **Step 1：写时钟测试。**

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.services.research_delivery.timing import publication_window_state


CN = ZoneInfo("Asia/Shanghai")
OPEN = datetime(2026, 10, 13, 17, 10, tzinfo=CN)
DEADLINE = datetime(2026, 10, 13, 19, 0, tzinfo=CN)


def test_window_boundaries_and_utc_are_consistent():
    for hour, minute, expected in ((17, 9, "waiting"), (17, 10, "open"),
                                  (18, 59, "open"), (19, 0, "expired")):
        local = datetime(2026, 10, 13, hour, minute, tzinfo=CN)
        assert publication_window_state(local, OPEN, DEADLINE, trading_day=True) == expected
        utc = local.astimezone(timezone.utc)
        assert publication_window_state(utc, OPEN, DEADLINE, trading_day=True) == expected


def test_unknown_calendar_and_closed_session_do_not_publish():
    assert publication_window_state(OPEN, OPEN, DEADLINE, trading_day=None) == "blocked"
    assert publication_window_state(OPEN, OPEN, DEADLINE, trading_day=False) == "closed"


def test_naive_clock_and_invalid_window_are_rejected():
    with pytest.raises(ValueError):
        publication_window_state(OPEN.replace(tzinfo=None), OPEN, DEADLINE, trading_day=True)
    with pytest.raises(ValueError):
        publication_window_state(OPEN, DEADLINE, OPEN, trading_day=True)
```

- [x] **Step 2：确认失败。**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_research_delivery_timing.py -q
```

预期缺少新模块；不依赖真实当前时间或网络日历。

- [x] **Step 3：实现 `timing.py` 完整内容。**

```python
from datetime import datetime
from typing import Literal


def publication_window_state(
    now: datetime,
    opens_at: datetime,
    not_after: datetime,
    *,
    trading_day: bool | None,
) -> Literal["blocked", "closed", "waiting", "open", "expired"]:
    if any(value.utcoffset() is None for value in (now, opens_at, not_after)):
        raise ValueError("timezone-aware timestamps are required")
    if opens_at >= not_after:
        raise ValueError("opens_at must precede not_after")
    if trading_day is None:
        return "blocked"
    if not trading_day:
        return "closed"
    if now >= not_after:
        return "expired"
    if now < opens_at:
        return "waiting"
    return "open"
```

公告/运维非交易日也可运行，由对应订阅政策明确传入允许发布，不套用 A 股交易日条件。该函数不计算周/月末交易日；后续调度 adapter 必须从真实日历获取，不允许日历不可用时默认为周五。

- [x] **Step 4：重跑时钟测试，预期 3 passed。** 检查所有时间由调用者注入，无真实 sleep 或系统时钟依赖。
- [x] **Step 5：审查差异并记录，确认没有任何调度器注册或发送代码。**

## Task 4：本批总验收与文档

**Files:** Review 本批新文件；Modify `docs/CHANGELOG.md`（只有实际实现通过后才写“新增数据验收核心”）、本计划 checkbox。

**Interfaces:** 本批纯函数供 W1 adapter、W3 调度、W5 worker 调用；未产生新的 API、数据库表或外部行为。

- [x] **Step 1：在隔离环境运行全部本批测试。**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_research_delivery_dependencies.py tests/test_research_delivery_readiness.py tests/test_research_delivery_timing.py -q
```

预期全部通过，无网络、模型或发送器调用。样例股票 A/B/C 和 fixture 源仅是确定性测试数据，不写入真实行情缓存。

- [x] **Step 2：语法与差异检查。**

```powershell
.\.venv\Scripts\python.exe -m py_compile src/services/research_delivery/contracts.py src/services/research_delivery/dependencies.py src/services/research_delivery/readiness.py src/services/research_delivery/timing.py
git diff --check
```

若全工作区出现其他旧文件的 whitespace 问题，单独报告，不顺手批量格式化。

- [x] **Step 3：由独立 reviewer 分别核对 spec 一致性与代码正确性。** 必查：覆盖分母不可缩小、缺日期阻断、not_applicable 不冒充有效数值、首 100 上限透明、日历未知阻断、不改变原系统行为。
- [x] **Step 4：仅在完成上述验证后增加扁平 changelog。** 记录“已实现核心，但未接真实采集和推送”，不称整个功能已可用。
- [x] **Step 5：交付第一批并展开 W1 真实适配任务的下一份 TDD 计划。** 下一批需额外增加评分因子依赖、股票状态凭据来源、逐行快照日期、冻结快照注入和历史/ETF 原始数据验证；不把本批合成凭据视为真实验收。

## 实施记录（2026-10-13）

Tasks1–4 已完成；额外补上 W1 的冻结数据只计算预览与本地命令。项目 `.venv` 合并回归687通过/21Windows跳过，语法检查及工作区空白检查通过，各实现批次独立审查无阻断问题。核心测试新增范围外凭据回归后共19项。后续 W1 生产采集器、ETF 与真实数据证据仍待实现；操作方式和边界见 [开发验收说明](../../feishu-research-delivery.md)。没有提交、部署或外发。

## 自审与执行边界

本计划覆盖总路线 W1 的纯核心子集与 W3 的时窗核心，其余 spec 要求已逐项归入总路线 W0—W6，不声称全部具有逐行实现方案。执行前须读取当前代码与 `AGENTS.md`，保持本批无外部副作用。

建议子代理按 Task 1 → Task 2/3 → Task 4 分工并逐批审查；也可由主代理按同一清单顺序执行。无论哪种方式，在线数据验证、真实飞书测试和正式启用均不属于本批自动操作。
