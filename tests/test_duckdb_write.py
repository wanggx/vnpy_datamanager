"""阶段一 universe 批量下载写入 DuckDB 测试。

用真实 DuckdbDatabase + mock datafeed（构造 BarData）验证：
- 批量下载把多个标的的 Bar 写入 vnpy.duckdb
- 重复保存不产生重复记录
- 多周期隔离
- end 参数生效
- 批量任务汇总成功/失败/跳过
- 失败标的可只重试
"""
import gc
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("duckdb")

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import DB_TZ  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402
from vnpy_duckdb import DuckdbDatabase  # noqa: E402

from vnpy_datamanager.engine import (  # noqa: E402
    ManagerEngine, DataUniverse, DownloadMode, DownloadTaskStatus,
)


@pytest.fixture()
def database(tmp_path: Path) -> DuckdbDatabase:
    db: DuckdbDatabase = DuckdbDatabase(tmp_path / "duckdb_write.duckdb")
    yield db
    db.close()
    del db
    gc.collect()


def make_bar(symbol: str, exchange: Exchange, dt: datetime,
             interval: Interval = Interval.MINUTE, close: float = 10.0) -> BarData:
    return BarData(
        symbol=symbol, exchange=exchange, datetime=dt, interval=interval,
        volume=100, turnover=1000, open_interest=0,
        open_price=close - 1, high_price=close + 1, low_price=close - 2,
        close_price=close, gateway_name="TEST",
    )


def make_engine(
    monkeypatch: pytest.MonkeyPatch,
    database: DuckdbDatabase,
    datafeed: MagicMock,
) -> ManagerEngine:
    """构造一个用真实 DuckDB + mock datafeed 的 ManagerEngine。"""
    monkeypatch.setattr("vnpy_datamanager.engine.get_database", lambda: database)
    monkeypatch.setattr("vnpy_datamanager.engine.get_datafeed", lambda: datafeed)
    main_engine = MagicMock()
    main_engine.get_contract.return_value = None   # 让 download 走 datafeed 分支
    engine: ManagerEngine = ManagerEngine(main_engine, MagicMock())
    return engine


def _mock_xtdata(monkeypatch, symbols_with_exchange):
    """mock _get_xtdata，让 get_universe_symbols 返回固定标的列表。

    xtquant 板块返回的代码用 .SH/.SZ 后缀（非 vnpy 的 SSE/SZSE），
    get_universe_symbols 会按 VALID_MARKETS (.SH/.SZ) 过滤后再 _parse_vt_symbol。
    """
    # vnpy Exchange -> xtquant 后缀
    ex_suffix = {Exchange.SSE: ".SH", Exchange.SZSE: ".SZ", Exchange.BSE: ".BJ"}

    def fake_get_xtdata():
        xt = MagicMock()
        codes = [f"{s}{ex_suffix[ex]}" for s, ex in symbols_with_exchange]
        xt.get_stock_list_in_sector.return_value = codes
        return xt
    monkeypatch.setattr(ManagerEngine, "_get_xtdata", staticmethod(fake_get_xtdata))


def test_save_and_load_1000_minute_bars(database: DuckdbDatabase) -> None:
    """1000 根 1m Bar 保存和读回。"""
    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)
    bars = [make_bar("600000", Exchange.SSE, base + timedelta(minutes=i))
            for i in range(1000)]
    assert database.save_bar_data(bars)

    loaded = database.load_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        base, base + timedelta(minutes=1000),
    )
    assert len(loaded) == 1000
    assert loaded[0].datetime == base
    assert loaded[-1].datetime == base + timedelta(minutes=999)


def test_duplicate_save_no_extra_records(database: DuckdbDatabase) -> None:
    """重复保存同主键不产生重复记录。"""
    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)
    bars = [make_bar("600000", Exchange.SSE, base + timedelta(minutes=i))
            for i in range(10)]
    database.save_bar_data(bars)
    # 再保存前 5 根（不同 close），应覆盖而非新增
    dup = [make_bar("600000", Exchange.SSE, base + timedelta(minutes=i), close=99)
           for i in range(5)]
    database.save_bar_data(dup)

    loaded = database.load_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        base, base + timedelta(minutes=10),
    )
    assert len(loaded) == 10  # 无重复
    # 前 5 根被覆盖
    assert loaded[0].close_price == 99.0
    assert loaded[5].close_price == 10.0


