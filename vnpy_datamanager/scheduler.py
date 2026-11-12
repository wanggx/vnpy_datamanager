"""每日定时下载调度服务。

阶段一第 3 步：每天 04:00（Asia/Shanghai）自动下载沪深全市场 1m/1d 行情，
写入 vnpy.duckdb 并触发质量检查。采用最近 3 个交易日的追赶策略，失败重试，
应用启动时补偿错过的任务。

调度器复用 ManagerEngine 的 universe 批量下载接口，不另写逐标的下载逻辑。
应用内调度器只在 VeighNa Trader 进程运行时触发；关闭时假设 04:00 不执行，
启动时检查最近任务记录，错过则补偿。
"""
from datetime import datetime, timedelta, time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from threading import Thread
from threading import Event as ThreadEvent

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Interval, Exchange
from vnpy.trader.database import DB_TZ
from vnpy.trader.setting import SETTINGS

from .engine import (
    ManagerEngine, DataUniverse, DownloadMode,
    DownloadTaskResult, EVENT_DM_DOWNLOAD_PROGRESS,
)


# 调度器配置键前缀
SCHEDULE_SETTING_PREFIX: str = "datamanager.schedule"

# 调度任务记录事件
EVENT_DM_SCHEDULE_STATUS: str = "eDmScheduleStatus."


class ScheduleTaskType(Enum):
    """调度任务类型。"""
    DOWNLOAD_1M = "1m"
    DOWNLOAD_1D = "1d"


# 定时下载支持的周期。不含 1h：A 股小时线要另外合成，定时补数收益低。
SUPPORTED_INTERVALS: tuple[str, ...] = ("1m", "1d")


def normalize_intervals(intervals: object) -> list[str]:
    """把配置里的周期收敛到 ``SUPPORTED_INTERVALS``（保序、去重）。

    旧配置可能存着 ``["1m", "1h"]``，这里静默丢掉 1h；全丢光时回退默认
    ``["1m", "1d"]``，避免定时任务因为周期为空而什么都不做。
    """
    if not isinstance(intervals, (list, tuple)):
        return list(SUPPORTED_INTERVALS)
    result: list[str] = []
    for value in intervals:
        text: str = str(value).strip().lower()
        if text in SUPPORTED_INTERVALS and text not in result:
            result.append(text)
    return result or list(SUPPORTED_INTERVALS)


@dataclass
class ScheduledDownloadConfig:
    """每日定时下载配置。"""
    enabled: bool = True
    run_time: str = "04:00"              # Asia/Shanghai 本地时间
    timezone: str = "Asia/Shanghai"
    universe: str = "沪深全市场"
    intervals: list[str] = field(default_factory=lambda: ["1m", "1d"])
    repair_days: int = 3                 # 追赶最近 N 个交易日
    max_retries: int = 3
    retry_delay_seconds: int = 60
    run_when_app_starts: bool = True


@dataclass
class ScheduledDownloadStatus:
    """调度任务执行状态。"""
    task_id: str
    planned_at: datetime                # 计划执行时间
    started_at: datetime | None = None
    finished_at: datetime | None = None
    interval: str = ""
    target_trading_day: str = ""         # 目标交易日 YYYYMMDD
    total_symbols: int = 0
    success_symbols: int = 0
    failed_symbols: int = 0
    coverage_ratio: float = 0.0
    quality_status: str = ""
    retry_count: int = 0
    error_summary: str = ""
    status: str = "pending"             # pending/running/completed/failed/cancelled


