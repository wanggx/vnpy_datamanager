"""数据质量检查服务。

阶段一核心功能：对 DuckDB 中的 Bar/Tick 数据按交易日历和交易时段做连续性、
重复、OHLC、成交量等结构化检查，输出 PASS/WARN/FAIL 报告。报告写入同一
vnpy.duckdb 文件的 quality 逻辑表（与插件行情表物理同库、逻辑分离）。

本模块是阶段三情绪计算的前置门禁：情绪历史构建必须先调用
DataQualityService.check_bar_data，按 PASS/WARN/FAIL 决定是否允许计算。
"""
from datetime import datetime, date, time, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import BaseDatabase, get_database, DB_TZ


# ---------------------------------------------------------------------
# 交易时段与交易日历
# ---------------------------------------------------------------------

# A 股交易时段（分钟线期望根的时间点：09:31 起为第一根分钟线收盘）
MORNING_START: time = time(9, 31)
MORNING_END: time = time(11, 30)
AFTERNOON_START: time = time(13, 1)
AFTERNOON_END: time = time(15, 0)

# 沪深交易所集合
A_SHARE_EXCHANGES: tuple[Exchange, ...] = (Exchange.SSE, Exchange.SZSE, Exchange.BSE)


class QualityStatus(Enum):
    """质量检查结果状态。"""
    PASS = "PASS"      # 该日所有关键标的和周期数据完整
    WARN = "WARN"      # 有少量缺失，但达到允许覆盖率
    FAIL = "FAIL"      # 整日缺失或关键标的缺失


@dataclass
class MissingRange:
    """缺失时间段。"""
    start: datetime
    end: datetime
    count: int = 0     # 缺失根数


@dataclass
class DataQualityReport:
    """单标的单周期单区间的质量报告。"""
    symbol: str
    exchange: Exchange
    interval: Interval
    start: datetime
    end: datetime

    expected_count: int = 0          # 期望根数
    actual_count: int = 0            # 实际根数
    duplicate_count: int = 0          # 重复时间戳数
    missing_ranges: list[MissingRange] = field(default_factory=list)
    outside_session_count: int = 0   # 非交易时段数据数
    ohlc_error_count: int = 0        # OHLC 关系错误数
    volume_error_count: int = 0       # 成交量/成交额异常数

    status: QualityStatus = QualityStatus.PASS
    error: str = ""

    # 元数据
    source: str = ""
    download_batch_id: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(DB_TZ))


@dataclass
class UniverseQualityReport:
    """universe 批量质量报告（总体 + 逐标的）。"""
    universe: str
    interval: Interval
    start: datetime
    end: datetime

    total_symbols: int = 0
    pass_count: int = 0
    warn_count: int = 0
    fail_count: int = 0
    overall_coverage: float = 0.0     # 总体覆盖率 actual/expected

    symbol_reports: list[DataQualityReport] = field(default_factory=list)
    status: QualityStatus = QualityStatus.PASS

    @property
    def failed_symbols(self) -> list[str]:
        """FAIL 状态的标的列表。"""
        return [
            f"{r.symbol}.{r.exchange.value}"
            for r in self.symbol_reports
            if r.status == QualityStatus.FAIL
        ]


# ---------------------------------------------------------------------
# 交易日历辅助
# ---------------------------------------------------------------------

def is_trading_day(d: date) -> bool:
    """粗略判断是否为交易日（排除周末）。

    精确的节假日判断需交易日历；第一版仅排除周末，停牌/半日市/节假日由
    数据缺失规则与覆盖率阈值容忍。后续可接入 xtdata.get_trading_dates
    或本地交易日历表。
    """
    return d.weekday() < 5


def trading_days_in_range(start: date, end: date) -> list[date]:
    """返回 [start, end] 区间内的交易日（排除周末）。"""
    days: list[date] = []
    cur: date = start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def expected_minutes_for_day(d: date) -> list[time]:
    """生成某交易日的期望分钟线时间点集合（09:31-11:30, 13:01-15:00）。

    返回 240 个 time（上午 120 + 下午 120）。
    """
    times: list[time] = []
    # 上午 09:31 - 11:30
    t: datetime = datetime.combine(d, MORNING_START)
    end_am: datetime = datetime.combine(d, MORNING_END)
    while t <= end_am:
        times.append(t.time())
        t += timedelta(minutes=1)
    # 下午 13:01 - 15:00
    t = datetime.combine(d, AFTERNOON_START)
    end_pm: datetime = datetime.combine(d, AFTERNOON_END)
    while t <= end_pm:
        times.append(t.time())
        t += timedelta(minutes=1)
    return times


