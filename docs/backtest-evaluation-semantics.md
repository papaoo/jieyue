# 回测评估语义（研判建议评估引擎）

本文档说明 `src/core/backtest_engine.py` 的评估口径。它评估的是**历史研判建议的事后表现**，不是组合策略回测。

## 定位边界

| 能力 | 本引擎 | 组合回测（`src/core/etf_rotation.py`） |
| --- | --- | --- |
| 评估对象 | 单条 LLM 研判建议（文本或结构化 action） | 规则化轮动策略 |
| 仓位 | 仅 long / cash 两态 | 多槽位等权 + 现金/避险资产 |
| 资金 | 无（单笔收益率） | 有（净值、夏普、最大回撤） |
| 执行 | 信号次日可成交价 + 双边成本 | 次日执行 + 成本 + 停牌桥接 |
| 结论用途 | 「研判准不准」「策略意见权重校准」 | 「策略能不能上实盘」 |

需要净值曲线、夏普、Calmar、多标的资金分配时，请用 ETF 轮动回测，不要用本引擎的数字当策略收益。

## 两个基准必须分清

| 字段 | 基准 | 用途 |
| --- | --- | --- |
| `stock_return_pct` | 信号当日收盘价（决策点） | 方向命中 / win-loss 判定（预测是从决策点做出的） |
| `entry_price` + `trade_return_pct` | 信号**次日**可成交价 | 可成交性度量（前视偏差只存在于"按当日收盘价成交"这个动作） |
| `simulated_return_pct` | 次日入场 + 双边成本 | 建议的事后损益 |

long 建议的 `simulated_return_pct` = `_net_return_pct(entry_price, exit_price, config)`；
cash 建议（卖出/观望）返回 `simulated_return_basis="cash_vs_hold"`，即相对同日买入持有的**超额收益**（正=避跌有效，负=踏空代价），不发生交易故不计成本。

## v1 → v2 的语义变更

`BACKTEST_ENGINE_VERSION`（默认 `v2`）用于区分不同语义下的结果，**v1 与 v2 结果不可直接比较**。

| 变更 | v1 | v2 |
| --- | --- | --- |
| 模拟成交价 | 信号当日收盘价（前视偏差，系统性高估收益） | 信号次日开盘价；缺 `open` 时退化为次日收盘价；`entry_price_basis` 记录实际口径 |
| 交易成本 | 无（裸价差） | 买入 `commission+slippage`、卖出 `commission+stamp_duty+slippage`（默认 3/5/5 bps） |
| 止损/止盈成交 | 一律按触发价（跳空高估止损效率） | 跳空保护：止损按 `min(open, stop)`，止盈按 `max(open, target)` |
| 卖出/观望建议 | 模拟收益恒为 0（等于放弃评估） | 相对持有基准的超额收益 |
| 摘要指标 | 胜率、方向命中率、平均收益 | 新增盈亏比、最大回撤（按分析日排序的逐笔序列）、平均盈亏、单笔最大回撤/盈利、基准超额、`ambiguous_count` |

只看胜率会被"高胜率小亏低赚"误导，请同时看 `profit_factor` 与 `excess_vs_benchmark_pct`。

`max_drawdown_pct` 是**逐笔建议序列**的近似回撤，用于发现极端离群值，不是策略净值回撤；缺少 `analysis_date` 的行不参与该序列（`drawdown_series_points` 会小于样本数）。

## 参数

| `EvaluationConfig` | 默认 | 说明 |
| --- | --- | --- |
| `eval_window_days` | — | 前向评估窗口（交易日数） |
| `neutral_band_pct` | `2.0` | ±2% 内视为震荡，不计入 win/loss |
| `execution_lag_days` | `1` | `0` 表示按信号当日收盘价成交（仅研究用，会引入前视偏差） |
| `commission_bps` / `stamp_duty_bps` / `slippage_bps` | `3.0` / `5.0` / `5.0` | 单边成本（bps = 万分之一） |

## 复权基准前置条件

评估窗口依赖本地 `stock_daily`。若同一标的窗口内混用了前复权与不复权数据，除权日附近的收益会完全失真且无法从价格分辨。落库时记录 `price_adjustment`，回测窗口检测到混用时会输出告警，详见 [数据源稳定性](data-source-stability.md#复权口径一致性)。

## 相关文档

- [决策信号](decision-signals.md)
- [ETF 轮动回测](etf-rotation.md)
- [数据源稳定性](data-source-stability.md)