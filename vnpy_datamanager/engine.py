"""历史数据的导入、导出、查询和下载。"""
import csv
from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TextIO

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.constant import Interval, Exchange
from vnpy.trader.object import BarData, TickData, ContractData, HistoryRequest
from vnpy.trader.database import BaseDatabase, get_database, BarOverview, DB_TZ
from vnpy.trader.datafeed import BaseDatafeed, get_datafeed
from vnpy.trader.utility import ZoneInfo

APP_NAME: str = "DataManager"

# 批量下载进度事件类型（UI 与调度器订阅）
EVENT_DM_DOWNLOAD_PROGRESS: str = "eDmDownloadProgress."


# 全市场标的枚举常量（与 vnpy_scripttrader/script/market/a_share.py 对齐，
# 避免市场过滤口径散落多处定义导致漂移）。
PRIMARY_SECTOR: str = "沪深A股"
FALLBACK_SECTORS: tuple[str, ...] = ()
VALID_MARKETS: tuple[str, ...] = (".SH", ".SZ")


class DownloadMode(Enum):
    """批量下载模式。"""
    APPEND = "append"      # 只下载已有结束时间之后
    REPAIR = "repair"      # 只下载指定区间缺口
    REPLACE = "replace"    # 删除指定市场和周期数据后重新下载


class DownloadTaskStatus(Enum):
    """批量下载任务状态。"""
    PENDING = "pending"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class DataUniverse:
    """批量下载的标的集合描述。"""
    name: str                           # 集合名称，如"沪深全市场"
    symbols: list[str] = field(default_factory=list)   # 显式标的列表
    exchanges: list[Exchange] = field(default_factory=list)
    source: str = "xtquant"             # 数据来源标识


@dataclass
class DownloadProgress:
    """批量下载进度事件载荷。"""
    task_id: str
    phase: str                           # 当前阶段描述
    total_symbols: int
    completed_symbols: int
    failed_symbols: int
    skipped_symbols: int
    current_symbol: str
    status: DownloadTaskStatus
    error: str = ""


@dataclass
class DownloadTaskResult:
    """批量下载任务汇总结果。"""
    task_id: str
    universe: str
    interval: Interval | None
    start: datetime
    end: datetime
    mode: DownloadMode
    total: int
    success: int
    failed: int
    skipped: int
    failed_symbols: list[str] = field(default_factory=list)
    status: DownloadTaskStatus = DownloadTaskStatus.COMPLETED
    error: str = ""


