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


def test_check_warn_missing_some_bars(database: DuckdbDatabase) -> None:
    """缺失少量 1m bar 应判 WARN（覆盖率 0.95 以下、0.5 以上）。"""
    from datetime import date
    d = date(2024, 1, 2)
    bars = []
    times = expected_minutes_for_day(d)
    # 跳过 20 根（240 - 20 = 220，覆盖率 0.917）
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
    assert report.status == QualityStatus.WARN
    assert len(report.missing_ranges) > 0


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
