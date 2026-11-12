"""每日定时下载任务对话框（对应阶段一原型 stage1-scheduled-download-vnpy）。

配置每天 04:00 批量下载 1m/1d，查看任务状态、立即执行、查看任务记录。
「立即执行」在后台线程跑，主线程用进度对话框显示逐标的进度，界面不卡。
"""
import threading
import time
import uuid
from datetime import datetime

from vnpy.event import Event, EventEngine
from vnpy.trader.ui import QtWidgets, QtCore
from vnpy.trader.database import DB_TZ

from ..engine import (
    ManagerEngine, EVENT_DM_DOWNLOAD_PROGRESS, DownloadProgress,
    DownloadTaskStatus,
)
from ..scheduler import (
    ScheduledDownloadService, ScheduledDownloadConfig, ScheduledDownloadStatus,
)


class ScheduledDownloadDialog(QtWidgets.QDialog):
    """每日定时下载任务配置与状态对话框。"""

    # 后台执行线程 → 主线程（Qt 自动排队到 GUI 线程）
    signal_progress: QtCore.Signal = QtCore.Signal(Event)
    signal_finished: QtCore.Signal = QtCore.Signal()

    def __init__(
        self,
        engine: ManagerEngine,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.engine: ManagerEngine = engine
        self.service: ScheduledDownloadService = engine.scheduler

        self.setWindowTitle("定时下载任务")
        self.setMinimumWidth(700)

        self._run_task_id: str = ""
        self._run_result: ScheduledDownloadStatus | None = None
        self._run_error: str = ""

        self._init_ui()
        self._load_status()

    def _init_ui(self) -> None:
        """任务设置 + 任务状态两块。"""
        cfg: ScheduledDownloadConfig = self.service.config

        # 任务设置
        self.enabled_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("启用自动下载")
        self.enabled_check.setChecked(cfg.enabled)

        self.run_time_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit(cfg.run_time)

        self.timezone_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.timezone_combo.addItem("Asia/Shanghai")
        self.timezone_combo.setCurrentText(cfg.timezone)

        self.universe_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.universe_combo.addItem("沪深全市场")
        self.universe_combo.setCurrentText(cfg.universe)

        self.interval_1m_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("1m")
        self.interval_1m_check.setChecked("1m" in cfg.intervals)
        self.interval_1d_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("1d")
        self.interval_1d_check.setChecked("1d" in cfg.intervals)

        self.repair_days_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for n in [1, 2, 3, 5]:
            self.repair_days_combo.addItem(f"最近 {n} 个交易日", n)
        self.repair_days_combo.setCurrentIndex(
            max(0, self.repair_days_combo.findData(cfg.repair_days))
        )

        self.retry_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit(str(cfg.max_retries))

        save_button: QtWidgets.QPushButton = QtWidgets.QPushButton("保存配置")
        save_button.clicked.connect(self._save_config)

        # 任务状态
        self.status_label: QtWidgets.QLabel = QtWidgets.QLabel("尚未执行")
        self.status_label.setWordWrap(True)

        run_now_button: QtWidgets.QPushButton = QtWidgets.QPushButton("立即执行一次")
        run_now_button.clicked.connect(self._run_now)
        record_button: QtWidgets.QPushButton = QtWidgets.QPushButton("查看任务记录")
        record_button.clicked.connect(self._show_records)

        # 布局
        settings_group: QtWidgets.QGroupBox = QtWidgets.QGroupBox("任务设置")
        s_form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        s_form.addRow(self.enabled_check)
        s_form.addRow("执行时间", self.run_time_edit)
        s_form.addRow("时区", self.timezone_combo)
        s_form.addRow("数据集合", self.universe_combo)
        interval_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        interval_row.addWidget(self.interval_1m_check)
        interval_row.addWidget(self.interval_1d_check)
        interval_row.addStretch()
        s_form.addRow("周期", interval_row)
        s_form.addRow("追赶范围", self.repair_days_combo)
        s_form.addRow("失败重试次数", self.retry_edit)
        s_form.addRow(save_button)
        settings_group.setLayout(s_form)

        status_group: QtWidgets.QGroupBox = QtWidgets.QGroupBox("任务状态")
        st_vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        st_vbox.addWidget(self.status_label)
        st_btn_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        st_btn_row.addWidget(run_now_button)
        st_btn_row.addWidget(record_button)
        st_btn_row.addStretch()
        st_vbox.addLayout(st_btn_row)
        status_group.setLayout(st_vbox)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(settings_group)
        vbox.addWidget(status_group)
        self.setLayout(vbox)

    def _save_config(self) -> None:
        """保存配置到 SETTINGS。"""
        intervals: list[str] = []
        if self.interval_1m_check.isChecked():
            intervals.append("1m")
        if self.interval_1d_check.isChecked():
            intervals.append("1d")
        if not intervals:
            QtWidgets.QMessageBox.warning(self, "提示", "请至少勾选一个周期（1m / 1d）")
            return
        cfg: ScheduledDownloadConfig = ScheduledDownloadConfig(
            enabled=self.enabled_check.isChecked(),
            run_time=self.run_time_edit.text().strip() or "04:00",
            timezone=self.timezone_combo.currentText(),
            universe=self.universe_combo.currentText(),
            intervals=intervals,
            repair_days=self.repair_days_combo.currentData(),
            max_retries=int(self.retry_edit.text() or "3"),
        )
        self.service.save_config(cfg)
        QtWidgets.QMessageBox.information(self, "保存", "配置已保存")

    def _run_now(self) -> None:
        """立即执行一次定时下载：后台线程跑，主线程用进度对话框显示进度。"""
        task_id: str = str(uuid.uuid4())
        self._run_task_id = task_id
        self._run_result = None
        self._run_error = ""

        event_engine: EventEngine = self.engine.event_engine
        self.signal_progress.connect(self._on_download_progress)
        event_engine.register(EVENT_DM_DOWNLOAD_PROGRESS, self.signal_progress.emit)

        # 进度：0..0 表示总量未知，收到第一条事件后按 total_symbols 设置上限
        self._progress_started_at = time.monotonic()
        self._symbol_started_at = self._progress_started_at
        self._progress_phase = "准备执行"
        self._progress_symbol = ""
        self._progress_interval = ""
        self._progress_counts: tuple[int, int, int, int] = (0, 0, 0, 0)

        self._progress_dialog = QtWidgets.QProgressDialog(
            "准备执行...", "", 0, 0, self
        )
        self._progress_dialog.setWindowTitle("定时下载任务")
        self._progress_dialog.setMinimumWidth(460)
        self._progress_dialog.setCancelButton(None)   # 调度任务不支持中断
        # 一个任务会跑多个周期（1m/1d），周期结束时 value==maximum 不能自动关闭
        self._progress_dialog.setAutoClose(False)
        self._progress_dialog.setAutoReset(False)
        self._progress_dialog.setWindowModality(
            QtCore.Qt.WindowModality.WindowModal
        )
        self._progress_dialog.setValue(0)

        self._progress_timer: QtCore.QTimer = QtCore.QTimer(self)
        self._progress_timer.timeout.connect(self._tick_progress)
        self._progress_timer.start(1000)

        self.signal_finished.connect(self._on_run_finished)

        threading.Thread(
            target=self._run_schedule_thread, args=(task_id,), daemon=True,
        ).start()

        self._progress_dialog.exec_()

        self._progress_timer.stop()
        try:
            self.signal_progress.disconnect(self._on_download_progress)
            self.signal_finished.disconnect(self._on_run_finished)
        except (TypeError, RuntimeError):
            pass

        if self._run_error:
            QtWidgets.QMessageBox.warning(
                self, "执行失败", f"定时任务执行异常：{self._run_error}"
            )
            return

        status: ScheduledDownloadStatus | None = self._run_result
        if status is not None:
            self._render_status(status)
            QtWidgets.QMessageBox.information(
                self, "执行完成",
                f"标的总数：{status.total_symbols}\n成功：{status.success_symbols}\n"
                f"失败：{status.failed_symbols}\n"
                f"覆盖率：{status.coverage_ratio * 100:.1f}%",
            )

    def _run_schedule_thread(self, task_id: str) -> None:
        """后台线程：跑一次调度任务，完成后通知主线程关闭进度框。"""
        try:
            self._run_result = self.service.run_now(task_id=task_id)
        except Exception as ex:  # noqa: BLE001 - 后台线程异常不崩 GUI
            self._run_error = str(ex)
            self.engine.write_log(f"定时任务执行异常：{ex}", source="Schedule")
        finally:
            self.engine.event_engine.unregister(
                EVENT_DM_DOWNLOAD_PROGRESS, self.signal_progress.emit
            )
            self.signal_finished.emit()

    def _on_download_progress(self, event: Event) -> None:
        """主线程槽：只处理本任务（task_id 前缀匹配）的下载进度。"""
        p = event.data
        if not isinstance(p, DownloadProgress):
            return
        if not p.task_id.startswith(self._run_task_id):
            return

        dialog: QtWidgets.QProgressDialog = self._progress_dialog
        total: int = p.total_symbols
        done: int = p.completed_symbols + p.failed_symbols + p.skipped_symbols
        if total > 0:
            dialog.setMaximum(total)
            # 每换一个周期，进度条重新起算
            if done <= total:
                dialog.setValue(done)

        if p.current_symbol != self._progress_symbol:
            self._symbol_started_at = time.monotonic()
        self._progress_phase = p.phase
        self._progress_symbol = p.current_symbol
        self._progress_interval = p.task_id.replace(f"{self._run_task_id}_", "")
        self._progress_counts = (
            p.completed_symbols, p.failed_symbols, p.skipped_symbols, total,
        )
        if p.status != DownloadTaskStatus.RUNNING and total > 0:
            # 该周期收尾：保持进度满格，但对话框由 _on_run_finished 统一关闭
            dialog.setMaximum(total)
            dialog.setValue(total)
        dialog.setLabelText(self._progress_label())

    def _progress_label(self) -> str:
        """进度文案：周期 + 阶段/标的 + 计数 + 已等待时长。"""
        success, failed, skipped, total = self._progress_counts
        waited: int = int(time.monotonic() - self._progress_started_at)
        symbol_waited: int = int(time.monotonic() - self._symbol_started_at)
        interval: str = self._progress_interval or "—"
        text: str = (
            f"[{interval}] {self._progress_phase}：{self._progress_symbol or '—'}\n"
            f"成功 {success}  失败 {failed}  跳过 {skipped}  /  总数 {total}\n"
            f"累计 {waited}s · 当前标的 {symbol_waited}s"
        )
        if symbol_waited >= 30:
            text += "\n数据源响应慢：单标的超时会跳过后继续"
        return text

    def _tick_progress(self) -> None:
        """每秒刷新一次，卡住时也能看到停在哪个标的、等了多久。"""
        if hasattr(self, "_progress_dialog"):
            self._progress_dialog.setLabelText(self._progress_label())

    def _on_run_finished(self) -> None:
        """任务结束（后台线程发来）：停掉心跳并关闭进度框，主线程继续收尾。"""
        if hasattr(self, "_progress_timer"):
            self._progress_timer.stop()
        if hasattr(self, "_progress_dialog"):
            # autoClose/autoReset 都关了（多周期任务），所以用 close() 结束 exec_()
            self._progress_dialog.close()

    def _load_status(self) -> None:
        """加载最近一次状态。"""
        status: ScheduledDownloadStatus | None = self.service.get_status()
        if status:
            self._render_status(status)
        else:
            self.status_label.setText("尚未执行过定时任务。")

    def _render_status(self, status: ScheduledDownloadStatus) -> None:
        """渲染状态文本。"""
        text: str = (
            f"计划时间：{status.planned_at:%Y-%m-%d %H:%M:%S}\n"
            f"目标交易日：{status.target_trading_day}\n"
            f"状态：{status.status}\n"
            f"1m/1d 合计：成功 {status.success_symbols} / 失败 {status.failed_symbols} / "
            f"总数 {status.total_symbols}\n覆盖率：{status.coverage_ratio * 100:.1f}%\n"
            f"重试次数：{status.retry_count}\n"
            f"错误：{status.error_summary or '无'}"
        )
        self.status_label.setText(text)

    def _show_records(self) -> None:
        """查看 DuckDB download_task 表里的任务记录。"""
        conn = getattr(self.engine.database, "conn", None)
        if conn is None:
            QtWidgets.QMessageBox.information(self, "任务记录", "数据库不支持记录查看")
            return
        try:
            rows = conn.execute(
                "SELECT planned_at, interval, target_trading_day, "
                "total_symbols, success_symbols, failed_symbols, status "
                "FROM download_task ORDER BY planned_at DESC LIMIT 20"
            ).fetchall()
        except Exception as ex:
            QtWidgets.QMessageBox.information(self, "任务记录", f"无记录或读取失败：{ex}")
            return
        if not rows:
            QtWidgets.QMessageBox.information(self, "任务记录", "暂无任务记录")
            return
        msg: str = "\n".join(
            f"{r[0]}  {r[1]}  {r[2]}  成功{r[4]}/{r[3]}  失败{r[5]}  {r[6]}"
            for r in rows
        )
        QtWidgets.QMessageBox.information(self, "最近 20 条任务记录", msg)
