"""数据质量检查服务。

阶段一核心功能：对 DuckDB 中的 Bar/Tick 数据按交易日历和交易时段做连续性、
重复、OHLC、成交量等结构化检查，输出 PASS/FAIL 报告。本地发现缺口时会向
数据源（大 QMT）复核这些时间点：源头同样没有（停牌、无成交等）算正常缺口，
源头有而本地没有才是 FAIL。报告写入同一 vnpy.duckdb 文件的 quality 逻辑表
（与插件行情表物理同库、逻辑分离）。

本模块是阶段三情绪计算的前置门禁：情绪历史构建必须先调用
DataQualityService.check_bar_data，按 PASS/FAIL 决定是否允许计算。
"""
from datetime import datetime, date, time, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

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
    """质量检查结果状态（只有两态）。"""
    PASS = "PASS"      # 本地完整，或缺失时间点在数据源同样不存在（正常缺口）
    FAIL = "FAIL"      # 数据源有数据但本地没有，或存在结构性错误


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

    # 缺口复核：本地按日历算出的缺失点，向数据源再查一遍
    missing_count: int = 0            # 本地缺失点数（含正常缺口）
    remote_gap_count: int = 0         # 数据源有、本地没有（真洞）
    source_missing_count: int = 0     # 数据源同样没有（正常缺口）
    remote_checked: bool = False      # 是否完成了数据源复核

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
    """粗略判断是否为交易日（只排除周末）。

    这只是**回退**用的粗日历：精确判断（含节假日休市）请由调用方通过
    `ManagerEngine.get_trading_dates()`（大 QMT `get_trading_dates`）取回后，
    以 `trading_days=` 传给 `check_bar_data`。否则节假日的正常工作日会被当成
    缺口，进而对全市场触发无谓的回源拉数。
    """
    return d.weekday() < 5


def trading_days_in_range(start: date, end: date) -> list[date]:
    """返回 [start, end] 区间内的交易日（只排除周末，未取精确日历时使用）。"""
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

# 缺口复核回调：把整段检查周期重新向数据源取一次，返回源头真实存在的时间点；
# 返回 None 表示无法复核（数据源未就绪等）。
BarProbe = Callable[
    [str, Exchange, Interval, datetime, datetime], set[datetime] | None
]


