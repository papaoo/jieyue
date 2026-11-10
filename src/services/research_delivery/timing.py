from datetime import datetime, time
from typing import Literal


# 发布时窗（Asia/Shanghai，闭开区间 [开始, 截止)）。
#
# 语义：窗口起点是「共享 A 股批次最早开始准备」的时间，它同时是**启用校验的边界**——
# 启用订阅时会拒绝窗口外的 planned_time，返回 `subscription_not_configured`。
# 因此调整各群计划时间必须同步这里，否则页面上改完时间会启不动。
#
# 起点取 15:30（A 股 15:00 收盘后 30 分钟）：这是**策略起跑时间**——
# 工作进程在窗口打开时就开始「跑策略 + 整合数据」，而不是等到某个计划时间。
# 留 30 分钟是给日线收盘价/成交量落定（数据源通常 15:05–15:20 稳定）留余量；
# 数据或模型未就绪时由 readiness 门禁顺延，不会用不完整数据提前发布。
# 截止保留 22:30：公告与风险类内容依赖盘后陆续披露的公告，需要足够尾部空间。
#
# ⚠️ 这两个值是唯一真源：启用校验、工作进程 tick、发布门禁、采集窗口、批次过期时间
# 全部引用它们。历史上这些位置各自硬编码 17:05/22:30，改一处会导致
# 「页面能启用但 worker 不执行」这类静默失效，请勿再写字面量。
PUBLICATION_WINDOW_START_TIME = time(15, 30)
PUBLICATION_WINDOW_END_TIME = time(22, 30)
PUBLICATION_WINDOW_START = PUBLICATION_WINDOW_START_TIME.strftime("%H:%M")
PUBLICATION_WINDOW_END = PUBLICATION_WINDOW_END_TIME.strftime("%H:%M")


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
