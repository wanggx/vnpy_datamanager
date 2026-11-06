"""数据管理界面组件。"""
import threading
import uuid
from collections.abc import Sequence
from functools import partial
from datetime import datetime, timedelta
from typing import cast

from vnpy.event import Event, EventEngine
from vnpy.trader.ui import QtWidgets, QtCore
from vnpy.trader.engine import MainEngine
from vnpy.trader.constant import Interval, Exchange
from vnpy.trader.object import BarData
from vnpy.trader.database import DB_TZ
from vnpy.trader.utility import available_timezones

from ..engine import (
    APP_NAME, ManagerEngine, BarOverview, DataUniverse, DownloadMode,
    EVENT_DM_DOWNLOAD_PROGRESS, DownloadProgress, DownloadTaskStatus,
    DownloadTaskResult,
)


INTERVAL_NAME_MAP: dict[Interval, str] = {
    Interval.MINUTE: "分钟线",
    Interval.HOUR: "小时线",
    Interval.DAILY: "日线",
}


class ManagerWidget(QtWidgets.QWidget):
    """数据管理主界面。"""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """取得数据管理引擎并初始化界面。"""
        super().__init__()

        self.engine: ManagerEngine = cast(ManagerEngine, main_engine.get_engine(APP_NAME))

        self.init_ui()

    def init_ui(self) -> None:
        """搭建数据树、K 线表和刷新、导入、更新、下载按钮。"""
        self.setWindowTitle("数据管理")

        self.init_tree()
        self.init_table()

        refresh_button: QtWidgets.QPushButton = QtWidgets.QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh_tree)

        import_button: QtWidgets.QPushButton = QtWidgets.QPushButton("导入数据")
        import_button.clicked.connect(self.import_data)

        update_button: QtWidgets.QPushButton = QtWidgets.QPushButton("更新数据")
        update_button.clicked.connect(self.update_data)

        download_button: QtWidgets.QPushButton = QtWidgets.QPushButton("下载数据")
        download_button.clicked.connect(self.download_data)

        quality_button: QtWidgets.QPushButton = QtWidgets.QPushButton("检查数据")
        quality_button.clicked.connect(self.show_quality_dialog)

        schedule_button: QtWidgets.QPushButton = QtWidgets.QPushButton("定时任务")
        schedule_button.clicked.connect(self.show_schedule_dialog)

        hbox1: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox1.addWidget(refresh_button)
        hbox1.addStretch()
        hbox1.addWidget(import_button)
        hbox1.addWidget(update_button)
        hbox1.addWidget(download_button)
        hbox1.addWidget(quality_button)
        hbox1.addWidget(schedule_button)

        hbox2: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        hbox2.addWidget(self.tree)
        hbox2.addWidget(self.table)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(hbox1)
        vbox.addLayout(hbox2)

        self.setLayout(vbox)

    def init_tree(self) -> None:
        """创建数据概况树及其表头。"""
        labels: list = [
            "数据",
            "本地代码",
            "代码",
            "交易所",
            "数据量",
            "开始时间",
            "结束时间",
            "",
            "",
            ""
        ]

        self.tree: QtWidgets.QTreeWidget = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(len(labels))
        self.tree.setHeaderLabels(labels)

    def init_table(self) -> None:
        """创建 K 线表格，列宽按内容调整。"""
        labels: list = [
            "时间",
            "开盘价",
            "最高价",
            "最低价",
            "收盘价",
            "成交量",
            "成交额",
            "持仓量"
        ]

        self.table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.table.setColumnCount(len(labels))
        self.table.setHorizontalHeaderLabels(labels)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )

    def refresh_tree(self) -> None:
        """清空概况树，并按周期、交易所和合约重新挂上数据节点。"""
        self.tree.clear()

        # 初始化节点缓存字典
        interval_childs: dict[Interval, QtWidgets.QTreeWidgetItem] = {}
        exchange_childs: dict[tuple[Interval, Exchange], QtWidgets.QTreeWidgetItem] = {}

        # 查询数据汇总，并基于合约代码进行排序
        overviews: list[BarOverview] = self.engine.get_bar_overview()
        overviews.sort(key=lambda x: x.symbol)

        # 添加数据周期节点
        interval: Interval
        for interval in [Interval.MINUTE, Interval.HOUR, Interval.DAILY]:
            interval_child: QtWidgets.QTreeWidgetItem = QtWidgets.QTreeWidgetItem()
            interval_childs[interval] = interval_child

            interval_name: str = INTERVAL_NAME_MAP[interval]
            interval_child.setText(0, interval_name)

        # 遍历添加数据节点
        overview: BarOverview
        for overview in overviews:
            interval = cast(Interval, overview.interval)
            exchange: Exchange = cast(Exchange, overview.exchange)
            start: datetime = cast(datetime, overview.start)
            end: datetime = cast(datetime, overview.end)

            # 获取交易所节点
            key: tuple[Interval, Exchange] = (interval, exchange)
            exchange_child: QtWidgets.QTreeWidgetItem | None = exchange_childs.get(key)

            if not exchange_child:
                interval_child = interval_childs[interval]

                exchange_child = QtWidgets.QTreeWidgetItem(interval_child)
                exchange_child.setText(0, exchange.value)

                exchange_childs[key] = exchange_child

            #  创建数据节点
            item: QtWidgets.QTreeWidgetItem = QtWidgets.QTreeWidgetItem(exchange_child)

            item.setText(1, f"{overview.symbol}.{exchange.value}")
            item.setText(2, overview.symbol)
            item.setText(3, exchange.value)
            item.setText(4, str(overview.count))
            item.setText(5, start.strftime("%Y-%m-%d %H:%M:%S"))
            item.setText(6, end.strftime("%Y-%m-%d %H:%M:%S"))

            output_button: QtWidgets.QPushButton = QtWidgets.QPushButton("导出")
            output_func: partial[None] = partial(
                self.output_data,
                overview.symbol,
                exchange,
                interval,
                start,
                end
            )
            output_button.clicked.connect(output_func)

            show_button: QtWidgets.QPushButton = QtWidgets.QPushButton("查看")
            show_func: partial[None] = partial(
                self.show_data,
                overview.symbol,
                exchange,
                interval,
                start,
                end
            )
            show_button.clicked.connect(show_func)

            delete_button: QtWidgets.QPushButton = QtWidgets.QPushButton("删除")
            delete_func: partial[None] = partial(
                self.delete_data,
                overview.symbol,
                exchange,
                interval
            )
            delete_button.clicked.connect(delete_func)

            self.tree.setItemWidget(item, 7, show_button)
            self.tree.setItemWidget(item, 8, output_button)
            self.tree.setItemWidget(item, 9, delete_button)

        # 展开顶层节点
        self.tree.addTopLevelItems(list(interval_childs.values()))

        for interval_child in interval_childs.values():
            interval_child.setExpanded(True)

    def import_data(self) -> None:
        """确认 CSV 导入参数后写入数据库并提示起止时间与条数，取消对话框则返回。"""
        dialog: ImportDialog = ImportDialog()
        n: int = dialog.exec_()
        if n != dialog.DialogCode.Accepted:
            return

        file_path: str = dialog.file_edit.text()
        symbol: str = dialog.symbol_edit.text()
        exchange: Exchange = dialog.exchange_combo.currentData()
        interval: Interval = dialog.interval_combo.currentData()
        tz_name: str = dialog.tz_combo.currentText()
        datetime_head: str = dialog.datetime_edit.text()
        open_head: str = dialog.open_edit.text()
        low_head: str = dialog.low_edit.text()
        high_head: str = dialog.high_edit.text()
        close_head: str = dialog.close_edit.text()
        volume_head: str = dialog.volume_edit.text()
        turnover_head: str = dialog.turnover_edit.text()
        open_interest_head: str = dialog.open_interest_edit.text()
        datetime_format: str = dialog.format_edit.text()

        start: datetime | None
        end: datetime
        count: int
        start, end, count = self.engine.import_data_from_csv(
            file_path,
            symbol,
            exchange,
            interval,
            tz_name,
            datetime_head,
            open_head,
            high_head,
            low_head,
            close_head,
            volume_head,
            turnover_head,
            open_interest_head,
            datetime_format
        )

        msg: str = f"\
        CSV载入成功\n\
        代码：{symbol}\n\
        交易所：{exchange.value}\n\
        周期：{interval.value}\n\
        起始：{start}\n\
        结束：{end}\n\
        总数量：{count}\n\
        "
        QtWidgets.QMessageBox.information(self, "载入成功！", msg, QtWidgets.QMessageBox.StandardButton.Ok)

    def output_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime
    ) -> None:
        """选择时间区间和 CSV 路径后导出，文件被占用时提示失败。"""
        # Get output date range
        dialog: DateRangeDialog = DateRangeDialog(start, end)
        n: int = dialog.exec_()
        if n != dialog.DialogCode.Accepted:
            return
        start, end = dialog.get_date_range()

        # Get output file path
        path: str
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "导出数据",
            "",
            "CSV(*.csv)"
        )
        if not path:
            return

        result: bool = self.engine.output_data_to_csv(
            path,
            symbol,
            exchange,
            interval,
            start,
            end
        )

        if not result:
            QtWidgets.QMessageBox.warning(
                self,
                "导出失败！",
                "该文件已在其他程序中打开，请关闭相关程序后再尝试导出数据。"
            )

    def show_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime
    ) -> None:
        """选择时间区间后，把 K 线填入表格。"""
        # Get output date range
        dialog: DateRangeDialog = DateRangeDialog(start, end)
        n: int = dialog.exec_()
        if n != dialog.DialogCode.Accepted:
            return
        start, end = dialog.get_date_range()

        bars: list[BarData] = self.engine.load_bar_data(
            symbol,
            exchange,
            interval,
            start,
            end
        )

        self.table.setRowCount(0)
        self.table.setRowCount(len(bars))

        row: int
        bar: BarData
        for row, bar in enumerate(bars):
            self.table.setItem(row, 0, DataCell(bar.datetime.strftime("%Y-%m-%d %H:%M:%S")))
            self.table.setItem(row, 1, DataCell(str(bar.open_price)))
            self.table.setItem(row, 2, DataCell(str(bar.high_price)))
            self.table.setItem(row, 3, DataCell(str(bar.low_price)))
            self.table.setItem(row, 4, DataCell(str(bar.close_price)))
            self.table.setItem(row, 5, DataCell(str(bar.volume)))
            self.table.setItem(row, 6, DataCell(str(bar.turnover)))
            self.table.setItem(row, 7, DataCell(str(bar.open_interest)))

    def delete_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval
    ) -> None:
        """确认后删除该合约和周期的全部 K 线，并提示删除条数。"""
        n: QtWidgets.QMessageBox.StandardButton = QtWidgets.QMessageBox.warning(
            self,
            "删除确认",
            f"请确认是否要删除{symbol} {exchange.value} {interval.value}的全部数据",
            QtWidgets.QMessageBox.StandardButton.Ok,
            QtWidgets.QMessageBox.StandardButton.Cancel
        )

        if n == QtWidgets.QMessageBox.StandardButton.Cancel:
            return

        count: int = self.engine.delete_bar_data(
            symbol,
            exchange,
            interval
        )

        QtWidgets.QMessageBox.information(
            self,
            "删除成功",
            f"已删除{symbol} {exchange.value} {interval.value}共计{count}条数据",
            QtWidgets.QMessageBox.StandardButton.Ok
        )

    def update_data(self) -> None:
        """遍历已有 K 线概况，从各自结束时间继续下载，进度框可取消。"""
        overviews: list[BarOverview] = self.engine.get_bar_overview()
        total: int = len(overviews)
        count: int = 0

        dialog: QtWidgets.QProgressDialog = QtWidgets.QProgressDialog(
            "历史数据更新中",
            "取消",
            0,
            100
        )
        dialog.setWindowTitle("更新进度")
        dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        dialog.setValue(0)

        overview: BarOverview
        for overview in overviews:
            if dialog.wasCanceled():
                break

            self.engine.download_bar_data(
                overview.symbol,
                cast(Exchange, overview.exchange),
                cast(Interval, overview.interval),
                cast(datetime, overview.end),
                self.output
            )
            count += 1
            progress: int = int(round(count / total * 100, 0))
            dialog.setValue(progress)

        dialog.close()

    def download_data(self) -> None:
        """打开历史数据下载对话框。"""
        dialog: DownloadDialog = DownloadDialog(self.engine)
        dialog.exec_()

    def show_quality_dialog(self) -> None:
        """打开数据完整性检查对话框。"""
        from .quality_dialog import DataQualityDialog
        dialog: DataQualityDialog = DataQualityDialog(self.engine, self)
        dialog.exec_()

    def show_schedule_dialog(self) -> None:
        """打开每日定时下载任务对话框。"""
        from .schedule_dialog import ScheduledDownloadDialog
        dialog: ScheduledDownloadDialog = ScheduledDownloadDialog(self.engine, self)
        dialog.exec_()

    def show(self) -> None:
        """最大化显示窗口。"""
        self.showMaximized()

    def output(self, msg: str) -> None:
        """输出下载过程中的日志"""
        QtWidgets.QMessageBox.warning(
            self,
            "数据下载",
            msg,
            QtWidgets.QMessageBox.StandardButton.Ok,
            QtWidgets.QMessageBox.StandardButton.Ok,
        )