def test_multi_interval_isolation(database: DuckdbDatabase) -> None:
    """多周期数据隔离。"""
    base = datetime(2024, 1, 2, tzinfo=DB_TZ)
    min_bars = [make_bar("600000", Exchange.SSE, base + timedelta(minutes=i),
                         interval=Interval.MINUTE) for i in range(5)]
    day_bars = [make_bar("600000", Exchange.SSE, base + timedelta(days=i),
                         interval=Interval.DAILY) for i in range(5)]
    database.save_bar_data(min_bars)
    database.save_bar_data(day_bars)

    min_loaded = database.load_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE, base, base + timedelta(days=1))
    day_loaded = database.load_bar_data(
        "600000", Exchange.SSE, Interval.DAILY, base, base + timedelta(days=10))
    assert len(min_loaded) == 5
    assert len(day_loaded) == 5
    assert all(b.interval == Interval.MINUTE for b in min_loaded)
    assert all(b.interval == Interval.DAILY for b in day_loaded)


def test_universe_download_writes_multiple_symbols(
    monkeypatch, database: DuckdbDatabase,
) -> None:
    """批量下载多个标的写入 DuckDB。"""
    symbols = [("600000", Exchange.SSE), ("000001", Exchange.SZSE)]
    _mock_xtdata(monkeypatch, symbols)

    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)

    # mock datafeed：每个标的返回 5 根 1m bar
    def query_bar_history(req, output=None):
        return [make_bar(req.symbol, req.exchange, req.start + timedelta(minutes=i))
                for i in range(5)]

    datafeed = MagicMock()
    datafeed.query_bar_history.side_effect = query_bar_history
    engine = make_engine(monkeypatch, database, datafeed)

    universe = DataUniverse(name="沪深全市场", source="xtquant")
    result = engine.download_universe_bar_data(
        universe, Interval.MINUTE, base, base + timedelta(minutes=10),
        output=lambda msg: None, mode=DownloadMode.REPLACE,
    )

    assert result.status == DownloadTaskStatus.COMPLETED
    assert result.total == 2
    assert result.success == 2
    assert result.failed == 0
    # 验证两个标的都写入 DuckDB
    for sym, ex in symbols:
        loaded = database.load_bar_data(sym, ex, Interval.MINUTE, base, base + timedelta(minutes=10))
        assert len(loaded) == 5


def test_universe_download_skips_already_covered(
    monkeypatch, database: DuckdbDatabase,
) -> None:
    """APPEND 模式：已覆盖到 end 的标的跳过。"""
    symbols = [("600000", Exchange.SSE), ("000001", Exchange.SZSE)]
    _mock_xtdata(monkeypatch, symbols)
    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)
    end = base + timedelta(minutes=10)

    # 预先写入 600000 覆盖到 end
    pre_bars = [make_bar("600000", Exchange.SSE, end - timedelta(minutes=i))
                for i in range(5)]
    database.save_bar_data(pre_bars)

    def query_bar_history(req, output=None):
        return [make_bar(req.symbol, req.exchange, req.start + timedelta(minutes=i))
                for i in range(5)]

    datafeed = MagicMock()
    datafeed.query_bar_history.side_effect = query_bar_history
    engine = make_engine(monkeypatch, database, datafeed)

    universe = DataUniverse(name="沪深全市场", source="xtquant")
    result = engine.download_universe_bar_data(
        universe, Interval.MINUTE, base, end,
        output=lambda msg: None, mode=DownloadMode.APPEND,
    )
    # 600000 已覆盖跳过，000001 下载
    assert result.success == 1
    assert result.skipped == 1
    # datafeed 只被调用一次（000001）
    assert datafeed.query_bar_history.call_count == 1