class ScheduledDownloadService:
    """每日定时下载调度服务。

    由 ManagerEngine 持有，在 VeighNa Trader 启动时初始化。后台线程按配置时间
    触发，复用 ManagerEngine.download_universe_bar_data 执行批量下载。
    """

    def __init__(
        self,
        engine: ManagerEngine,
        event_engine: EventEngine,
    ) -> None:
        self.engine: ManagerEngine = engine
        self.event_engine: EventEngine = event_engine

        self.config: ScheduledDownloadConfig = self._load_config()
        self._stop_event: ThreadEvent = ThreadEvent()
        self._thread: Thread | None = None
        self._last_status: ScheduledDownloadStatus | None = None
        self._last_run_date: str = ""    # YYYYMMDD，防同一天重复执行

    # -- 配置持久化 ---------------------------------------------------

    def _load_config(self) -> ScheduledDownloadConfig:
        """从 VeighNa SETTINGS 读取配置。"""
        cfg: ScheduledDownloadConfig = ScheduledDownloadConfig()
        raw: dict = SETTINGS.get(f"{SCHEDULE_SETTING_PREFIX}.config", {})
        if not raw:
            return cfg
        cfg.enabled = raw.get("enabled", cfg.enabled)
        cfg.run_time = raw.get("run_time", cfg.run_time)
        cfg.timezone = raw.get("timezone", cfg.timezone)
        cfg.universe = raw.get("universe", cfg.universe)
        cfg.intervals = normalize_intervals(raw.get("intervals", cfg.intervals))
        cfg.repair_days = raw.get("repair_days", cfg.repair_days)
        cfg.max_retries = raw.get("max_retries", cfg.max_retries)
        cfg.retry_delay_seconds = raw.get("retry_delay_seconds", cfg.retry_delay_seconds)
        cfg.run_when_app_starts = raw.get("run_when_app_starts", cfg.run_when_app_starts)
        return cfg

    def save_config(self, config: ScheduledDownloadConfig) -> None:
        """保存配置到 VeighNa SETTINGS。"""
        self.config = config
        SETTINGS[f"{SCHEDULE_SETTING_PREFIX}.config"] = {
            "enabled": config.enabled,
            "run_time": config.run_time,
            "timezone": config.timezone,
            "universe": config.universe,
            "intervals": config.intervals,
            "repair_days": config.repair_days,
            "max_retries": config.max_retries,
            "retry_delay_seconds": config.retry_delay_seconds,
            "run_when_app_starts": config.run_when_app_starts,
        }

    def get_status(self) -> ScheduledDownloadStatus | None:
        """返回最近一次任务状态。"""
        return self._last_status

    # -- 后台线程 -----------------------------------------------------

    def start(self) -> None:
        """启动调度后台线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止调度后台线程。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run_loop(self) -> None:
        """调度主循环：每分钟检查是否到达运行时间。"""
        while not self._stop_event.is_set():
            if not self.config.enabled:
                self._stop_event.wait(timeout=60.0)
                continue

            now: datetime = datetime.now(DB_TZ)
            try:
                target_time: time = time.fromisoformat(self.config.run_time)
            except ValueError:
                target_time = time(4, 0)

            # 到达运行时间且当天未跑过
            today_key: str = now.strftime("%Y%m%d")
            if (now.hour == target_time.hour
                    and now.minute == target_time.minute
                    and today_key != self._last_run_date):
                self._last_run_date = today_key
                self.run_now()

            self._stop_event.wait(timeout=60.0)

    # -- 执行 ---------------------------------------------------------

    def run_now(self) -> ScheduledDownloadStatus:
        """立即执行一次定时下载任务（按配置的周期 + 追赶策略）。"""
        import uuid

        task_id: str = str(uuid.uuid4())
        now: datetime = datetime.now(DB_TZ)
        status: ScheduledDownloadStatus = ScheduledDownloadStatus(
            task_id=task_id, planned_at=now,
        )
        self._last_status = status

        # 确定目标交易日（最近已完成交易日）
        target_day: datetime = self._previous_completed_trading_day(now)
        status.target_trading_day = target_day.strftime("%Y%m%d")

        def output(msg: str) -> None:
            self.engine.write_log(msg, source="Schedule")

        universe: DataUniverse = DataUniverse(name=self.config.universe, source="xtquant")
        start_dt: datetime = now - timedelta(days=self.config.repair_days * 2)
        end_dt: datetime = target_day.replace(hour=15, minute=0, second=0, microsecond=0)

        # 按配置周期分别提交
        for interval_str in self.config.intervals:
            interval: Interval = self._str_to_interval(interval_str)
            if interval is None:
                continue
            status.interval = interval_str
            status.started_at = datetime.now(DB_TZ)
            status.status = "running"
            self._emit_status(status)

            result: DownloadTaskResult = self.engine.download_universe_bar_data(
                universe=universe,
                interval=interval,
                start=start_dt,
                end=end_dt,
                output=output,
                mode=DownloadMode.APPEND,
                task_id=f"{task_id}_{interval_str}",
            )

            status.total_symbols += result.total
            status.success_symbols += result.success
            status.failed_symbols += result.failed
            status.coverage_ratio = (
                status.success_symbols / status.total_symbols
                if status.total_symbols > 0 else 0.0
            )
            status.retry_count = 0

            # 失败重试
            for retry in range(self.config.max_retries):
                if not result.failed_symbols:
                    break
                self._stop_event.wait(timeout=self.config.retry_delay_seconds)
                if self._stop_event.is_set():
                    break
                status.retry_count = retry + 1
                self._emit_status(status)
                retry_result = self.engine.download_universe_bar_data(
                    universe=universe,
                    interval=interval,
                    start=start_dt,
                    end=end_dt,
                    output=output,
                    mode=DownloadMode.APPEND,
                    task_id=f"{task_id}_{interval_str}_retry{retry+1}",
                    failed_only=result.failed_symbols,
                )
                status.success_symbols += retry_result.success
                status.failed_symbols = retry_result.failed.__len__() if hasattr(retry_result, 'failed') else status.failed_symbols
                result = retry_result

        status.finished_at = datetime.now(DB_TZ)
        status.status = "failed" if status.failed_symbols > 0 else "completed"
        status.error_summary = (
            f"失败 {status.failed_symbols} 个标的" if status.failed_symbols else ""
        )
        self._emit_status(status)

        # 持久化任务记录
        self._save_task_record(status)
        return status

    def check_and_compensate_on_start(self) -> None:
        """应用启动补偿：若当天错过执行且无成功记录，立即补偿一次。"""
        if not self.config.run_when_app_starts:
            return
        now: datetime = datetime.now(DB_TZ)
        today_key: str = now.strftime("%Y%m%d")
        self._last_run_date = today_key  # 标记今天，防止主循环重复触发

        # 检查今天是否已有成功记录（读 DuckDB download_task 表）
        if self._has_today_success_record(now):
            return

        # 错过运行时间（当前已过 04:00）才补偿
        try:
            target_time: time = time.fromisoformat(self.config.run_time)
        except ValueError:
            target_time = time(4, 0)
        if now.hour > target_time.hour or (
            now.hour == target_time.hour and now.minute > target_time.minute
        ):
            self.engine.write_log("启动补偿：检测到当天尚未执行定时下载，立即补偿", source="Schedule")
            self.run_now()

    # -- 辅助 ---------------------------------------------------------

    @staticmethod
    def _previous_completed_trading_day(now: datetime) -> datetime:
        """返回最近已完成交易日（排除周末；精确节假日需交易日历）。"""
        d: datetime = now - timedelta(days=1)
        # 跳过周末，最多回退 10 天
        for _ in range(10):
            if d.weekday() < 5:
                # 交易日收盘后才算完成（15:00 后）
                if d.date() < now.date():
                    return d
                if d.date() == now.date() and now.hour >= 15:
                    return d
            d -= timedelta(days=1)
        return now - timedelta(days=1)

    @staticmethod
    def _str_to_interval(s: str) -> Interval | None:
        return {
            "1m": Interval.MINUTE,
            "1d": Interval.DAILY,
        }.get(s)

    def _emit_status(self, status: ScheduledDownloadStatus) -> None:
        """发布调度状态事件。"""
        event: Event = Event(EVENT_DM_SCHEDULE_STATUS, status)
        self.event_engine.put(event)

    def _save_task_record(self, status: ScheduledDownloadStatus) -> None:
        """把任务记录写入 DuckDB download_task 表。"""
        conn = getattr(self.engine.database, "conn", None)
        if conn is None:
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_task (
                task_id VARCHAR PRIMARY KEY,
                planned_at TIMESTAMP,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                universe VARCHAR,
                interval VARCHAR,
                target_trading_day VARCHAR,
                total_symbols INTEGER,
                success_symbols INTEGER,
                failed_symbols INTEGER,
                coverage_ratio DOUBLE,
                quality_status VARCHAR,
                retry_count INTEGER,
                error_summary VARCHAR,
                status VARCHAR
            )
            """
        )
        conn.execute(
            """
            INSERT INTO download_task VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                status.task_id,
                self._naive(status.planned_at),
                self._naive(status.started_at),
                self._naive(status.finished_at),
                self.config.universe,
                status.interval,
                status.target_trading_day,
                status.total_symbols,
                status.success_symbols,
                status.failed_symbols,
                status.coverage_ratio,
                status.quality_status,
                status.retry_count,
                status.error_summary,
                status.status,
            ),
        )

    @staticmethod
    def _naive(dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        if dt.tzinfo:
            return dt.astimezone(DB_TZ).replace(tzinfo=None)
        return dt

    def _has_today_success_record(self, now: datetime) -> bool:
        """检查今天是否已有成功的定时任务记录。"""
        conn = getattr(self.engine.database, "conn", None)
        if conn is None:
            return False
        try:
            today_start: datetime = now.replace(hour=0, minute=0, second=0, microsecond=0)
            row = conn.execute(
                """
                SELECT count(*) FROM download_task
                WHERE planned_at >= ? AND status = 'completed'
                """,
                (self._naive(today_start),),
            ).fetchone()
            return bool(row and row[0] > 0)
        except Exception:
            # 表不存在等情况下不阻断补偿
            return False