class DataCell(QtWidgets.QTableWidgetItem):
    """居中对齐的表格单元格。"""

    def __init__(self, text: str = "") -> None:
        """用给定文本创建居中对齐的单元格。"""
        super().__init__(text)

        self.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)


class DateRangeDialog(QtWidgets.QDialog):
    """选择数据起止时间的对话框。"""

    def __init__(self, start: datetime, end: datetime, parent: QtWidgets.QWidget | None = None) -> None:
        """用起止日期的下一天作为日期框初值。"""
        super().__init__(parent)

        self.setWindowTitle("选择数据区间")

        self.start_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit(
            QtCore.QDate(
                start.year,
                start.month,
                start.day + 1
            )
        )
        self.end_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit(
            QtCore.QDate(
                end.year,
                end.month,
                end.day + 1
            )
        )

        button: QtWidgets.QPushButton = QtWidgets.QPushButton("确定")
        button.clicked.connect(self.accept)

        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        form.addRow("开始时间", self.start_edit)
        form.addRow("结束时间", self.end_edit)
        form.addRow(button)

        self.setLayout(form)

    def get_date_range(self) -> tuple[datetime, datetime]:
        """返回开始时间，以及结束日期的下一天。"""
        start: datetime = cast(datetime, self.start_edit.dateTime().toPython())
        end: datetime = cast(datetime, self.end_edit.dateTime().toPython()) + timedelta(days=1)
        return start, end


