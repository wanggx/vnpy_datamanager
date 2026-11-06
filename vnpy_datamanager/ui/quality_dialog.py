"""数据完整性检查对话框（对应阶段一原型 stage1-data-quality-vnpy-v2）。

独立对话框，后台任务执行检查，两层进度，总览/按交易日/按标的三视图，
支持取消、导出报告、批量修复缺口。
"""
from datetime import datetime, timedelta
from typing import cast

from vnpy.trader.ui import QtWidgets, QtCore
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ

from ..engine import ManagerEngine, DataUniverse, DownloadMode
from ..quality import (
    DataQualityService, DataQualityReport, UniverseQualityReport,
    QualityStatus, expected_minutes_for_day, trading_days_in_range,
)


class DataQualityDialog(QtWidgets.QDialog):
    """数据完整性检查对话框。"""

    UNIVERSE_OPTIONS: list[str] = ["沪深全市场", "指数成分", "自选股票池"]

    def __init__(
        self,
        engine: ManagerEngine,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.engine: ManagerEngine = engine
        self.service: DataQualityService = DataQualityService(engine.database)

        self.setWindowTitle("数据完整性检查")
        self.setMinimumWidth(900)
        self.setMinimumHeight(600)

        self._report: UniverseQualityReport | None = None
        self._cancelled: bool = False

        self._init_ui()

    def _init_ui(self) -> None:
        """搭建顶部条件栏、进度面板、结果区。"""
        # 顶部条件栏
        self.universe_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.universe_combo.addItems(self.UNIVERSE_OPTIONS)

        self.interval_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for iv in [Interval.MINUTE, Interval.HOUR, Interval.DAILY]:
            self.interval_combo.addItem(str(iv.name), iv)

        end_dt: datetime = datetime.now(DB_TZ)
        start_dt: datetime = end_dt - timedelta(days=30)
        self.start_date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit(
            QtCore.QDate(start_dt.year, start_dt.month, start_dt.day)
        )
        self.end_date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit(
            QtCore.QDate(end_dt.year, end_dt.month, end_dt.day)
        )

        check_button: QtWidgets.QPushButton = QtWidgets.QPushButton("开始检查")
        check_button.clicked.connect(self._run_check)

        top_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        top_row.addWidget(QtWidgets.QLabel("数据集合"))
        top_row.addWidget(self.universe_combo)
        top_row.addWidget(QtWidgets.QLabel("周期"))
        top_row.addWidget(self.interval_combo)
        top_row.addWidget(QtWidgets.QLabel("开始"))
        top_row.addWidget(self.start_date_edit)
        top_row.addWidget(QtWidgets.QLabel("结束"))
        top_row.addWidget(self.end_date_edit)
        top_row.addStretch()
        top_row.addWidget(check_button)

        # 进度面板
        self.phase_label: QtWidgets.QLabel = QtWidgets.QLabel("尚未检查")
        self.progress_bar: QtWidgets.QProgressBar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.cancel_button: QtWidgets.QPushButton = QtWidgets.QPushButton("取消检查")
        self.cancel_button.clicked.connect(self._cancel)

        progress_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        progress_row.addWidget(self.phase_label, 1)
        progress_row.addWidget(self.progress_bar, 2)
        progress_row.addWidget(self.cancel_button)

        # 结果区：三 Tab
        self.tab: QtWidgets.QTabWidget = QtWidgets.QTabWidget()
        self.tab.addTab(self._build_overview_tab(), "总览")
        self.tab.addTab(self._build_by_day_tab(), "按交易日")
        self.tab.addTab(self._build_by_symbol_tab(), "按标的")

        # 底部操作
        export_button: QtWidgets.QPushButton = QtWidgets.QPushButton("导出报告")
        export_button.clicked.connect(self._export_report)
        repair_button: QtWidgets.QPushButton = QtWidgets.QPushButton("批量修复缺口")
        repair_button.clicked.connect(self._repair_gaps)

        bottom_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        bottom_row.addStretch()
        bottom_row.addWidget(export_button)
        bottom_row.addWidget(repair_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(top_row)
        vbox.addLayout(progress_row)
        vbox.addWidget(self.tab, 1)
        vbox.addLayout(bottom_row)
        self.setLayout(vbox)

    # -- 三视图表格 -----------------------------------------------------

    def _build_overview_tab(self) -> QtWidgets.QWidget:
        """总览：交易日总数、完整/缺失/异常、覆盖率。"""
        w: QtWidgets.QWidget = QtWidgets.QWidget()
        self.overview_label: QtWidgets.QLabel = QtWidgets.QLabel("尚未检查")
        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(self.overview_label)
        vbox.addStretch()
        w.setLayout(vbox)
        return w

    def _build_by_day_tab(self) -> QtWidgets.QWidget:
        """按交易日：交易日/期望标的数/实际标的数/缺失标的/分钟缺口/状态/操作。"""
        w: QtWidgets.QWidget = QtWidgets.QWidget()
        labels: list[str] = ["交易日", "期望标的数", "实际标的数", "缺失标的", "分钟缺口", "状态", "操作"]
        self.day_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.day_table.setColumnCount(len(labels))
        self.day_table.setHorizontalHeaderLabels(labels)
        self.day_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(self.day_table)
        w.setLayout(vbox)
        return w

    def _build_by_symbol_tab(self) -> QtWidgets.QWidget:
        """按标的：标的/期望Bar/实际Bar/缺失区间/状态/操作。"""
        w: QtWidgets.QWidget = QtWidgets.QWidget()
        labels: list[str] = ["标的", "期望Bar", "实际Bar", "缺失区间", "状态", "操作"]
        self.symbol_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.symbol_table.setColumnCount(len(labels))
        self.symbol_table.setHorizontalHeaderLabels(labels)
        self.symbol_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(self.symbol_table)
        w.setLayout(vbox)
        return w

    # -- 检查执行 ------------------------------------------------------

    def _run_check(self) -> None:
        """执行检查（第一版在当前线程同步执行；长任务可后续改后台 worker）。"""
        self._cancelled = False
        self.phase_label.setText("检查中...")
        self.progress_bar.setValue(0)
        QtWidgets.QApplication.processEvents()

        interval: Interval = Interval(self.interval_combo.currentData())
        start_date: QtCore.QDate = self.start_date_edit.date()
        end_date: QtCore.QDate = self.end_date_edit.date()
        start: datetime = datetime(start_date.year(), start_date.month(), start_date.day(), tzinfo=DB_TZ)
        end: datetime = datetime(end_date.year(), end_date.month(), end_date.day(), 23, 59, 59, tzinfo=DB_TZ)

        # 解析标的
        universe: DataUniverse = DataUniverse(
            name=self.universe_combo.currentText(), source="xtquant"
        )
        symbols: list = self.engine.get_universe_symbols(universe)
        if not symbols:
            self.phase_label.setText("未获取到标的（大 QMT 服务可能未启动）")
            return

        # 逐标的检查，更新进度
        report: UniverseQualityReport = UniverseQualityReport(
            universe=universe.name, interval=interval, start=start, end=end,
        )
        report.total_symbols = len(symbols)
        total_expected: int = 0
        total_actual: int = 0

        for idx, (symbol, exchange) in enumerate(symbols):
            if self._cancelled:
                self.phase_label.setText("已取消")
                break
            sr: DataQualityReport = self.service.check_bar_data(
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

            # 进度
            pct: int = int((idx + 1) / report.total_symbols * 100)
            self.progress_bar.setValue(pct)
            self.phase_label.setText(
                f"检查中 · {idx + 1}/{report.total_symbols} · 当前 {symbol}.{exchange.value}"
            )
            QtWidgets.QApplication.processEvents()

        report.overall_coverage = total_actual / total_expected if total_expected else 0.0
        if report.fail_count > 0 and report.overall_coverage < self.service.FAIL_COVERAGE_THRESHOLD:
            report.status = QualityStatus.FAIL
        elif report.warn_count > 0:
            report.status = QualityStatus.WARN
        else:
            report.status = QualityStatus.PASS

        self._report = report
        self._render_report(report)
        self.phase_label.setText("检查完成")
        self.progress_bar.setValue(100)

    def _cancel(self) -> None:
        """请求取消检查。"""
        self._cancelled = True

    def _render_report(self, report: UniverseQualityReport) -> None:
        """把报告渲染到三个 Tab。"""
        # 总览
        self.overview_label.setText(
            f"标的总数：{report.total_symbols}  通过：{report.pass_count}  "
            f"警告：{report.warn_count}  失败：{report.fail_count}  "
            f"总体覆盖率：{report.overall_coverage * 100:.1f}%  状态：{report.status.value}"
        )

        # 按标的
        self.symbol_table.setRowCount(0)
        for sr in report.symbol_reports:
            row: int = self.symbol_table.rowCount()
            self.symbol_table.insertRow(row)
            self.symbol_table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"{sr.symbol}.{sr.exchange.value}"))
            self.symbol_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(sr.expected_count)))
            self.symbol_table.setItem(row, 2, QtWidgets.QTableWidgetItem(str(sr.actual_count)))
            missing_desc: str = "; ".join(
                f"{mr.start.strftime('%H:%M')}-{mr.end.strftime('%H:%M')}({mr.count})"
                for mr in sr.missing_ranges[:3]
            ) + ("..." if len(sr.missing_ranges) > 3 else "")
            self.symbol_table.setItem(row, 3, QtWidgets.QTableWidgetItem(missing_desc))
            status_item: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(sr.status.value)
            status_item.setForeground(self._status_color(sr.status))
            self.symbol_table.setItem(row, 4, status_item)

        # 按交易日聚合（第一版按标的报告里的交易日聚合）
        self.day_table.setRowCount(0)
        day_map: dict[str, dict] = {}
        for sr in report.symbol_reports:
            if not sr.missing_ranges:
                continue
            for mr in sr.missing_ranges:
                day_key: str = mr.start.strftime("%Y-%m-%d")
                d = day_map.setdefault(day_key, {"expected": 0, "actual": 0, "missing_symbols": set()})
                d["missing_symbols"].add(f"{sr.symbol}.{sr.exchange.value}")
        for day_key in sorted(day_map.keys()):
            row = self.day_table.rowCount()
            self.day_table.insertRow(row)
            self.day_table.setItem(row, 0, QtWidgets.QTableWidgetItem(day_key))
            self.day_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(report.total_symbols)))
            self.day_table.setItem(row, 2, QtWidgets.QTableWidgetItem(
                str(report.total_symbols - len(day_map[day_key]["missing_symbols"]))
            ))
            self.day_table.setItem(row, 3, QtWidgets.QTableWidgetItem(str(len(day_map[day_key]["missing_symbols"]))))
            self.day_table.setItem(row, 4, QtWidgets.QTableWidgetItem("-"))
            status = QualityStatus.WARN if day_map[day_key]["missing_symbols"] else QualityStatus.PASS
            si = QtWidgets.QTableWidgetItem(status.value)
            si.setForeground(self._status_color(status))
            self.day_table.setItem(row, 5, si)

    @staticmethod
    def _status_color(status: QualityStatus) -> 'QtCore.QBrush':
        """状态颜色：PASS 绿、WARN 橙、FAIL 红。"""
        from vnpy.trader.ui import QtGui
        color_map: dict = {
            QualityStatus.PASS: QtGui.QColor("#2d9465"),
            QualityStatus.WARN: QtGui.QColor("#ba8136"),
            QualityStatus.FAIL: QtGui.QColor("#b84d4d"),
        }
        return QtGui.QBrush(color_map.get(status, QtGui.QColor("#c1cbd3")))

    def _export_report(self) -> None:
        """导出报告为 CSV。"""
        if not self._report:
            QtWidgets.QMessageBox.information(self, "导出", "尚无报告可导出")
            return
        from vnpy.trader.utility import get_file_path
        path = get_file_path("quality_report.csv")
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer: csv.writer = csv.writer(f)
            writer.writerow(["标的", "期望", "实际", "重复", "OHLC错误", "状态"])
            for sr in self._report.symbol_reports:
                writer.writerow([
                    f"{sr.symbol}.{sr.exchange.value}", sr.expected_count,
                    sr.actual_count, sr.duplicate_count, sr.ohlc_error_count,
                    sr.status.value,
                ])
        QtWidgets.QMessageBox.information(self, "导出", f"报告已导出到 {path}")

    def _repair_gaps(self) -> None:
        """批量修复缺口：对失败/缺口标的重新下载。"""
        if not self._report or not self._report.failed_symbols:
            QtWidgets.QMessageBox.information(self, "修复", "无失败标的需修复")
            return
        interval: Interval = Interval(self.interval_combo.currentData())
        start_date: QtCore.QDate = self.start_date_edit.date()
        end_date: QtCore.QDate = self.end_date_edit.date()
        start: datetime = datetime(start_date.year(), start_date.month(), start_date.day(), tzinfo=DB_TZ)
        end: datetime = datetime(end_date.year(), end_date.month(), end_date.day(), 23, 59, 59, tzinfo=DB_TZ)

        universe: DataUniverse = DataUniverse(
            name=self.universe_combo.currentText(), source="xtquant"
        )
        result = self.engine.download_universe_bar_data(
            universe, interval, start, end,
            lambda msg: self.engine.write_log(msg, source="QualityRepair"),
            mode=DownloadMode.APPEND,
            failed_only=self._report.failed_symbols,
        )
        QtWidgets.QMessageBox.information(
            self, "修复完成",
            f"重试 {result.total} 个失败标的：成功 {result.success}，仍失败 {result.failed}",
        )