class DataQualityService:
    """数据质量检查服务。

    通过 get_database() 拿到的数据库实例读取 Bar/Tick，按交易日历和交易时段
    做结构化检查。发现缺口时用 BarProbe 把整段周期回源重取一次比对：源头同样
    没有的洞算正常，源头有而本地没有才判 FAIL。报告写入同一 DuckDB 文件的
    quality.data_quality_report 与 quality.data_gap 表（若数据库实例暴露连接
    则建表；否则报告仅在内存返回）。
    """

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
        probe: BarProbe | None = None,
        trading_days: list[date] | None = None,
    ) -> DataQualityReport:
        """检查单标的单周期在 [start, end] 区间的数据质量。

        probe 用于回源复核缺口（见 BarProbe）；不传时只要有缺口就判 FAIL。
        trading_days 是 QMT 给的精确交易日历（含节假日剔除）；不传时退化为
        只排除周末的粗日历，节假日的正常工作日在粗日历里会被误判成缺口。
        """
        report: DataQualityReport = DataQualityReport(
            symbol=symbol, exchange=exchange, interval=interval,
            start=start, end=end, source="duckdb",
        )
        days: list[date] = (
            trading_days if trading_days is not None
            else trading_days_in_range(start.date(), end.date())
        )

        # 读取实际数据
        bars: list = self.database.load_bar_data(
            symbol, exchange, interval, start, end
        )
        report.actual_count = len(bars)

        if not bars:
            # 完全没数据也要回源确认：源头也没有才可能是正常（未上市/长期停牌）
            report.expected_count = self._expected_count(interval, start, end, days)
            self._check_gaps(report, interval, start, end, {}, probe, days)
            self._classify_status(report)
            return report

        # 期望根数
        report.expected_count = self._expected_count(
            interval,
            start.astimezone(DB_TZ) if start.tzinfo else start,
            end.astimezone(DB_TZ) if end.tzinfo else end,
            days,
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

        # 缺失 + 非交易时段 + 回源复核
        self._check_gaps(report, interval, start, end, bar_times, probe, days)

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

    def _check_gaps(
        self,
        report: DataQualityReport,
        interval: Interval,
        start: datetime,
        end: datetime,
        bar_times: dict[datetime, Any],
        probe: BarProbe | None,
        days: list[date],
    ) -> None:
        """算缺口区间、向数据源复核缺口，并把结果写进报告。

        只有确实算出缺口（missing_ranges 非空）才会调用 probe 回源拉数。
        """
        missing_points: list[datetime] = []
        if interval == Interval.MINUTE:
            report.missing_ranges, report.outside_session_count = \
                self._check_minute_continuity(
                    report.symbol, report.exchange, start, end, bar_times, days
                )
            missing_points = self._expected_points(
                interval, start, end, bar_times, days
            )
        elif interval == Interval.HOUR:
            report.missing_ranges, report.outside_session_count = \
                self._check_hour_continuity(
                    report.symbol, report.exchange, start, end, bar_times, days
                )
        else:
            report.missing_ranges = self._check_daily_continuity(
                report.symbol, report.exchange, start, end, bar_times, days
            )
            missing_points = self._expected_points(
                interval, start, end, bar_times, days
            )

        report.missing_count = sum(mr.count for mr in report.missing_ranges)

        # 回源复核（整段重取）：源头也没有的洞是正常的（停牌/无成交），
        # 源头有而本地没有才是真洞。
        if not report.missing_ranges:
            return
        if probe is None:
            report.remote_gap_count = report.missing_count
            report.error = "缺口未经数据源复核"
            return
        if not missing_points:
            report.remote_gap_count = report.missing_count
            report.error = "该周期暂不支持回源复核"
            return
        available: set[datetime] | None = probe(
            report.symbol, report.exchange, interval, start, end
        )
        if available is None:
            report.remote_gap_count = report.missing_count
            report.error = "数据源复核失败"
            return
        report.remote_checked = True
        report.remote_gap_count = sum(1 for dt in missing_points if dt in available)
        report.source_missing_count = report.missing_count - report.remote_gap_count
        if report.remote_gap_count:
            report.error = f"数据源有 {report.remote_gap_count} 根本地缺失"

    def _expected_points(
        self,
        interval: Interval,
        start: datetime,
        end: datetime,
        bar_times: dict[datetime, Any],
        days: list[date],
    ) -> list[datetime]:
        """按交易日历列出本地缺失的时间点（用于回源复核）。"""
        points: list[datetime] = []
        for d in days:
            if interval == Interval.DAILY:
                dt: datetime = datetime.combine(d, time(0, 0), tzinfo=DB_TZ)
                if start <= dt <= end and dt not in bar_times:
                    points.append(dt)
                continue
            for t in expected_minutes_for_day(d):
                dt = datetime.combine(d, t).replace(tzinfo=DB_TZ)
                if start <= dt <= end and dt not in bar_times:
                    points.append(dt)
        return points

    def _classify_status(self, report: DataQualityReport) -> None:
        """只有两态：缺口经数据源复核为正常才算 PASS，否则 FAIL。"""
        if report.ohlc_error_count > 0 or report.volume_error_count > 0:
            report.status = QualityStatus.FAIL
            return
        if report.remote_gap_count > 0:
            report.status = QualityStatus.FAIL
            return
        report.status = QualityStatus.PASS

    def _expected_count(
        self,
        interval: Interval,
        start: datetime,
        end: datetime,
        days: list[date],
    ) -> int:
        """计算 [start, end] 区间的期望根数。"""
        if interval == Interval.MINUTE:
            count: int = 0
            for d in days:
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
            for d in days:
                for h in (10, 11, 14, 15):
                    dt = datetime(d.year, d.month, d.day, h, 30, tzinfo=DB_TZ)
                    if start <= dt <= end:
                        count += 1
            return count
        elif interval == Interval.DAILY:
            return len(days)
        return 0

    def _check_minute_continuity(
        self,
        symbol: str,
        exchange: Exchange,
        start: datetime,
        end: datetime,
        bar_times: dict[datetime, Any],
        days: list[date],
    ) -> tuple[list[MissingRange], int]:
        """检查 1m 连续性，返回 (缺失区间, 非交易时段数据数)。"""
        missing_ranges: list[MissingRange] = []
        outside: int = 0
        expected_set: set[time] = set(expected_minutes_for_day(date(2024, 1, 2)))

        for d in days:
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
        days: list[date],
    ) -> tuple[list[MissingRange], int]:
        """检查 1h 连续性。"""
        missing_ranges: list[MissingRange] = []
        # 1h 期望时间点（vnpy 约定标记区间开始：10:00, 11:00, 14:00, 15:00
        # 经 xt_datafeed adjustment 减 1h 后为 9:30, 10:30, 13:30, 14:30 左右）
        # 第一版用宽松判断：只统计缺失交易日数
        for d in days:
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
        days: list[date],
    ) -> list[MissingRange]:
        """检查日线连续性（缺失交易日）。"""
        missing_ranges: list[MissingRange] = []
        for d in days:
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
        probe: BarProbe | None = None,
        trading_days: list[date] | None = None,
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
                symbol, exchange, interval, start, end,
                probe=probe, trading_days=trading_days,
            )
            report.symbol_reports.append(sr)
            total_expected += sr.expected_count
            total_actual += sr.actual_count

            if sr.status == QualityStatus.PASS:
                report.pass_count += 1
            else:
                report.fail_count += 1

        report.overall_coverage = (
            total_actual / total_expected
            if total_expected > 0 else 0.0
        )
        report.status = (
            QualityStatus.FAIL if report.fail_count > 0 else QualityStatus.PASS
        )

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