class ImportDialog(QtWidgets.QDialog):
    """从 CSV 文件导入数据的对话框。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        """创建 CSV 导入表单，周期列表不含 Tick。"""
        super().__init__()

        self.setWindowTitle("从CSV文件导入数据")
        self.setFixedWidth(300)

        self.setWindowFlags(
            (self.windowFlags() | QtCore.Qt.WindowType.CustomizeWindowHint)
            & ~QtCore.Qt.WindowType.WindowMaximizeButtonHint)

        file_button: QtWidgets.QPushButton = QtWidgets.QPushButton("选择文件")
        file_button.clicked.connect(self.select_file)

        load_button: QtWidgets.QPushButton = QtWidgets.QPushButton("确定")
        load_button.clicked.connect(self.accept)

        self.file_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.symbol_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit()

        self.exchange_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        exchange: Exchange
        for exchange in Exchange:
            self.exchange_combo.addItem(str(exchange.name), exchange)

        self.interval_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        interval: Interval
        for interval in Interval:
            if interval != Interval.TICK:
                self.interval_combo.addItem(str(interval.name), interval)

        self.tz_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.tz_combo.addItems(cast(Sequence[str], available_timezones()))
        self.tz_combo.setCurrentIndex(self.tz_combo.findText("Asia/Shanghai"))

        self.datetime_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("datetime")
        self.open_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("open")
        self.high_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("high")
        self.low_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("low")
        self.close_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("close")
        self.volume_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("volume")
        self.turnover_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("turnover")
        self.open_interest_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("open_interest")

        self.format_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit("%Y-%m-%d %H:%M:%S")

        info_label: QtWidgets.QLabel = QtWidgets.QLabel("合约信息")
        info_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        head_label: QtWidgets.QLabel = QtWidgets.QLabel("表头信息")
        head_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        format_label: QtWidgets.QLabel = QtWidgets.QLabel("格式信息")
        format_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        form.addRow(file_button, self.file_edit)
        form.addRow(QtWidgets.QLabel())
        form.addRow(info_label)
        form.addRow("代码", self.symbol_edit)
        form.addRow("交易所", self.exchange_combo)
        form.addRow("周期", self.interval_combo)
        form.addRow("时区", self.tz_combo)
        form.addRow(QtWidgets.QLabel())
        form.addRow(head_label)
        form.addRow("时间戳", self.datetime_edit)
        form.addRow("开盘价", self.open_edit)
        form.addRow("最高价", self.high_edit)
        form.addRow("最低价", self.low_edit)
        form.addRow("收盘价", self.close_edit)
        form.addRow("成交量", self.volume_edit)
        form.addRow("成交额", self.turnover_edit)
        form.addRow("持仓量", self.open_interest_edit)
        form.addRow(QtWidgets.QLabel())
        form.addRow(format_label)
        form.addRow("时间格式", self.format_edit)
        form.addRow(QtWidgets.QLabel())
        form.addRow(load_button)

        self.setLayout(form)

    def select_file(self) -> None:
        """选择 CSV 文件并填入路径。"""
        result: tuple[str, str] = QtWidgets.QFileDialog.getOpenFileName(
            self, filter="CSV (*.csv)")
        filename: str = result[0]
        if filename:
            self.file_edit.setText(filename)


class DownloadDialog(QtWidgets.QDialog):
    """批量下载历史数据的对话框（对应阶段一原型 stage1-datamanager-vnpy-v3）。

    面向数据集合/股票池批量下载，不要求用户逐个输入标的。保留原单标的下载
    能力：数据集合选"指定标的"时显示代码输入框。
    """

    # 数据集合选项
    UNIVERSE_OPTIONS: list[str] = ["沪深全市场", "指数成分", "自选股票池", "指定标的"]
    # 标的范围
    SCOPE_OPTIONS: list[str] = ["全部标的", "失败标的", "缺口标的"]
    # 下载模式
    MODE_OPTIONS: list[str] = ["追加", "补缺口", "重下"]
    # 进度事件经 Qt 信号跨线程投递回 GUI 主线程（事件引擎线程 emit → 主线程槽）
    signal_progress: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, engine: ManagerEngine, parent: QtWidgets.QWidget | None = None) -> None:
        """创建批量下载表单。"""
        super().__init__(parent)

        self.engine: ManagerEngine = engine

        self.setWindowTitle("批量下载历史数据")
        self.setMinimumWidth(480)

        # 数据集合
        self.universe_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.universe_combo.addItems(self.UNIVERSE_OPTIONS)
        self.universe_combo.currentTextChanged.connect(self._on_universe_changed)

        # 单标的输入（仅"指定标的"时显示）
        self.symbol_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.exchange_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for exchange in Exchange:
            self.exchange_combo.addItem(str(exchange.name), exchange)
        self.symbol_edit.setVisible(False)
        self.exchange_combo.setVisible(False)

        # 标的范围
        self.scope_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.scope_combo.addItems(self.SCOPE_OPTIONS)

        # 周期（按用户决策：1m/1h/1d/tick 四档）
        self.interval_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        for interval in [Interval.MINUTE, Interval.HOUR, Interval.DAILY, Interval.TICK]:
            self.interval_combo.addItem(str(interval.name), interval)

        # 下载模式
        self.mode_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.mode_combo.addItems(self.MODE_OPTIONS)

        # 日期
        end_dt: datetime = datetime.now(DB_TZ)
        start_dt: datetime = end_dt - timedelta(days=365)
        self.start_date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit(
            QtCore.QDate(start_dt.year, start_dt.month, start_dt.day)
        )
        self.end_date_edit: QtWidgets.QDateEdit = QtWidgets.QDateEdit(
            QtCore.QDate(end_dt.year, end_dt.month, end_dt.day)
        )

        # 覆盖信息提示
        self.info_label: QtWidgets.QLabel = QtWidgets.QLabel(
            "选择数据集合后点击\"查询覆盖\"查看标的数量与已有覆盖率。"
        )
        self.info_label.setWordWrap(True)

        # 质量门禁提示
        self.quality_label: QtWidgets.QLabel = QtWidgets.QLabel(
            "数据质量门禁：覆盖率、缺失区间、重复记录、OHLC 异常。"
            "下载完成后自动检查，失败标的可批量重试。"
        )
        self.quality_label.setWordWrap(True)

        # 按钮
        query_button: QtWidgets.QPushButton = QtWidgets.QPushButton("查询覆盖")
        query_button.clicked.connect(self._query_coverage)
        download_button: QtWidgets.QPushButton = QtWidgets.QPushButton("开始下载")
        download_button.clicked.connect(self.download)
        cancel_button: QtWidgets.QPushButton = QtWidgets.QPushButton("取消")
        cancel_button.clicked.connect(self.close)

        # 布局
        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        form.addRow("数据集合", self.universe_combo)
        form.addRow("代码", self.symbol_edit)
        form.addRow("交易所", self.exchange_combo)
        form.addRow("标的范围", self.scope_combo)
        form.addRow("周期", self.interval_combo)
        form.addRow("下载模式", self.mode_combo)
        form.addRow("开始日期", self.start_date_edit)
        form.addRow("结束日期", self.end_date_edit)
        form.addRow(self.info_label)
        form.addRow(self.quality_label)

        btn_row: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        btn_row.addWidget(query_button)
        btn_row.addStretch()
        btn_row.addWidget(download_button)
        btn_row.addWidget(cancel_button)
        form.addRow(btn_row)

        self.setLayout(form)

    def _on_universe_changed(self, text: str) -> None:
        """数据集合切换：仅"指定标的"显示代码/交易所输入。"""
        is_single: bool = (text == "指定标的")
        self.symbol_edit.setVisible(is_single)
        self.exchange_combo.setVisible(is_single)

    def _build_universe(self) -> DataUniverse:
        """根据当前选择构造 DataUniverse。"""
        name: str = self.universe_combo.currentText()
        if name == "指定标的":
            return DataUniverse(
                name=name,
                symbols=[f"{self.symbol_edit.text()}.{self.exchange_combo.currentData().value}"],
                source="manual",
            )
        return DataUniverse(name=name, source="xtquant")

    def _query_coverage(self) -> None:
        """查询股票池标的数量与已有覆盖率。"""
        universe: DataUniverse = self._build_universe()
        if universe.source == "manual":
            self.info_label.setText("指定标的模式：单标的下载。")
            return
        symbols: list = self.engine.get_universe_symbols(universe)
        total: int = len(symbols)
        if total == 0:
            self.info_label.setText("未获取到标的（大 QMT 服务可能未启动）。")
            return
        # 统计已有覆盖
        interval: Interval = Interval(self.interval_combo.currentData())
        overviews: list[BarOverview] = self.engine.get_bar_overview()
        covered: int = sum(
            1 for ov in overviews
            if ov.interval == interval
            and (ov.symbol, ov.exchange) in set(symbols)
        )
        ratio: float = covered / total * 100 if total else 0
        self.info_label.setText(
            f"股票池标的数：{total}，已有覆盖率：{ratio:.1f}%，"
            f"将由数据源自动分批下载，用户不需要逐个输入或点击标的。"
        )

    def download(self) -> None:
        """执行批量下载（后台线程，进度对话框，可取消）。

        Tick 周期走 tick 接口，否则走 bar 接口。下载在后台线程串行逐标的
        调用引擎，引擎经事件引擎发 EVENT_DM_DOWNLOAD_PROGRESS，本对话框
        用 signal_progress 把事件投递回 GUI 主线程刷新进度条；主线程只在
        模态进度对话框上等待，不阻塞 Qt 事件循环，界面不卡。
        """
        universe: DataUniverse = self._build_universe()
        interval: Interval = Interval(self.interval_combo.currentData())
        mode_map: dict = {
            "追加": DownloadMode.APPEND,
            "补缺口": DownloadMode.REPAIR,
            "重下": DownloadMode.REPLACE,
        }
        mode: DownloadMode = mode_map[self.mode_combo.currentText()]

        start_date: QtCore.QDate = self.start_date_edit.date()
        end_date: QtCore.QDate = self.end_date_edit.date()
        start: datetime = datetime(start_date.year(), start_date.month(), start_date.day(), tzinfo=DB_TZ)
        end: datetime = datetime(end_date.year(), end_date.month(), end_date.day(), 23, 59, 59, tzinfo=DB_TZ)

        # UI 生成 task_id，便于取消按钮定位任务（引擎 task_id= 参数回退到自生成）
        task_id: str = str(uuid.uuid4())
        self._task_id: str = task_id
        self._last_result: DownloadTaskResult | None = None

        # 订阅进度事件：事件引擎线程调 signal.emit → Qt 自动投递到主线程槽
        event_engine: EventEngine = self.engine.event_engine
        self.signal_progress.connect(self.process_progress_event)
        event_engine.register(EVENT_DM_DOWNLOAD_PROGRESS, self.signal_progress.emit)

        # 进度对话框（模态，主线程在 exec_ 上等信号槽，不跑下载）
        self._progress_dialog: QtWidgets.QProgressDialog = QtWidgets.QProgressDialog(
            "准备下载...", "取消", 0, 0, self
        )
        self._progress_dialog.setWindowTitle("下载历史数据")
        self._progress_dialog.setMinimumWidth(420)
        self._progress_dialog.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        # 取消：设标志，引擎在当前标的后停止；对话框不立即关，等 CANCELLED 进度回传
        self._progress_dialog.canceled.connect(self._cancel_download)
        # 0..0 表示总量未知，首条进度事件到了再 setMaximum
        self._progress_dialog.setValue(0)

        # 起后台线程跑下载（daemon：进程退出不阻塞）
        self._download_thread: threading.Thread = threading.Thread(
            target=self._run_download,
            args=(task_id, universe, interval, start, end, mode),
            daemon=True,
        )
        self._download_thread.start()

        # 阻塞等进度对话框关闭（终态时 process_progress_event 调 reset()）
        self._progress_dialog.exec_()

        # 结果汇总（沿用原 download 末尾的格式）
        result: DownloadTaskResult | None = self._last_result
        if result is not None:
            QtWidgets.QMessageBox.information(
                self, "下载结束",
                f"标的总数：{result.total}\n成功：{result.success}\n"
                f"失败：{result.failed}\n跳过：{result.skipped}",
                QtWidgets.QMessageBox.StandardButton.Ok,
            )

    def _cancel_download(self) -> None:
        """进度对话框取消按钮：请求引擎在当前标的后停止（非立即）。"""
        self.engine.cancel_download_task(self._task_id)
        # 提示正在取消，等 CANCELLED 进度回传后再关对话框
        if hasattr(self, "_progress_dialog"):
            self._progress_dialog.setLabelText("正在取消，等待当前标的完成...")

    def _run_download(
        self,
        task_id: str,
        universe: DataUniverse,
        interval: Interval,
        start: datetime,
        end: datetime,
        mode: DownloadMode,
    ) -> None:
        """后台线程：调引擎批量下载并写入 DuckDB，结果存 self._last_result。

        引擎在循环里 _emit_progress 发 EVENT_DM_DOWNLOAD_PROGRESS，经事件引擎
        → signal_progress → 主线程 process_progress_event 刷新进度。线程结束
        后发一个终态进度事件（若引擎未发）并清理事件订阅。
        """
        try:
            if interval == Interval.TICK:
                result: DownloadTaskResult = self.engine.download_universe_tick_data(
                    universe, start, end, self.output, task_id=task_id
                )
            else:
                result = self.engine.download_universe_bar_data(
                    universe, interval, start, end, self.output, mode, task_id=task_id
                )
            self._last_result = result
        except Exception as ex:  # noqa: BLE001 - 后台线程异常不崩 GUI
            self.output(f"下载任务异常终止：{ex}")
            self._last_result = DownloadTaskResult(
                task_id=task_id, universe=universe.name, interval=interval,
                start=start, end=end, mode=mode, total=0,
                success=0, failed=0, skipped=0,
                failed_symbols=[], status=DownloadTaskStatus.FAILED, error=str(ex),
            )
            # 异常时引擎未发终态，手动发一个，让主线程关对话框
            self.engine._emit_progress(DownloadProgress(
                task_id=task_id, phase="异常终止", total_symbols=0,
                completed_symbols=0, failed_symbols=0, skipped_symbols=0,
                current_symbol="", status=DownloadTaskStatus.FAILED, error=str(ex),
            ))
        finally:
            # 清理事件订阅，避免下次重复绑定
            event_engine: EventEngine = self.engine.event_engine
            event_engine.unregister(EVENT_DM_DOWNLOAD_PROGRESS, self.signal_progress.emit)

    def process_progress_event(self, event: Event) -> None:
        """主线程槽：据 DownloadProgress 刷新进度对话框，终态时关闭。"""
        p: DownloadProgress = event.data
        if not isinstance(p, DownloadProgress):
            return
        if not hasattr(self, "_progress_dialog"):
            return

        dialog: QtWidgets.QProgressDialog = self._progress_dialog
        total: int = p.total_symbols
        done: int = p.completed_symbols + p.failed_symbols + p.skipped_symbols

        if total > 0:
            dialog.setMaximum(total)
        dialog.setValue(done)
        dialog.setLabelText(
            f"{p.phase}：{p.current_symbol or '—'}\n"
            f"成功 {p.completed_symbols}  失败 {p.failed_symbols}  "
            f"跳过 {p.skipped_symbols}  /  总数 {total}"
        )

        # 终态：存结果并关闭对话框（触发 exec_ 返回）
        if p.status in (
            DownloadTaskStatus.COMPLETED,
            DownloadTaskStatus.CANCELLED,
            DownloadTaskStatus.FAILED,
        ):
            # 引擎未回传完整结果时，由进度字段拼一个最小结果给汇总弹框用
            if self._last_result is None:
                self._last_result = DownloadTaskResult(
                    task_id=p.task_id, universe="", interval=None,
                    start=datetime.now(DB_TZ), end=datetime.now(DB_TZ),
                    mode=DownloadMode.APPEND, total=total,
                    success=p.completed_symbols, failed=p.failed_symbols,
                    skipped=p.skipped_symbols, failed_symbols=[],
                    status=p.status, error=p.error,
                )
            # 断开 canceled→_cancel_download，避免 reset() 触发再次取消
            try:
                dialog.canceled.disconnect(self._cancel_download)
            except (TypeError, RuntimeError):
                pass
            dialog.reset()

    def output(self, msg: str) -> None:
        """输出下载过程中的日志。"""
        self.engine.write_log(msg, source="DataManager")