def test_universe_download_failed_retry(
    monkeypatch, database: DuckdbDatabase,
) -> None:
    """失败标的可只重试失败标的。"""
    symbols = [("600000", Exchange.SSE), ("000001", Exchange.SZSE)]
    _mock_xtdata(monkeypatch, symbols)
    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)

    call_count = {"600000": 0, "000001": 0}

    def query_bar_history(req, output=None):
        call_count[req.symbol] += 1
        if req.symbol == "600000":
            raise RuntimeError("模拟失败")
        return [make_bar(req.symbol, req.exchange, req.start + timedelta(minutes=i))
                for i in range(5)]

    datafeed = MagicMock()
    datafeed.query_bar_history.side_effect = query_bar_history
    engine = make_engine(monkeypatch, database, datafeed)

    universe = DataUniverse(name="沪深全市场", source="xtquant")
    # 第一次：600000 失败，000001 成功
    result = engine.download_universe_bar_data(
        universe, Interval.MINUTE, base, base + timedelta(minutes=10),
        output=lambda msg: None, mode=DownloadMode.REPLACE,
    )
    assert result.success == 1
    assert result.failed == 1
    assert "600000.SSE" in result.failed_symbols

    # 只重试 600000
    retry = engine.download_universe_bar_data(
        universe, Interval.MINUTE, base, base + timedelta(minutes=10),
        output=lambda msg: None, mode=DownloadMode.APPEND,
        failed_only=["600000.SSE"],
    )
    # 600000 仍失败（模拟持续失败）
    assert retry.total == 1
    assert retry.failed == 1


def test_end_parameter_takes_effect(
    monkeypatch, database: DuckdbDatabase,
) -> None:
    """download_bar_data 的 end 参数生效（限定 HistoryRequest.end）。"""
    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)
    end = base + timedelta(minutes=5)

    captured_reqs = []

    def query_bar_history(req, output=None):
        captured_reqs.append(req)
        return [make_bar(req.symbol, req.exchange, req.start + timedelta(minutes=i))
                for i in range(3)]

    datafeed = MagicMock()
    datafeed.query_bar_history.side_effect = query_bar_history
    engine = make_engine(monkeypatch, database, datafeed)

    engine.download_bar_data("600000", Exchange.SSE, Interval.MINUTE, base, lambda msg: None, end=end)
    assert captured_reqs[0].end == end


def test_universe_no_symbols_when_xtdata_unavailable(
    monkeypatch, database: DuckdbDatabase,
) -> None:
    """xtdata 不可用时 universe 返回空，批量下载 total=0。"""
    monkeypatch.setattr(ManagerEngine, "_get_xtdata", staticmethod(lambda: None))
    datafeed = MagicMock()
    engine = make_engine(monkeypatch, database, datafeed)

    universe = DataUniverse(name="沪深全市场", source="xtquant")
    result = engine.download_universe_bar_data(
        universe, Interval.MINUTE, datetime(2024, 1, 2, tzinfo=DB_TZ),
        datetime(2024, 1, 3, tzinfo=DB_TZ), output=lambda msg: None,
    )
    assert result.total == 0
    assert result.success == 0


def test_universe_download_symbol_timeout_marks_failed(
    monkeypatch, database: DuckdbDatabase,
) -> None:
    """单标的超时记失败并继续：不能让一只标的把整批卡在 99%。"""
    import time

    symbols = [
        ("600000", Exchange.SSE), ("000001", Exchange.SZSE), ("600519", Exchange.SSE),
    ]
    _mock_xtdata(monkeypatch, symbols)
    base = datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ)

    def query_bar_history(req, output=None):
        if req.symbol == "000001":
            time.sleep(2)          # 数据源迟迟不返回
            return []
        return [
            make_bar(req.symbol, req.exchange, req.start + timedelta(minutes=i))
            for i in range(3)
        ]

    datafeed = MagicMock()
    datafeed.query_bar_history.side_effect = query_bar_history
    engine = make_engine(monkeypatch, database, datafeed)
    engine.symbol_timeout = 0.2    # 200ms 就判超时

    logs: list[str] = []
    result = engine.download_universe_bar_data(
        DataUniverse(name="沪深全市场", source="xtquant"), Interval.MINUTE,
        base, base + timedelta(minutes=5), output=logs.append,
        mode=DownloadMode.REPLACE,
    )

    assert result.status == DownloadTaskStatus.COMPLETED
    assert result.total == 3
    assert result.success == 2
    assert result.failed == 1
    assert result.failed_symbols == ["000001.SZSE"]
    assert any("超时" in msg for msg in logs)
