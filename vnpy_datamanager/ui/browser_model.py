"""数据浏览器的纯逻辑：过滤、排序、截断（不依赖 Qt，便于单测）。

界面只负责渲染这里返回的 ``rows``。之所以单独拆出来：5200+ 个标的的
``BarOverview`` 过滤/排序/搜索是纯数据操作，混在 ``QTableWidget`` 里既难测也难
复用；而「只渲染前 N 行」与「搜索跨全库」的语义都落在这一层。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from vnpy.trader.constant import Exchange, Interval


# 浏览器的周期单选：只有 1 分钟与日线（与定时下载周期一致，小时线不再使用）。
BROWSER_INTERVALS: tuple[Interval, ...] = (Interval.DAILY, Interval.MINUTE)

# 默认每页行数；「加载更多」一次再加这么多。
PAGE_SIZE: int = 100

# 右侧预览最多渲染的根数（1m 全历史可达 5 万根，全塞进表格会假死）。
PREVIEW_ROWS: int = 500

SORT_RECENT: str = "recent"
SORT_SYMBOL: str = "symbol"


@dataclass
class BrowserRow:
    """一行列表数据（一个标的 + 一个周期）。"""

    symbol: str
    exchange: Exchange
    interval: Interval
    name: str
    count: int
    start: datetime
    end: datetime

    @property
    def vt_symbol(self) -> str:
        """``600000.SSE`` 形式。"""
        return f"{self.symbol}.{self.exchange.value}"


def normalize_keyword(text: str) -> str:
    """搜索词归一：去空白、转大写（交易所后缀是大写）。"""
    return str(text or "").strip().upper()


def match_score(row: BrowserRow, keyword: str) -> int:
    """匹配优先级，越小越靠前；``0`` 表示不匹配。

    - 1: ``code.exchange`` 完全匹配（输入 ``600000.SSE``）
    - 2: 代码完全匹配（输入 ``600000``）
    - 3: 代码前缀匹配（输入 ``600``）
    - 4: 代码包含（输入 ``0600``）
    - 5: 名称包含（输入 ``浦发``）
    """
    if not keyword:
        return 1
    vt: str = row.vt_symbol.upper()
    code: str = row.symbol.upper()
    if keyword == vt:
        return 1
    if keyword == code:
        return 2
    if code.startswith(keyword):
        return 3
    if keyword in code:
        return 4
    if keyword and keyword in (row.name or "").upper():
        return 5
    return 0


def filter_rows(
    rows: list[BrowserRow],
    interval: Interval,
    exchange: Exchange | None = None,
    keyword: str = "",
    sort: str = SORT_RECENT,
) -> list[BrowserRow]:
    """按周期（单选）/交易所/关键字过滤并排序，返回**完整匹配集**。

    截断交给调用方（界面按 ``display_limit`` 只渲染前 N 行）；这样「搜索跨全库」
    与「只显示 N 行」互不干扰。
    """
    key: str = normalize_keyword(keyword)

    scored: list[tuple[int, BrowserRow]] = []
    for row in rows:
        if row.interval != interval:
            continue
        if exchange is not None and row.exchange != exchange:
            continue
        score: int = match_score(row, key)
        if not score:
            continue
        scored.append((score, row))

    if sort == SORT_SYMBOL:
        scored.sort(key=lambda item: (item[0], item[1].symbol, item[1].exchange.value))
    else:   # 最近更新优先
        scored.sort(key=lambda item: (item[0], -item[1].end.timestamp(), item[1].symbol))

    return [row for _, row in scored]
