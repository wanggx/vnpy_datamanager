"""每日定时下载任务对话框（对应阶段一原型 stage1-scheduled-download-vnpy）。

配置每天 04:00 批量下载 1m/1d，查看任务状态、立即执行、查看任务记录。
"""
from datetime import datetime

from vnpy.trader.ui import QtWidgets, QtCore
from vnpy.trader.database import DB_TZ

from ..engine import ManagerEngine
from ..scheduler import (
    ScheduledDownloadService, ScheduledDownloadConfig, ScheduledDownloadStatus,
)


class ScheduledDownloadDialog(QtWidgets.QDialog):
    """每日定时下载任务配置与状态对话框。"""

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
        """立即执行一次定时下载。"""
        self.status_label.setText("正在执行...")
        QtWidgets.QApplication.processEvents()
        status: ScheduledDownloadStatus = self.service.run_now()
        self._render_status(status)
        QtWidgets.QMessageBox.information(
            self, "执行完成",
            f"标的总数：{status.total_symbols}\n成功：{status.success_symbols}\n"
            f"失败：{status.failed_symbols}\n覆盖率：{status.coverage_ratio * 100:.1f}%",
        )

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