# ---------------------------------------------------------------------
# DataQualityService
# ---------------------------------------------------------------------

class DataQualityService:
    """数据质量检查服务。

    通过 get_database() 拿到的数据库实例读取 Bar/Tick，按交易日历和交易时段
    做结构化检查。报告写入同一 DuckDB 文件的 quality.data_quality_report
    与 quality.data_gap 表（若数据库实例暴露连接则建表；否则报告仅在内存返回）。
    """

    # 1m 检查：覆盖率低于此阈值标 FAIL（整标的区间）
    FAIL_COVERAGE_THRESHOLD: float = 0.5
    WARN_COVERAGE_THRESHOLD: float = 0.95

    def __init__(self, database: BaseDatabase | None = None) -> None:
        self.database: BaseDatabase = database or get_database()

    # -- 单标的检查 ----------------------------------------------------

    def check_bar_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> DataQualityReport:
        """检查单标的单周期在 [start, end] 区间的数据质量。"""
        report: DataQualityReport = DataQualityReport(
            symbol=symbol, exchange=exchange, interval=interval,
            start=start, end=end, source="duckdb",
        )

        # 读取实际数据
        bars: list = self.database.load_bar_data(
            symbol, exchange, interval, start, end
        )
        report.actual_count = len(bars)

        if not bars:
            report.status = QualityStatus.FAIL
            report.error = "无数据"
            self._classify_status(report)
            return report

        # 期望根数
        report.expected_count = self._expected_count(
            interval, start.astimezone(DB_TZ) if start.tzinfo else start,
            end.astimezone(DB_TZ) if end.tzinfo else end,
        )

        # 重复时间戳
        seen: set[datetime] = set()
        duplicate: int = 0
        for b in bars:
            dt: datetime = b.datetime
            if dt in seen:
                duplicate += 1
            else:
                seen.add(dt)
        report.duplicate_count = duplicate

        # 时间点集合（带日期）
        bar_times: dict[datetime, Any] = {b.datetime: b for b in bars}

        # 缺失 + 非交易时段
        missing_count: int = 0
        if interval == Interval.MINUTE:
            missing_ranges, outside = self._check_minute_continuity(
                symbol, exchange, start, end, bar_times
            )
            report.missing_ranges = missing_ranges
            report.outside_session_count = outside
            missing_count = sum(mr.count for mr in missing_ranges)
        elif interval == Interval.HOUR:
            missing_ranges, outside = self._check_hour_continuity(
                symbol, exchange, start, end, bar_times
            )
            report.missing_ranges = missing_ranges
            report.outside_session_count = outside
            missing_count = sum(mr.count for mr in missing_ranges)
        else:
            # 日线：检查交易日缺失
            missing_ranges = self._check_daily_continuity(
                symbol, exchange, start, end, bar_times
            )
            report.missing_ranges = missing_ranges
            missing_count = sum(mr.count for mr in missing_ranges)

        # OHLC 与成交量异常
        ohlc_err: int = 0
        vol_err: int = 0
        for b in bars:
            # high 不应低于 open/close 最大值
            if b.high_price < max(b.open_price, b.close_price):
                ohlc_err += 1
            # low 不应高于 open/close 最小值
            if b.low_price > min(b.open_price, b.close_price):
                ohlc_err += 1
            # high 不应低于 low
            if b.high_price < b.low_price:
                ohlc_err += 1
            # 负成交量/成交额
            if b.volume < 0 or b.turnover < 0:
                vol_err += 1
        report.ohlc_error_count = ohlc_err
        report.volume_error_count = vol_err

        self._classify_status(report)
        return report

    def _classify_status(self, report: DataQualityReport) -> None:
        """根据各项指标判定 PASS/WARN/FAIL。"""
        if report.actual_count == 0:
            report.status = QualityStatus.FAIL
            return

        # OHLC 错误或重复过多直接 FAIL
        if report.ohlc_error_count > 0:
            report.status = QualityStatus.FAIL
            return

        coverage: float = (
            report.actual_count / report.expected_count
            if report.expected_count > 0 else 1.0
        )
        if coverage < self.FAIL_COVERAGE_THRESHOLD:
            report.status = QualityStatus.FAIL
        elif coverage < self.WARN_COVERAGE_THRESHOLD:
            report.status = QualityStatus.WARN
        else:
            report.status = QualityStatus.PASS

    def _expected_count(
        self, interval: Interval, start: datetime, end: datetime
    ) -> int:
        """计算 [start, end] 区间的期望根数。"""
        if interval == Interval.MINUTE:
            count: int = 0
            for d in trading_days_in_range(start.date(), end.date()):
                # 该日期望 240 根，但起止日按时间截断
                day_times = expected_minutes_for_day(d)
                for t in day_times:
                    dt: datetime = datetime.combine(d, t).replace(tzinfo=DB_TZ)
                    if start <= dt <= end:
                        count += 1
            return count
        elif interval == Interval.HOUR:
            # 每个交易日 4 根小时线（10:30, 11:30, 14:00, 15:00 大致）
            count = 0
            for d in trading_days_in_range(start.date(), end.date()):
                for h in (10, 11, 14, 15):
                    dt = datetime(d.year, d.month, d.day, h, 30, tzinfo=DB_TZ)
                    if start <= dt <= end:
                        count += 1
            return count
        elif interval == Interval.DAILY:
            return len(trading_days_in_range(start.date(), end.date()))
        return 0

    def _check_minute_continuity(
        self,
        symbol: str,
        exchange: Exchange,
        start: datetime,
        end: datetime,
        bar_times: dict[datetime, Any],
    ) -> tuple[list[MissingRange], int]:
        """检查 1m 连续性，返回 (缺失区间, 非交易时段数据数)。"""
        missing_ranges: list[MissingRange] = []
        outside: int = 0
        expected_set: set[time] = set(expected_minutes_for_day(date(2024, 1, 2)))

        for d in trading_days_in_range(start.date(), end.date()):
            day_times: list[datetime] = [
                datetime.combine(d, t).replace(tzinfo=DB_TZ)
                for t in expected_minutes_for_day(d)
            ]
            # 该日缺失的时间点
            missing_today: list[datetime] = []
            for dt in day_times:
                if start <= dt <= end and dt not in bar_times:
                    missing_today.append(dt)
            if missing_today:
                missing_ranges.append(MissingRange(
                    start=missing_today[0], end=missing_today[-1],
                    count=len(missing_today),
                ))

        # 非交易时段：实际数据中时间不在期望集合内的
        expected_todays: set[time] = {
            t for t in expected_minutes_for_day(date(2024, 1, 2))
        }
        for dt in bar_times:
            if dt.time() not in expected_todays:
                outside += 1

        return missing_ranges, outside

    def _check_hour_continuity(
        self,
        symbol: str,
        exchange: Exchange,
        start: datetime,
        end: datetime,
        bar_times: dict[datetime, Any],
    ) -> tuple[list[MissingRange], int]:
        """检查 1h 连续性。"""
        missing_ranges: list[MissingRange] = []
        # 1h 期望时间点（vnpy 约定标记区间开始：10:00, 11:00, 14:00, 15:00
        # 经 xt_datafeed adjustment 减 1h 后为 9:30, 10:30, 13:30, 14:30 左右）
        # 第一版用宽松判断：只统计缺失交易日数
        for d in trading_days_in_range(start.date(), end.date()):
            day_bars = [dt for dt in bar_times if dt.date() == d]
            if not day_bars and start.date() <= d <= end.date():
                missing_ranges.append(MissingRange(
                    start=datetime.combine(d, time(0, 0), tzinfo=DB_TZ),
                    end=datetime.combine(d, time(15, 0), tzinfo=DB_TZ),
                    count=4,
                ))
        return missing_ranges, 0

    def _check_daily_continuity(
        self,
        symbol: str,
        exchange: Exchange,
        start: datetime,
        end: datetime,
        bar_times: dict[datetime, Any],
    ) -> list[MissingRange]:
        """检查日线连续性（缺失交易日）。"""
        missing_ranges: list[MissingRange] = []
        for d in trading_days_in_range(start.date(), end.date()):
            dt: datetime = datetime.combine(d, time(0, 0), tzinfo=DB_TZ)
            if start <= dt <= end and dt not in bar_times:
                missing_ranges.append(MissingRange(start=dt, end=dt, count=1))
        return missing_ranges

    # -- universe 批量检查 --------------------------------------------

    def check_universe_data(
        self,
        symbols: list[tuple[str, Exchange]],
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> UniverseQualityReport:
        """批量检查 universe 中所有标的的质量。"""
        report: UniverseQualityReport = UniverseQualityReport(
            universe=f"{len(symbols)}个标的", interval=interval,
            start=start, end=end,
        )
        report.total_symbols = len(symbols)

        total_expected: int = 0
        total_actual: int = 0

        for symbol, exchange in symbols:
            sr: DataQualityReport = self.check_bar_data(
                symbol, exchange, interval, start, end
            )
            report.symbol_reports.append(sr)
            total_expected += sr.expected_count
            total_actual += sr.actual_count

            if sr.status == QualityStatus.PASS:
                report.pass_count += 1
            elif sr.status == QualityStatus.WARN:
                report.warn_count += 1
            else:
                report.fail_count += 1

        report.overall_coverage = (
            total_actual / total_expected
            if total_expected > 0 else 0.0
        )

        # 总体状态：任一 FAIL 且覆盖率低则 FAIL，否则按覆盖率
        if report.fail_count > 0 and report.overall_coverage < self.FAIL_COVERAGE_THRESHOLD:
            report.status = QualityStatus.FAIL
        elif report.warn_count > 0 or report.overall_coverage < self.WARN_COVERAGE_THRESHOLD:
            report.status = QualityStatus.WARN
        else:
            report.status = QualityStatus.PASS

        return report

    # -- 持久化 -------------------------------------------------------

    def save_report(self, report: DataQualityReport) -> None:
        """把质量报告写入 DuckDB quality.data_quality_report 表。

        复用 get_database() 实例的连接（DuckdbDatabase 暴露 conn 属性）。
        若数据库实例无 conn 属性（非 DuckDB），则跳过持久化，报告仅在内存。
        """
        conn = getattr(self.database, "conn", None)
        if conn is None:
            return

        self._ensure_quality_tables(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO quality_data_quality_report BY NAME
            SELECT * FROM (VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ) AS t(
                symbol, exchange, interval, start_dt, end_dt,
                expected_count, actual_count, duplicate_count,
                outside_session_count, ohlc_error_count, volume_error_count,
                status, error, source, download_batch_id, checked_at, missing_count
            )
            """,
            (
                report.symbol, report.exchange.value, report.interval.value,
                self._to_naive(report.start), self._to_naive(report.end),
                report.expected_count, report.actual_count,
                report.duplicate_count, report.outside_session_count,
                report.ohlc_error_count, report.volume_error_count,
                report.status.value, report.error, report.source,
                report.download_batch_id, self._to_naive(report.checked_at),
                sum(mr.count for mr in report.missing_ranges),
            )
        )

    @staticmethod
    def _to_naive(dt: datetime) -> datetime:
        """把带时区时间转为 DB_TZ 无时区时间（与插件存储约定一致）。"""
        if dt.tzinfo:
            return dt.astimezone(DB_TZ).replace(tzinfo=None)
        return dt

    @staticmethod
    def _ensure_quality_tables(conn: Any) -> None:
        """创建 quality 逻辑表（若不存在）。与插件行情表物理同库、逻辑分离。"""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quality_data_quality_report (
                symbol VARCHAR NOT NULL,
                exchange VARCHAR NOT NULL,
                interval VARCHAR NOT NULL,
                start_dt TIMESTAMP NOT NULL,
                end_dt TIMESTAMP NOT NULL,
                expected_count INTEGER NOT NULL,
                actual_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
                outside_session_count INTEGER NOT NULL,
                ohlc_error_count INTEGER NOT NULL,
                volume_error_count INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                error VARCHAR,
                source VARCHAR,
                download_batch_id VARCHAR,
                checked_at TIMESTAMP,
                missing_count INTEGER NOT NULL,
                PRIMARY KEY (symbol, exchange, interval, start_dt, end_dt)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quality_data_gap (
                symbol VARCHAR NOT NULL,
                exchange VARCHAR NOT NULL,
                interval VARCHAR NOT NULL,
                start_dt TIMESTAMP NOT NULL,
                end_dt TIMESTAMP NOT NULL,
                count INTEGER NOT NULL,
                checked_at TIMESTAMP,
                PRIMARY KEY (symbol, exchange, interval, start_dt, end_dt)
            )
            """
        )
