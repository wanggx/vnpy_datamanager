"""数据质量检查服务测试。"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import DB_TZ  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402
from vnpy_duckdb import DuckdbDatabase  # noqa: E402

from vnpy_datamanager.quality import (  # noqa: E402
    DataQualityService,
    QualityStatus,
    expected_minutes_for_day,
    trading_days_in_range,
)


@pytest.fixture()
def database(tmp_path: Path) -> DuckdbDatabase:
    import gc
    db: DuckdbDatabase = DuckdbDatabase(tmp_path / "quality.duckdb")
    yield db
    db.close()
    del db
    gc.collect()   # Windows 上 DuckDB close 后文件句柄延迟释放，强制 GC 让 tmp_path 可清理


def make_bar(symbol: str, exchange: Exchange, dt: datetime,
             interval: Interval = Interval.MINUTE,
             close: float = 10.0, volume: float = 100.0) -> BarData:
    return BarData(
        symbol=symbol, exchange=exchange, datetime=dt, interval=interval,
        volume=volume, turnover=volume * close, open_interest=0,
        open_price=close - 1, high_price=close + 1, low_price=close - 2,
        close_price=close, gateway_name="TEST",
    )


def test_expected_minutes_for_day_count() -> None:
    """一个交易日期望 240 根 1m（上午 120 + 下午 120）。"""
    from datetime import date
    times = expected_minutes_for_day(date(2024, 1, 2))
    assert len(times) == 240
    assert times[0].hour == 9 and times[0].minute == 31
    assert times[-1].hour == 15 and times[-1].minute == 0


def test_trading_days_excludes_weekend() -> None:
    """交易日排除周末。"""
    from datetime import date
    # 2024-01-06 是周六，2024-01-07 周日，2024-01-08 周一
    days = trading_days_in_range(date(2024, 1, 6), date(2024, 1, 8))
    assert days == [date(2024, 1, 8)]


def test_check_pass_complete_minute_data(database: DuckdbDatabase) -> None:
    """完整 1m 数据应判 PASS。"""
    # 造一天完整的 240 根 1m bar
    from datetime import date
    d = date(2024, 1, 2)  # 周二
    bars = []
    for t in expected_minutes_for_day(d):
        dt = datetime.combine(d, t, tzinfo=DB_TZ)
        bars.append(make_bar("600000", Exchange.SSE, dt))
    database.save_bar_data(bars)

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
    )
    assert report.actual_count == 240
    assert report.expected_count == 240
    assert report.duplicate_count == 0
    assert report.status == QualityStatus.PASS


def test_check_missing_bars_without_probe_is_fail(database: DuckdbDatabase) -> None:
    """缺 bar 又没回源复核时，保守判 FAIL（不能默默放过）。"""
    from datetime import date
    d = date(2024, 1, 2)
    bars = []
    times = expected_minutes_for_day(d)
    # 跳过 20 根（240 - 20 = 220）
    for i, t in enumerate(times):
        if i % 12 == 0:  # 每 12 根跳 1 根，约跳 20 根
            continue
        dt = datetime.combine(d, t, tzinfo=DB_TZ)
        bars.append(make_bar("600000", Exchange.SSE, dt))
    database.save_bar_data(bars)

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
    )
    assert report.actual_count < 240
    assert report.expected_count == 240
    assert report.missing_count > 0
    assert report.remote_checked is False
    assert report.status == QualityStatus.FAIL


def test_check_missing_bars_confirmed_by_source_is_pass(database: DuckdbDatabase) -> None:
    """回源复核发现源头同样没有这些 bar（停牌/无成交）→ PASS。"""
    from datetime import date
    d = date(2024, 1, 2)
    bars = []
    times = expected_minutes_for_day(d)
    for i, t in enumerate(times):
        if i % 12 == 0:
            continue
        dt = datetime.combine(d, t, tzinfo=DB_TZ)
        bars.append(make_bar("600000", Exchange.SSE, dt))
    database.save_bar_data(bars)

    probed: list[tuple[datetime, datetime]] = []

    def probe(symbol, exchange, interval, start, end):
        probed.append((start, end))
        return set()   # 源头一根都没有

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
        probe=probe,
    )
    # 回源复核取的是整段检查周期，不是零散的缺失点
    assert probed == [
        (datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
         datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ))
    ]
    assert report.remote_checked is True
    assert report.remote_gap_count == 0
    assert report.source_missing_count == report.missing_count > 0
    assert report.status == QualityStatus.PASS


def test_check_missing_bars_present_in_source_is_fail(database: DuckdbDatabase) -> None:
    """回源复核发现源头有、本地没有（真洞）→ FAIL。"""
    from datetime import date
    d = date(2024, 1, 2)
    times = expected_minutes_for_day(d)
    bars = [
        make_bar("600000", Exchange.SSE, datetime.combine(d, t, tzinfo=DB_TZ))
        for i, t in enumerate(times) if i % 12 != 0
    ]
    database.save_bar_data(bars)

    # 源头这一整段都有数据
    full_day: set[datetime] = {
        datetime.combine(d, t, tzinfo=DB_TZ) for t in times
    }

    def probe(symbol, exchange, interval, start, end):
        return full_day

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
        probe=probe,
    )
    assert report.remote_checked is True
    assert report.remote_gap_count == report.missing_count > 0
    assert report.source_missing_count == 0
    assert report.status == QualityStatus.FAIL


def test_check_probe_unavailable_is_fail(database: DuckdbDatabase) -> None:
    """回源复核失败（数据源未就绪）时按 FAIL 处理，并写明原因。"""
    from datetime import date
    d = date(2024, 1, 2)
    times = expected_minutes_for_day(d)
    database.save_bar_data([
        make_bar("600000", Exchange.SSE, datetime.combine(d, t, tzinfo=DB_TZ))
        for i, t in enumerate(times) if i % 12 != 0
    ])

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
        probe=lambda *a: None,
    )
    assert report.remote_checked is False
    assert report.status == QualityStatus.FAIL
    assert "复核失败" in report.error


def test_check_fail_no_data(database: DuckdbDatabase) -> None:
    """无数据应判 FAIL。"""
    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "000001", Exchange.SZSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
    )
    assert report.actual_count == 0
    assert report.status == QualityStatus.FAIL


def test_check_fail_ohlc_error(database: DuckdbDatabase) -> None:
    """OHLC 关系错误（high < low）应判 FAIL。"""
    from datetime import date
    d = date(2024, 1, 2)
    bars = []
    for t in expected_minutes_for_day(d):
        dt = datetime.combine(d, t, tzinfo=DB_TZ)
        bars.append(make_bar("600000", Exchange.SSE, dt))
    # 制造一个 high < low 的坏 bar
    bars[0] = BarData(
        symbol="600000", exchange=Exchange.SSE, datetime=bars[0].datetime,
        interval=Interval.MINUTE, volume=100, turnover=1000, open_interest=0,
        open_price=5, high_price=3, low_price=8, close_price=5, gateway_name="T",
    )
    database.save_bar_data(bars)

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
    )
    assert report.ohlc_error_count > 0
    assert report.status == QualityStatus.FAIL


def test_check_duplicate_count(database: DuckdbDatabase) -> None:
    """重复保存同主键应被插件去重，实际无重复行。"""
    from datetime import date
    d = date(2024, 1, 2)
    bars = [make_bar("600000", Exchange.SSE,
                     datetime.combine(d, t, tzinfo=DB_TZ))
            for t in expected_minutes_for_day(d)]
    database.save_bar_data(bars)
    # 重复写第一根（不同 close_price），插件 INSERT OR REPLACE 应覆盖而非新增
    first_dt = datetime.combine(d, expected_minutes_for_day(d)[0], tzinfo=DB_TZ)
    database.save_bar_data([make_bar("600000", Exchange.SSE, first_dt, close=99)])

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
    )
    # 插件去重保证实际无重复时间戳
    assert report.duplicate_count == 0
    assert report.actual_count == 240
    assert report.status == QualityStatus.PASS


def test_save_report_to_duckdb(database: DuckdbDatabase) -> None:
    """报告可写入 quality_data_quality_report 表。"""
    from datetime import date
    d = date(2024, 1, 2)
    bars = [make_bar("600000", Exchange.SSE,
                     datetime.combine(d, t, tzinfo=DB_TZ))
            for t in expected_minutes_for_day(d)]
    database.save_bar_data(bars)

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600000", Exchange.SSE, Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
    )
    svc.save_report(report)

    # 验证写入
    rows = database.conn.execute(
        "SELECT symbol, status FROM quality_data_quality_report"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "600000"
    assert rows[0][1] == "PASS"


def test_check_universe_aggregates(database: DuckdbDatabase) -> None:
    """universe 批量检查应聚合总体与逐标的。"""
    from datetime import date
    d = date(2024, 1, 2)
    # 标的 A 完整，标的 B 无数据
    bars_a = [make_bar("600000", Exchange.SSE,
                       datetime.combine(d, t, tzinfo=DB_TZ))
              for t in expected_minutes_for_day(d)]
    database.save_bar_data(bars_a)

    svc = DataQualityService(database)
    report = svc.check_universe_data(
        [("600000", Exchange.SSE), ("000001", Exchange.SZSE)],
        Interval.MINUTE,
        datetime(2024, 1, 2, 9, 31, tzinfo=DB_TZ),
        datetime(2024, 1, 2, 15, 0, tzinfo=DB_TZ),
    )
    assert report.total_symbols == 2
    assert report.pass_count == 1
    assert report.fail_count == 1
    assert "000001.SZSE" in report.failed_symbols


def test_daily_missing_one_day_confirmed_by_source_is_pass(database: DuckdbDatabase) -> None:
    """日线缺 1 天，回源确认源头也没有（停牌）→ PASS，不是 WARN/FAIL。"""
    from datetime import date, time
    days = trading_days_in_range(date(2026, 10, 13), date(2026, 11, 12))
    assert len(days) == 23
    skip = days[3]
    database.save_bar_data([
        make_bar("000010", Exchange.SZSE,
                 datetime.combine(d, time(0, 0), tzinfo=DB_TZ),
                 interval=Interval.DAILY)
        for d in days if d != skip
    ])

    svc = DataQualityService(database)
    start = datetime(2026, 10, 13, tzinfo=DB_TZ)
    end = datetime(2026, 11, 12, 23, 59, 59, tzinfo=DB_TZ)
    report = svc.check_bar_data(
        "000010", Exchange.SZSE, Interval.DAILY, start, end, probe=lambda *a: set()
    )
    assert (report.expected_count, report.actual_count) == (23, 22)
    assert [(mr.start.date(), mr.count) for mr in report.missing_ranges] == [(skip, 1)]
    assert report.missing_count == 1
    assert report.source_missing_count == 1
    assert report.remote_gap_count == 0
    assert report.status == QualityStatus.PASS


def test_daily_missing_one_day_present_in_source_is_fail(database: DuckdbDatabase) -> None:
    """日线缺 1 天，但源头有这一天的数据 → FAIL。"""
    from datetime import date, time
    days = trading_days_in_range(date(2026, 10, 13), date(2026, 11, 12))
    skip = days[3]
    database.save_bar_data([
        make_bar("000010", Exchange.SZSE,
                 datetime.combine(d, time(0, 0), tzinfo=DB_TZ),
                 interval=Interval.DAILY)
        for d in days if d != skip
    ])

    svc = DataQualityService(database)
    start = datetime(2026, 10, 13, tzinfo=DB_TZ)
    end = datetime(2026, 11, 12, 23, 59, 59, tzinfo=DB_TZ)
    day_dt = datetime.combine(skip, time(0, 0), tzinfo=DB_TZ)
    report = svc.check_bar_data(
        "000010", Exchange.SZSE, Interval.DAILY, start, end,
        probe=lambda symbol, exchange, interval, start_, end_: {day_dt},
    )
    assert report.remote_checked is True
    assert report.remote_gap_count == 1
    assert report.status == QualityStatus.FAIL


def test_universe_uses_probe_and_counts_fail(database: DuckdbDatabase) -> None:
    """universe 检查透传 probe：源头也缺的算 PASS，源头有的算 FAIL。"""
    from datetime import date, time
    days = trading_days_in_range(date(2026, 10, 13), date(2026, 11, 12))
    skip = days[3]
    for symbol, exchange, drop in (
        ("600000", Exchange.SSE, None),
        ("000010", Exchange.SZSE, skip),
    ):
        database.save_bar_data([
            make_bar(symbol, exchange, datetime.combine(d, time(0, 0), tzinfo=DB_TZ),
                     interval=Interval.DAILY)
            for d in days if d != drop
        ])

    # 600000 没有洞 → 不该调用 probe；000010 的洞源头也没有 → PASS
    calls: list[str] = []

    def probe(symbol, exchange, interval, start, end):
        calls.append(symbol)
        return set()

    svc = DataQualityService(database)
    report = svc.check_universe_data(
        [("600000", Exchange.SSE), ("000010", Exchange.SZSE)],
        Interval.DAILY,
        datetime(2026, 10, 13, tzinfo=DB_TZ),
        datetime(2026, 11, 12, 23, 59, 59, tzinfo=DB_TZ),
        probe=probe,
    )
    assert calls == ["000010"]
    assert (report.pass_count, report.fail_count) == (2, 0)
    assert report.status == QualityStatus.PASS
    assert report.failed_symbols == []

    # 换成源头有数据的 probe → 000010 变 FAIL
    all_days: set[datetime] = {
        datetime.combine(d, time(0, 0), tzinfo=DB_TZ) for d in days
    }
    report2 = svc.check_universe_data(
        [("600000", Exchange.SSE), ("000010", Exchange.SZSE)],
        Interval.DAILY,
        datetime(2026, 10, 13, tzinfo=DB_TZ),
        datetime(2026, 11, 12, 23, 59, 59, tzinfo=DB_TZ),
        probe=lambda symbol, exchange, interval, start, end: all_days,
    )
    assert (report2.pass_count, report2.fail_count) == (1, 1)
    assert report2.failed_symbols == ["000010.SZSE"]
    assert report2.status == QualityStatus.FAIL


def test_holiday_is_not_a_gap_and_skips_probe(database: DuckdbDatabase) -> None:
    """节假日不是缺口：传了精确交易日历后不应回源拉数（否则全市场白拉一遍）。"""
    from datetime import date, time
    start = datetime(2026, 9, 28, tzinfo=DB_TZ)
    end = datetime(2026, 10, 9, 23, 59, 59, tzinfo=DB_TZ)
    # 国庆休市：10-01/02/05/06/07/08 不是交易日
    holidays = {
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
        date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8),
    }
    days = [
        d for d in trading_days_in_range(start.date(), end.date())
        if d not in holidays
    ]
    database.save_bar_data([
        make_bar("600036", Exchange.SSE,
                 datetime.combine(d, time(0, 0), tzinfo=DB_TZ),
                 interval=Interval.DAILY)
        for d in days
    ])

    calls: list[str] = []

    def probe(symbol, exchange, interval, start_, end_):
        calls.append(symbol)
        return set()

    svc = DataQualityService(database)
    report = svc.check_bar_data(
        "600036", Exchange.SSE, Interval.DAILY, start, end,
        probe=probe, trading_days=days,
    )
    assert report.expected_count == len(days) == 4
    assert report.actual_count == 4
    assert report.missing_count == 0
    assert calls == []
    assert report.status == QualityStatus.PASS

    # 对照：不传日历（只排除周末）时节假日会被当成洞，于是触发回源
    report2 = svc.check_bar_data(
        "600036", Exchange.SSE, Interval.DAILY, start, end, probe=probe
    )
    assert report2.missing_count == 6
    assert calls == ["600036"]