class ManagerEngine(BaseEngine):
    """历史数据的导入、导出、查询和下载。"""

    def __init__(
        self,
        main_engine: MainEngine,
        event_engine: EventEngine,
    ) -> None:
        """取得数据库和数据服务。"""
        super().__init__(main_engine, event_engine, APP_NAME)

        self.database: BaseDatabase = get_database()
        self.datafeed: BaseDatafeed = get_datafeed()
        # 批量任务：task_id -> 取消标志；UI 与调度器共用
        self._download_tasks: dict[str, dict] = {}

        # 每日定时下载调度服务（不自动启动；由应用层 start_scheduler() 显式拉起，
        # 避免测试与离线场景误起后台线程）。复用本 engine 的 universe 批量接口。
        from .scheduler import ScheduledDownloadService
        self.scheduler: ScheduledDownloadService = ScheduledDownloadService(self, event_engine)

    def start_scheduler(self) -> None:
        """启动每日定时下载调度（应用启动时调用一次）。"""
        self.scheduler.start()
        self.scheduler.check_and_compensate_on_start()

    def write_log(self, msg: str, source: str = APP_NAME) -> None:
        """写日志（转发到主引擎）

        ``BaseEngine`` 本身没有 ``write_log``，只有 ``MainEngine`` 有；界面、
        调度器与质检对话框都按 ``engine.write_log(msg, source=...)`` 调用，
        缺这个方法时每打一行日志就抛 AttributeError 把下载线程带崩。
        """
        self.main_engine.write_log(msg, source)

    def stop_scheduler(self) -> None:
        """停止调度后台线程（应用关闭时调用）。"""
        self.scheduler.stop()

    def import_data_from_csv(
        self,
        file_path: str,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        tz_name: str,
        datetime_head: str,
        open_head: str,
        high_head: str,
        low_head: str,
        close_head: str,
        volume_head: str,
        turnover_head: str,
        open_interest_head: str,
        datetime_format: str
    ) -> tuple:
        """按表头解析 CSV 并写入 K 线：指定了时间格式则按该格式解析，否则用 fromisoformat；成交额或持仓量缺列时记为 0，返回开始时间、结束时间和条数。"""
        f: TextIO
        with open(file_path) as f:
            buf: list = [line.replace("\0", "") for line in f]

        reader: csv.DictReader = csv.DictReader(buf, delimiter=",")

        bars: list[BarData] = []
        start: datetime | None = None
        count: int = 0
        tz: ZoneInfo = ZoneInfo(tz_name)

        item: dict[str, str]
        for item in reader:
            if datetime_format:
                dt: datetime = datetime.strptime(item[datetime_head], datetime_format)
            else:
                dt = datetime.fromisoformat(item[datetime_head])
            dt = dt.replace(tzinfo=tz)

            turnover: str | int = item.get(turnover_head, 0)
            open_interest: str | int = item.get(open_interest_head, 0)

            bar: BarData = BarData(
                symbol=symbol,
                exchange=exchange,
                datetime=dt,
                interval=interval,
                volume=float(item[volume_head]),
                open_price=float(item[open_head]),
                high_price=float(item[high_head]),
                low_price=float(item[low_head]),
                close_price=float(item[close_head]),
                turnover=float(turnover),
                open_interest=float(open_interest),
                gateway_name="DB",
            )

            bars.append(bar)

            # do some statistics
            count += 1
            if not start:
                start = bar.datetime

        end: datetime = bar.datetime

        # insert into database
        self.database.save_bar_data(bars)

        return start, end, count

    def output_data_to_csv(
        self,
        file_path: str,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime
    ) -> bool:
        """把区间内 K 线写成 CSV，成功返回 True，没有写权限时返回 False。"""
        bars: list[BarData] = self.load_bar_data(symbol, exchange, interval, start, end)

        fieldnames: list = [
            "symbol",
            "exchange",
            "datetime",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
            "open_interest"
        ]

        try:
            f: TextIO
            with open(file_path, "w") as f:
                writer: csv.DictWriter = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()

                bar: BarData
                for bar in bars:
                    d: dict = {
                        "symbol": bar.symbol,
                        "exchange": bar.exchange.value,
                        "datetime": bar.datetime.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": bar.open_price,
                        "high": bar.high_price,
                        "low": bar.low_price,
                        "close": bar.close_price,
                        "turnover": bar.turnover,
                        "volume": bar.volume,
                        "open_interest": bar.open_interest,
                    }
                    writer.writerow(d)

            return True
        except PermissionError:
            return False

    def get_bar_overview(self) -> list[BarOverview]:
        """返回数据库中的 K 线概况。"""
        overview: list[BarOverview] = self.database.get_bar_overview()
        return overview

    def load_bar_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime
    ) -> list[BarData]:
        """从数据库加载指定区间的 K 线。"""
        bars: list[BarData] = self.database.load_bar_data(
            symbol,
            exchange,
            interval,
            start,
            end
        )

        return bars

    def delete_bar_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval
    ) -> int:
        """删除该合约和周期的 K 线并返回删除条数。"""
        count: int = self.database.delete_bar_data(
            symbol,
            exchange,
            interval
        )

        return count

    def download_bar_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        output: Callable,
        end: datetime | None = None
    ) -> int:
        """
        从数据服务查询 K 线数据。
        end 为空时回退到当前时间，保持向后兼容。
        """
        req: HistoryRequest = HistoryRequest(
            symbol=symbol,
            exchange=exchange,
            interval=Interval(interval),
            start=start,
            end=end or datetime.now(DB_TZ)
        )

        vt_symbol: str = f"{symbol}.{exchange.value}"
        contract: ContractData | None = self.main_engine.get_contract(vt_symbol)

        # If history data provided in gateway, then query
        if contract and contract.history_data:
            data: list[BarData] = self.main_engine.query_history(
                req, contract.gateway_name
            )
        # Otherwise use datafeed to query data
        else:
            data = self.datafeed.query_bar_history(req, output)

        if data:
            self.database.save_bar_data(data)
            return (len(data))

        return 0

    def download_tick_data(
        self,
        symbol: str,
        exchange: Exchange,
        start: datetime,
        output: Callable,
        end: datetime | None = None
    ) -> int:
        """
        从数据服务查询 Tick 数据。
        end 为空时回退到当前时间，保持向后兼容。
        """
        req: HistoryRequest = HistoryRequest(
            symbol=symbol,
            exchange=exchange,
            start=start,
            end=end or datetime.now(DB_TZ)
        )

        data: list[TickData] = self.datafeed.query_tick_history(req, output)

        if data:
            self.database.save_tick_data(data)
            return (len(data))

        return 0

    # ----------------------------------------------------------------
    # 全市场/股票池批量下载（阶段一）
    # ----------------------------------------------------------------

    @staticmethod
    def _get_xtdata():
        """惰性获取 xtdata 模块（与 XtDatafeed 同源：bigqmt_signal_trader.xtquant_compat）。

        datamanager 直接调 xtdata 枚举全市场标的；下载仍走 self.datafeed，保持
        datafeed 抽象不变。xtdata 不可用时返回 None，由调用方降级处理。
        """
        try:
            from bigqmt_signal_trader.xtquant_compat import xtdata  # noqa: TYT003
            return xtdata
        except Exception:
            return None

    def get_universe_symbols(self, universe: DataUniverse) -> list[tuple[str, Exchange]]:
        """解析 universe 为 (symbol, exchange) 列表。

        - universe.symbols 非空时直接用显式列表（按后缀推断交易所）。
        - universe.symbols 为空且 source 含 xtquant 时，调 xtdata.get_stock_list_in_sector
          枚举沪深全市场 A 股（PRIMARY_SECTOR），过滤北交所，与
          vnpy_scripttrader 的 get_all_stock_codes 口径一致。
        """
        # 显式标的列表
        if universe.symbols:
            return [self._parse_vt_symbol(s) for s in universe.symbols]

        # 从 xtquant 板块枚举全市场
        if universe.source != "xtquant":
            return []

        xtdata = self._get_xtdata()
        if xtdata is None:
            return []

        stock_codes: list[str] = xtdata.get_stock_list_in_sector(PRIMARY_SECTOR) or []
        if not stock_codes and FALLBACK_SECTORS:
            for sector in FALLBACK_SECTORS:
                stock_codes.extend(xtdata.get_stock_list_in_sector(sector) or [])

        # 过滤沪深 A 股，去重排序
        codes: list[str] = sorted({c for c in stock_codes if c.endswith(VALID_MARKETS)})
        return [self._parse_vt_symbol(c) for c in codes]

    @staticmethod
    def _parse_vt_symbol(code: str) -> tuple[str, Exchange]:
        """把 xtquant 代码（如 600000.SH）解析为 (symbol, Exchange)。"""
        if code.endswith(".SH"):
            return code[:-3], Exchange.SSE
        if code.endswith(".SZ"):
            return code[:-3], Exchange.SZSE
        if code.endswith(".BJ"):
            return code[:-3], Exchange.BSE
        # 兜底：无法识别交易所时按代码首位猜测（6/5开头沪市，0/3深市）
        if code and code[0] == "6":
            return code, Exchange.SSE
        return code, Exchange.SZSE

    def _emit_progress(self, progress: DownloadProgress) -> None:
        """发布批量下载进度事件。"""
        event: Event = Event(EVENT_DM_DOWNLOAD_PROGRESS, progress)
        self.event_engine.put(event)

    def _register_task(self, task_id: str) -> None:
        """登记一个活动任务（取消标志初始化为 False）。"""
        self._download_tasks[task_id] = {"cancel": False}

    def _is_cancelled(self, task_id: str) -> bool:
        """检查任务是否被请求取消。"""
        return self._download_tasks.get(task_id, {}).get("cancel", False)

    def cancel_download_task(self, task_id: str) -> None:
        """请求取消某个批量下载任务（在当前标的完成后停止）。"""
        if task_id in self._download_tasks:
            self._download_tasks[task_id]["cancel"] = True

    def _finish_task(self, task_id: str) -> None:
        """任务结束后清理登记。"""
        self._download_tasks.pop(task_id, None)

    def download_universe_bar_data(
        self,
        universe: DataUniverse,
        interval: Interval,
        start: datetime,
        end: datetime,
        output: Callable,
        mode: DownloadMode = DownloadMode.APPEND,
        task_id: str = "",
        failed_only: list[str] | None = None,
    ) -> DownloadTaskResult:
        """批量下载 universe 中所有标的的 Bar 并写入 DuckDB。

        - mode=APPEND：先查 overview，只下载已有结束时间之后的缺口；无 overview 的标的
          全量下载 [start, end]。
        - mode=REPLACE：先删除 universe 中各标的该周期数据，再全量下载 [start, end]。
        - mode=REPAIR：仅下载 failed_only 指定标的（接口预留，第一版行为同 APPEND）。
        - failed_only 非空时只重试这些标的。
        - 逐标的调用 self.download_bar_data（走 datafeed），分批进度回调，支持取消。
        - 已完成标的不重复下载（APPEND 模式下覆盖到 end 的跳过）。
        """
        import uuid

        task_id = task_id or str(uuid.uuid4())
        self._register_task(task_id)

        # 解析标的
        if failed_only is not None:
            symbols: list[tuple[str, Exchange]] = [self._parse_vt_symbol(s) for s in failed_only]
        else:
            symbols = self.get_universe_symbols(universe)

        total: int = len(symbols)
        success: int = 0
        failed: int = 0
        skipped: int = 0
        failed_symbols: list[str] = []

        self._emit_progress(DownloadProgress(
            task_id=task_id, phase="开始批量下载", total_symbols=total,
            completed_symbols=0, failed_symbols=0, skipped_symbols=0,
            current_symbol="", status=DownloadTaskStatus.RUNNING,
        ))

        # REPLACE 模式先删除
        if mode == DownloadMode.REPLACE and failed_only is None:
            for symbol, exchange in symbols:
                self.delete_bar_data(symbol, exchange, interval)

        # 现有覆盖范围（APPEND 模式跳过已覆盖到 end 的标的）
        overview_map: dict[tuple[str, Exchange], BarOverview] = {}
        if mode == DownloadMode.APPEND and failed_only is None:
            for ov in self.database.get_bar_overview():
                if ov.interval == interval:
                    overview_map[(ov.symbol, ov.exchange)] = ov

        for idx, (symbol, exchange) in enumerate(symbols):
            if self._is_cancelled(task_id):
                status = DownloadTaskStatus.CANCELLED
                self._emit_progress(DownloadProgress(
                    task_id=task_id, phase="已取消", total_symbols=total,
                    completed_symbols=success, failed_symbols=failed,
                    skipped_symbols=skipped, current_symbol=symbol,
                    status=status,
                ))
                self._finish_task(task_id)
                return DownloadTaskResult(
                    task_id=task_id, universe=universe.name, interval=interval,
                    start=start, end=end, mode=mode, total=total,
                    success=success, failed=failed, skipped=skipped,
                    failed_symbols=failed_symbols, status=status,
                )

            vt_symbol: str = f"{symbol}.{exchange.value}"

            # APPEND 模式：已覆盖到 end 的跳过
            if mode == DownloadMode.APPEND and failed_only is None:
                ov = overview_map.get((symbol, exchange))
                if ov and ov.end and ov.end >= end:
                    skipped += 1
                    self._emit_progress(DownloadProgress(
                        task_id=task_id, phase="跳过已覆盖", total_symbols=total,
                        completed_symbols=success, failed_symbols=failed,
                        skipped_symbols=skipped, current_symbol=vt_symbol,
                        status=DownloadTaskStatus.RUNNING,
                    ))
                    continue

            # 确定本次下载起点（APPEND 从已有结束时间之后，否则用 start）
            dl_start: datetime = start
            if mode == DownloadMode.APPEND and failed_only is None:
                ov = overview_map.get((symbol, exchange))
                if ov and ov.end:
                    dl_start = ov.end

            try:
                count: int = self.download_bar_data(
                    symbol, exchange, interval, dl_start, output, end
                )
                if count > 0:
                    success += 1
                else:
                    # 空数据：数据源无该标的该区间数据，记为跳过而非失败
                    skipped += 1
            except Exception as ex:  # noqa: BLE001 - 单标的失败不阻断整批
                failed += 1
                failed_symbols.append(vt_symbol)
                output(f"下载 {vt_symbol} {interval.value} 失败：{ex}")

            self._emit_progress(DownloadProgress(
                task_id=task_id, phase="下载中", total_symbols=total,
                completed_symbols=success, failed_symbols=failed,
                skipped_symbols=skipped, current_symbol=vt_symbol,
                status=DownloadTaskStatus.RUNNING,
            ))

        status: DownloadTaskStatus = DownloadTaskStatus.COMPLETED
        self._emit_progress(DownloadProgress(
            task_id=task_id, phase="完成", total_symbols=total,
            completed_symbols=success, failed_symbols=failed,
            skipped_symbols=skipped, current_symbol="",
            status=status,
        ))
        self._finish_task(task_id)
        return DownloadTaskResult(
            task_id=task_id, universe=universe.name, interval=interval,
            start=start, end=end, mode=mode, total=total,
            success=success, failed=failed, skipped=skipped,
            failed_symbols=failed_symbols, status=status,
        )

    def download_universe_tick_data(
        self,
        universe: DataUniverse,
        start: datetime,
        end: datetime,
        output: Callable,
        task_id: str = "",
        failed_only: list[str] | None = None,
    ) -> DownloadTaskResult:
        """批量下载 universe 中所有标的的 Tick 并写入 DuckDB。"""
        import uuid

        task_id = task_id or str(uuid.uuid4())
        self._register_task(task_id)

        if failed_only is not None:
            symbols = [self._parse_vt_symbol(s) for s in failed_only]
        else:
            symbols = self.get_universe_symbols(universe)

        total: int = len(symbols)
        success: int = 0
        failed: int = 0
        skipped: int = 0
        failed_symbols: list[str] = []

        self._emit_progress(DownloadProgress(
            task_id=task_id, phase="开始批量下载 Tick", total_symbols=total,
            completed_symbols=0, failed_symbols=0, skipped_symbols=0,
            current_symbol="", status=DownloadTaskStatus.RUNNING,
        ))

        for idx, (symbol, exchange) in enumerate(symbols):
            if self._is_cancelled(task_id):
                status = DownloadTaskStatus.CANCELLED
                self._finish_task(task_id)
                return DownloadTaskResult(
                    task_id=task_id, universe=universe.name, interval=None,
                    start=start, end=end, mode=DownloadMode.APPEND, total=total,
                    success=success, failed=failed, skipped=skipped,
                    failed_symbols=failed_symbols, status=status,
                )

            vt_symbol: str = f"{symbol}.{exchange.value}"
            try:
                count: int = self.download_tick_data(
                    symbol, exchange, start, output, end
                )
                if count > 0:
                    success += 1
                else:
                    skipped += 1
            except Exception as ex:  # noqa: BLE001
                failed += 1
                failed_symbols.append(vt_symbol)
                output(f"下载 {vt_symbol} Tick 失败：{ex}")

            self._emit_progress(DownloadProgress(
                task_id=task_id, phase="下载 Tick 中", total_symbols=total,
                completed_symbols=success, failed_symbols=failed,
                skipped_symbols=skipped, current_symbol=vt_symbol,
                status=DownloadTaskStatus.RUNNING,
            ))

        status = DownloadTaskStatus.COMPLETED
        self._emit_progress(DownloadProgress(
            task_id=task_id, phase="完成", total_symbols=total,
            completed_symbols=success, failed_symbols=failed,
            skipped_symbols=skipped, current_symbol="",
            status=status,
        ))
        self._finish_task(task_id)
        return DownloadTaskResult(
            task_id=task_id, universe=universe.name, interval=None,
            start=start, end=end, mode=DownloadMode.APPEND, total=total,
            success=success, failed=failed, skipped=skipped,
            failed_symbols=failed_symbols, status=status,
        )

