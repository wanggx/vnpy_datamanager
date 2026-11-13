from datetime import datetime

from vnpy.trader.constant import Exchange, Interval

from vnpy_datamanager.ui.browser_model import (
    BrowserRow,
    SORT_RECENT,
    SORT_SYMBOL,
    filter_rows,
)


def make_row(
    symbol: str,
    exchange: Exchange = Exchange.SSE,
    *,
    name: str = "",
    end: datetime | None = None,
    is_index: bool = False,
) -> BrowserRow:
    return BrowserRow(
        symbol=symbol,
        exchange=exchange,
        interval=Interval.DAILY,
        name=name,
        count=10,
        start=datetime(2026, 11, 1),
        end=end or datetime(2026, 11, 12),
        is_index=is_index,
    )


def test_index_rows_come_first_in_both_sorts() -> None:
    """大盘指数在列表里置顶（两种排序都如此），方便确认指数是否已下载。"""
    rows: list[BrowserRow] = [
        make_row("600000", name="浦发银行", end=datetime(2026, 11, 12, 15, 0)),
        make_row("000001", is_index=True, name="上证综指"),
        make_row("399001", Exchange.SZSE, is_index=True, name="深证成指"),
        make_row("000002", Exchange.SZSE, name="万科A"),
    ]

    for sort in (SORT_RECENT, SORT_SYMBOL):
        ordered = filter_rows(rows, Interval.DAILY, sort=sort)
        assert [row.symbol for row in ordered[:2]] == ["000001", "399001"], sort
        assert all(row.is_index for row in ordered[:2]), sort
        # 非指数行的相对顺序按各自排序规则，这里只校验它们都排在指数之后
        assert {row.symbol for row in ordered[2:]} == {"600000", "000002"}, sort


def test_index_rows_survive_filters_and_search() -> None:
    """按交易所过滤/搜索时不动置顶规则，且指数名称可直接被搜到。"""
    rows: list[BrowserRow] = [
        make_row("600000", name="浦发银行"),
        make_row("000001", is_index=True, name="上证综指"),
    ]

    by_exchange = filter_rows(rows, Interval.DAILY, exchange=Exchange.SSE)
    assert by_exchange[0].symbol == "000001"

    found = filter_rows(rows, Interval.DAILY, keyword="上证")
    assert [row.symbol for row in found] == ["000001"]
