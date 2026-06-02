"""plink v5.0 - 主窗口

整合所有 UI 组件，协调各模块间的通信。
v5.0: 连接恢复指示器、内存写入信号链、局部统计、配置持久化。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QFileDialog,
    QMessageBox,
    QStatusBar,
    QApplication,
    QLabel,
    QPushButton,
    QGroupBox,
)

from ..core.data_types import (
    ConnectionConfig,
    VarWatchEntry,
    VariableInfo,
    VarType,
)
from ..core.pyocd_backend import PyOcdBackend, PyOcdError, WriteError
from ..core.elf_reader import ElfSymbolReader
from ..core.symbol_cache import SymbolCache
from ..sampling_engine import SamplingEngine
from ..ring_buffer import MultiChannelRingBuffer
from .connection_panel import ConnectionPanel
from .variable_list import VariableListPanel
from .waveform_view import WaveformView

logger = logging.getLogger(__name__)


PRESET_COLORS = [
    "#00FF00", "#FF0000", "#0000FF", "#FFFF00", "#FF00FF", "#00FFFF",
    "#FF8000", "#8000FF", "#00FF80", "#FF0080", "#80FF00", "#0080FF",
]


_CONFIG_DIR = Path.home() / ".plink"
_CONFIG_FILE = _CONFIG_DIR / "config.json"


_AUTO_SAVE_DELAY_MS = 1000


class ConnectionWorker(QThread):
    """v3.0 后台连接线程

    连接流程：
      1. 解析 ELF（不依赖硬件，立即执行，~50ms）
      2. pyOCD 连接探针（attach 模式，不暂停 MCU）
    """

    progress = pyqtSignal(str)
    success = pyqtSignal(object, object)
    error = pyqtSignal(str)

    def __init__(self, config: ConnectionConfig, parent=None):
        super().__init__(parent)
        self._config = config

    def run(self):
        cfg = self._config


        self.progress.emit("正在解析符号文件...")
        reader = ElfSymbolReader()
        try:
            reader.load(cfg.elf_path)
        except Exception as e:
            self.error.emit(f"ELF 解析失败: {e}")
            return
        symbol_count = len(reader.list_globals())
        self.progress.emit(f"符号解析完成: {symbol_count} 个全局变量")


        self.progress.emit("正在连接调试探针...")
        backend = PyOcdBackend()
        try:
            backend.connect(
                unique_id=cfg.probe_unique_id,
                target_override=cfg.target_override,
                frequency=cfg.swd_frequency,
                connect_mode="attach",
            )
        except Exception as e:
            self.error.emit(f"pyOCD 连接失败: {e}")
            return


        cache = SymbolCache(reader)
        self.progress.emit("连接成功")
        self.success.emit(backend, cache)


class MainWindow(QMainWindow):
    """主窗口

    整合连接面板、变量列表、波形显示等组件，协调各模块间的通信。
    v5.0: 连接恢复指示器、内存写入、局部统计、配置持久化。
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("plink v5.0 - 嵌入式实时波形可视化工具")
        self.setGeometry(100, 100, 1400, 900)


        self._backend: Optional[PyOcdBackend] = None
        self._symbol_cache: Optional[SymbolCache] = None
        self._sampling_engine: Optional[SamplingEngine] = None
        self._buffer_manager = MultiChannelRingBuffer(capacity=600000)


        self._watch_entries: list[VarWatchEntry] = []
        self._color_index = 0


        self._csv_export_dir = str(Path.home() / "Documents")
        self._csv_filename_prefix = "plink_data"


        self._connection_worker: Optional[ConnectionWorker] = None
        self._is_connecting = False
        self._target_frequency = 1000


        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.timeout.connect(self._auto_save_config)


        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(500)

        self._setup_ui()
        self._setup_menu()
        self._setup_connections()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        splitter = QSplitter(Qt.Orientation.Horizontal)


        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        self._connection_panel = ConnectionPanel()
        left_layout.addWidget(self._connection_panel)


        debug_group = QGroupBox("调试控制")
        debug_layout = QHBoxLayout(debug_group)

        self._reset_btn = QPushButton("复位运行")
        self._reset_btn.setToolTip("复位 MCU 并从头开始运行")
        self._reset_btn.clicked.connect(self._on_reset_mcu)
        debug_layout.addWidget(self._reset_btn)

        self._halt_btn = QPushButton("暂停")
        self._halt_btn.setToolTip("暂停 MCU 运行（断点）")
        self._halt_btn.clicked.connect(self._on_halt_mcu)
        debug_layout.addWidget(self._halt_btn)

        self._resume_btn = QPushButton("继续运行")
        self._resume_btn.setToolTip("恢复 MCU 运行")
        self._resume_btn.clicked.connect(self._on_resume_mcu)
        debug_layout.addWidget(self._resume_btn)

        self._mcu_state_label = QLabel("MCU: 未知")
        debug_layout.addWidget(self._mcu_state_label)

        left_layout.addWidget(debug_group)
        self._update_debug_controls(False)

        self._variable_panel = VariableListPanel()
        left_layout.addWidget(self._variable_panel)

        splitter.addWidget(left_widget)


        self._waveform_view = WaveformView(self._buffer_manager)
        splitter.addWidget(self._waveform_view)

        splitter.setSizes([400, 1000])
        layout.addWidget(splitter)


        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)


        self._connection_indicator = QLabel("🔴 未连接")
        self._status_bar.addWidget(self._connection_indicator)

        self._status_label = QLabel("就绪")
        self._status_bar.addWidget(self._status_label)

    def _setup_menu(self) -> None:
        menubar = self.menuBar()


        file_menu = menubar.addMenu("文件")

        load_config_action = QAction("加载配置", self)
        load_config_action.triggered.connect(self._load_config)
        file_menu.addAction(load_config_action)

        save_config_action = QAction("保存配置", self)
        save_config_action.triggered.connect(self._save_config)
        file_menu.addAction(save_config_action)

        file_menu.addSeparator()

        export_csv_action = QAction("导出 CSV", self)
        export_csv_action.triggered.connect(self._export_csv)
        file_menu.addAction(export_csv_action)

        file_menu.addSeparator()

        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)


        settings_menu = menubar.addMenu("设置")

        freq_menu = settings_menu.addMenu("采样频率")
        for freq in [100, 150, 200, 250, 300, 500, 1000, 2000]:
            action = QAction(f"{freq} Hz", self)
            action.triggered.connect(lambda checked, f=freq: self._set_frequency(f))
            freq_menu.addAction(action)

        settings_menu.addSeparator()

        csv_path_action = QAction("设置 CSV 导出路径...", self)
        csv_path_action.triggered.connect(self._set_csv_export_path)
        settings_menu.addAction(csv_path_action)


        help_menu = menubar.addMenu("帮助")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _setup_connections(self) -> None:
        self._connection_panel.connect_requested.connect(self._on_connect)
        self._connection_panel.disconnect_requested.connect(self._on_disconnect)

        self._variable_panel.add_variable_requested.connect(self._on_add_variable)
        self._variable_panel.remove_variable_requested.connect(self._on_remove_variable)
        self._variable_panel.change_color_requested.connect(self._on_change_color)
        self._variable_panel.change_enabled_requested.connect(self._on_change_enabled)
        self._variable_panel.modify_value_requested.connect(self._on_modify_value)
        self._variable_panel.scale_changed_requested.connect(self._on_change_scale_offset)
        self._waveform_view.start_requested.connect(self._on_start_sampling)
        self._waveform_view.pause_requested.connect(self._on_pause_sampling)



    def _on_connect(self, config: ConnectionConfig) -> None:
        if self._is_connecting:
            QMessageBox.warning(self, "提示", "正在连接中，请稍候...")
            return


        if not config.elf_path or not Path(config.elf_path).exists():
            QMessageBox.warning(self, "提示", "请先选择有效的 ELF/AXF 文件")
            return

        self._is_connecting = True
        self._connection_panel.set_connected(False)
        self._connection_panel.set_progress("正在连接...")
        self._connection_indicator.setText("🟡 连接中...")
        self._status_label.setText("正在连接...")
        QApplication.processEvents()


        self._connection_worker = ConnectionWorker(config, self)
        self._connection_worker.progress.connect(self._on_connection_progress)
        self._connection_worker.success.connect(self._on_connection_success)
        self._connection_worker.error.connect(self._on_connection_error)
        self._connection_worker.finished.connect(lambda: setattr(self, '_is_connecting', False))
        self._connection_worker.start()

    def _on_connection_progress(self, status: str) -> None:
        self._connection_panel.set_progress(status)
        self._status_label.setText(status)

    def _on_connection_success(self, backend: PyOcdBackend, cache: SymbolCache) -> None:
        self._backend = backend
        self._symbol_cache = cache


        self._variable_panel.populate_tree(cache)


        self._sampling_engine = SamplingEngine(backend, self._buffer_manager)


        self._sampling_engine.on_connection_lost = self._on_connection_lost


        self._connection_panel.set_connected(True)
        probe_name = backend.probe_name
        freq_mhz = backend.session_frequency / 1_000_000
        self._connection_indicator.setText(f"🟢 已连接 — {probe_name} @ {freq_mhz:.0f} MHz")
        self._status_label.setText(f"已连接 ({cache.variable_count} 个变量)")
        self._update_sampling_controls()
        self._update_debug_controls(True)
        logger.info("连接成功: %d 个全局变量", cache.variable_count)


        self._auto_load_config()

    def _on_connection_error(self, error_msg: str) -> None:
        logger.error("连接失败: %s", error_msg)
        QMessageBox.critical(self, "连接失败", error_msg)
        self._connection_panel.set_connected(False)
        self._connection_panel.set_progress("")
        self._connection_indicator.setText("🔴 已断开")
        self._status_label.setText("连接失败")
        self._is_connecting = False

    def _on_connection_lost(self, error_type: str, message: str) -> None:
        """处理采样引擎报告的连接异常（从采样线程回调）。"""
        logger.warning("连接异常: type=%s, msg=%s", error_type, message)

        if error_type == "usb_disconnect":

            if self._sampling_engine and self._sampling_engine.is_running:
                self._sampling_engine.stop()
            self._connection_indicator.setText("🔴 连接已断开")
            self._status_label.setText("USB 连接断开")

            QTimer.singleShot(0, lambda: QMessageBox.critical(
                self, "连接断开",
                "USB 连接已断开，请检查线缆后手动重连。\n\n"
                "历史波形数据已保留，可在离线状态下回看。"
            ))

        elif error_type == "swd_timeout":
            self._connection_indicator.setText("🟡 SWD 通信异常，重试中...")
            self._status_label.setText(f"SWD 通信异常: {message}")

        elif error_type == "target_reset":
            self._connection_indicator.setText("🟡 目标已复位，正在重连...")
            self._status_label.setText("目标已复位，正在重连...")

        elif error_type == "address_error":
            if self._sampling_engine and self._sampling_engine.is_running:
                self._sampling_engine.stop()
            self._connection_indicator.setText("🔴 地址错误")
            QTimer.singleShot(0, lambda: QMessageBox.warning(
                self, "地址错误",
                "内存地址访问错误，可能 ELF 文件与目标固件不匹配。\n"
                "请重新加载 ELF 文件。"
            ))

        self._update_sampling_controls()

    def _on_disconnect(self) -> None:

        if self._connection_worker and self._connection_worker.isRunning():
            self._connection_worker.terminate()
            self._connection_worker.wait(2000)
            self._connection_worker = None
        self._is_connecting = False


        if self._sampling_engine:
            self._sampling_engine.stop()
            self._sampling_engine = None


        if self._backend:
            self._backend.disconnect()
            self._backend = None

        self._symbol_cache = None


        self._watch_entries.clear()
        self._buffer_manager.clear_all()
        self._waveform_view.clear()
        self._variable_panel.update_watch_list([])


        self._connection_panel.set_connected(False)
        self._connection_indicator.setText("🔴 未连接")
        self._status_label.setText("已断开")
        self._update_debug_controls(False)
        self._update_sampling_controls()
        logger.info("已断开连接")



    def _on_add_variable(self, expression: str) -> None:
        if not self._symbol_cache:
            QMessageBox.warning(self, "警告", "请先连接到目标板")
            return

        if any(entry.expression == expression for entry in self._watch_entries):
            QMessageBox.information(self, "提示", f"变量已在观察列表中: {expression}")
            return


        var_info = self._symbol_cache.resolve(expression)
        if var_info is None:

            results = self._symbol_cache.search(expression)
            if results:
                var_info = results[0]
            else:
                QMessageBox.warning(self, "添加失败", f"未找到变量: {expression}")
                return


        entry = VarWatchEntry(
            expression=expression,
            var_info=var_info,
            enabled=True,
            color=PRESET_COLORS[self._color_index % len(PRESET_COLORS)],
        )
        self._color_index += 1


        self._symbol_cache.add_to_monitor(var_info)


        if self._sampling_engine:
            buffer_id = self._sampling_engine.add_variable(entry)
            entry.buffer_id = buffer_id


        self._watch_entries.append(entry)
        self._variable_panel.update_watch_list(self._watch_entries)
        self._waveform_view.update_watch_list(self._watch_entries)
        self._update_sampling_controls()


        self._trigger_auto_save()

        logger.info("添加变量: %s @ 0x%08X", var_info.name, var_info.address)

    def _on_remove_variable(self, buffer_id: int) -> None:
        if self._sampling_engine:
            self._sampling_engine.remove_variable(buffer_id)


        if self._symbol_cache:
            for entry in self._watch_entries:
                if entry.buffer_id == buffer_id and entry.var_info:
                    self._symbol_cache.remove_from_monitor(entry.var_info.name)
                    break

        self._watch_entries = [e for e in self._watch_entries if e.buffer_id != buffer_id]
        self._variable_panel.update_watch_list(self._watch_entries)
        self._waveform_view.update_watch_list(self._watch_entries)

        if self._sampling_engine and not self._watch_entries and self._sampling_engine.is_running:
            self._sampling_engine.stop()

        self._update_sampling_controls()
        self._trigger_auto_save()

    def _on_change_color(self, buffer_id: int, color: str) -> None:
        for entry in self._watch_entries:
            if entry.buffer_id == buffer_id:
                entry.color = color
                break
        self._variable_panel.update_watch_list(self._watch_entries)
        self._waveform_view.update_watch_list(self._watch_entries)
        self._trigger_auto_save()

    def _on_change_enabled(self, buffer_id: int, enabled: bool) -> None:
        for entry in self._watch_entries:
            if entry.buffer_id == buffer_id:
                entry.enabled = enabled
                break
        self._variable_panel.update_watch_list(self._watch_entries)
        self._waveform_view.update_watch_list(self._watch_entries)
        self._trigger_auto_save()

    def _on_change_scale_offset(self, buffer_id: int, scale: float, offset: float) -> None:
        """处理通道缩放/偏移变更。"""
        for entry in self._watch_entries:
            if entry.buffer_id == buffer_id:
                entry.scale = scale
                entry.offset = offset
                break
        self._waveform_view.update_watch_list(self._watch_entries)
        self._trigger_auto_save()

    def _on_modify_value(self, expression: str, value_str: str) -> None:
        """处理变量值修改请求（从 WatchTableWidget 发出）。"""
        if not self._backend or not self._sampling_engine:
            self._status_label.showMessage("写入失败: 未连接", 3000)
            return


        entry = None
        for e in self._watch_entries:
            if e.expression == expression:
                entry = e
                break
        if not entry or not entry.var_info:
            self._status_label.showMessage(f"写入失败: 未找到变量 {expression}", 3000)
            return

        var_info = entry.var_info


        try:
            raw_value = self._parse_value_for_write(value_str, var_info)
        except ValueError as e:
            self._status_label.showMessage(f"写入失败: {e}", 3000)
            return


        write_lock = self._sampling_engine.get_write_lock()
        acquired = write_lock.acquire(timeout=0.1)
        if not acquired:
            self._status_label.showMessage("写入超时，变量未更新 — 采样线程繁忙", 2000)
            return

        try:
            self._backend.write_variable(var_info, raw_value)
            self._status_label.showMessage(f"已写入 {expression} = {value_str}", 2000)
        except WriteError as e:
            self._status_label.showMessage(f"写入失败: {expression} — {e}", 3000)
        except Exception as e:
            self._status_label.showMessage(f"写入失败: {expression} — {e}", 3000)
        finally:
            write_lock.release()

    def _parse_value_for_write(self, value_str: str, var_info: VariableInfo) -> int:
        """解析用户输入的值为整数（用于写入 MCU 内存）。

        支持十进制、十六进制 (0x...)、浮点数（转为 IEEE 754 整数表示）。

        Raises:
            ValueError: 无法解析
        """
        value_str = value_str.strip()


        if var_info.enum_values:

            if '(' in value_str and value_str.endswith(')'):
                try:
                    inner = value_str[value_str.index('(') + 1:-1]
                    return int(inner)
                except ValueError:
                    pass

            for val, names in var_info.enum_values.items():
                if value_str in names:
                    return val


        if var_info.var_type in (VarType.F32, VarType.F64):
            fval = float(value_str)
            if var_info.var_type == VarType.F32:
                import struct as _struct
                return _struct.unpack('<I', _struct.pack('<f', fval))[0]
            else:
                import struct as _struct
                lo, hi = _struct.unpack('<II', _struct.pack('<d', fval))
                return lo | (hi << 32)


        if value_str.startswith('0x') or value_str.startswith('0X'):
            return int(value_str, 16)
        return int(value_str)

    def _on_start_sampling(self) -> None:
        if not self._sampling_engine or not self._backend:
            QMessageBox.warning(self, "提示", "请先连接到目标板")
            return

        if not self._watch_entries:
            QMessageBox.information(self, "提示", "请先把变量加入观察列表")
            return

        if not self._sampling_engine.is_running:
            self._sampling_engine.start(self._target_frequency)
            self._connection_indicator.setText(
                f"🟢 已连接 — {self._backend.probe_name} @ {self._backend.session_frequency / 1e6:.0f} MHz"
            )
            self._status_label.setText(f"采样中: {self._target_frequency} Hz")

        self._update_sampling_controls()

    def _on_pause_sampling(self) -> None:
        if self._sampling_engine and self._sampling_engine.is_running:
            self._sampling_engine.stop()
            self._status_label.setText("采样已暂停")

        self._update_sampling_controls()



    def _on_reset_mcu(self) -> None:
        """复位 MCU 并从头运行。

        流程：reset_halt → 清空 → 启动采样 → resume
        确保采样线程在 MCU 恢复运行的瞬间就已经在读取数据。
        """
        if not self._backend or not self._sampling_engine:
            QMessageBox.warning(self, "提示", "请先连接到目标板")
            return

        if not self._watch_entries:
            QMessageBox.information(self, "提示", "请先把变量加入观察列表")
            return


        if self._sampling_engine.is_running:
            self._sampling_engine.stop()


        self._backend.reset_halt()


        self._buffer_manager.clear_all()
        self._waveform_view.clear()
        self._waveform_view.update_watch_list(self._watch_entries)


        self._sampling_engine.start(self._target_frequency)
        time.sleep(0.1)


        self._backend.resume()

        self._mcu_state_label.setText("MCU: 运行中")
        self._status_label.setText("MCU 已复位，从头开始采样")
        self._update_sampling_controls()

    def _on_halt_mcu(self) -> None:
        """暂停 MCU（类似 Keil 的暂停/断点）。"""
        if not self._backend:
            return


        if self._sampling_engine and self._sampling_engine.is_running:
            self._sampling_engine.stop()

        ok = self._backend.halt()
        if ok:
            self._mcu_state_label.setText("MCU: 已暂停")
            self._status_label.setText("MCU 已暂停")
        else:
            self._mcu_state_label.setText("MCU: 暂停失败")

        self._update_sampling_controls()

    def _on_resume_mcu(self) -> None:
        """恢复 MCU 运行。"""
        if not self._backend:
            return

        ok = self._backend.resume()
        if ok:
            self._mcu_state_label.setText("MCU: 运行中")
            self._status_label.setText("MCU 已恢复运行")
        else:
            self._mcu_state_label.setText("MCU: 恢复失败")

        self._update_sampling_controls()

    def _update_debug_controls(self, connected: bool) -> None:
        """更新调试控制按钮状态"""
        self._reset_btn.setEnabled(connected)
        self._halt_btn.setEnabled(connected)
        self._resume_btn.setEnabled(connected)
        if not connected:
            self._mcu_state_label.setText("MCU: 未知")

    def _update_sampling_controls(self) -> None:
        connected = self._backend is not None and self._sampling_engine is not None
        running = self._sampling_engine.is_running if self._sampling_engine else False
        has_variables = bool(self._watch_entries)
        self._waveform_view.set_sampling_state(connected, has_variables, running)



    def _update_status(self) -> None:
        if self._sampling_engine and self._sampling_engine.is_running:
            freq = self._sampling_engine.actual_frequency
            count = self._sampling_engine.sample_count
            errors = self._sampling_engine.error_count
            self._status_label.setText(
                f"采样: {freq:.1f} Hz | 样本: {count} | 错误: {errors}"
            )
            self._waveform_view.set_actual_frequency(freq)


            for entry in self._watch_entries:
                buf = self._buffer_manager.get_buffer(entry.buffer_id)
                if buf:
                    last = buf.get_last_value()
                    if last:
                        _, value = last
                        self._variable_panel.update_value(entry.buffer_id, value)


            self._update_local_statistics()

        elif self._backend and self._sampling_engine and not self._is_connecting:
            self._status_label.setText(f"已连接 | 已暂停 | 观察变量 {len(self._watch_entries)} 个")
            self._waveform_view.set_actual_frequency(0.0)

    def _update_local_statistics(self) -> None:
        """更新当前可见时间窗口内的局部统计（Max/Min/Avg）。"""
        visible_time = self._waveform_view.get_visible_time_range()
        if visible_time is None:
            return

        t_start_s, t_end_s = visible_time
        if self._waveform_view.time_origin_ns == 0:
            return

        t_start_ns = self._waveform_view.time_origin_ns + int(t_start_s * 1e9)
        t_end_ns = self._waveform_view.time_origin_ns + int(t_end_s * 1e9)

        for entry in self._watch_entries:
            buf = self._buffer_manager.get_buffer(entry.buffer_id)
            if not buf or buf.count == 0:
                continue

            ts, vals = buf.get_range(t_start_ns, t_end_ns)
            if len(vals) == 0:
                continue


            if len(vals) > 2000:
                step = len(vals) // 2000
                vals = vals[::step]

            import numpy as np
            min_v = float(np.min(vals))
            max_v = float(np.max(vals))
            avg_v = float(np.mean(vals))
            self._variable_panel.update_stats(entry.buffer_id, min_v, max_v, avg_v)

    def _set_frequency(self, freq: int) -> None:
        self._target_frequency = freq
        if self._sampling_engine and self._sampling_engine.is_running:
            self._sampling_engine.stop()
            self._sampling_engine.start(freq)
        self._update_sampling_controls()
        logger.info("采样频率已设置为 %d Hz", freq)



    def _save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "保存配置", "", "JSON 文件 (*.json)"
        )
        if not path:
            return

        config = self._build_config()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        logger.info("配置已保存: %s", path)

    def _load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "加载配置", "", "JSON 文件 (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
            self._apply_config(config)
            logger.info("配置已加载: %s", path)
        except Exception as e:
            QMessageBox.warning(self, "加载失败", str(e))

    def _build_config(self) -> dict:
        """构建当前配置字典。"""

        geometry = self.saveGeometry().toBase64().data().decode('ascii')
        state = self.saveState().toBase64().data().decode('ascii')

        return {
            "version": "5.0",
            "connection": self._connection_panel.get_config().__dict__,
            "variables": [
                {
                    "expression": e.expression,
                    "color": e.color,
                    "enabled": e.enabled,
                    "scale": e.scale,
                    "offset": e.offset,
                }
                for e in self._watch_entries
            ],
            "sampling": {
                "frequency": self._target_frequency,
            },
            "trigger": self._waveform_view.get_trigger_config(),
            "csv_export": {
                "directory": self._csv_export_dir,
                "filename_prefix": self._csv_filename_prefix,
            },
            "window": {
                "geometry": geometry,
                "state": state,
            },
        }

    def _apply_config(self, config: dict) -> None:
        """应用配置字典到当前状态。"""

        for var_config in config.get("variables", []):
            expr = var_config.get("expression", "")
            if expr:
                self._on_add_variable(expr)

                for entry in self._watch_entries:
                    if entry.expression == expr:
                        entry.color = var_config.get("color", entry.color)
                        entry.scale = var_config.get("scale", entry.scale)
                        entry.offset = var_config.get("offset", entry.offset)
                        break


        sampling = config.get("sampling", {})
        if sampling:
            self._target_frequency = sampling.get("frequency", self._target_frequency)


        trigger = config.get("trigger", {})
        if trigger:
            self._waveform_view.set_trigger_config(trigger)


        csv_config = config.get("csv_export", {})
        if csv_config:
            self._csv_export_dir = csv_config.get("directory", self._csv_export_dir)
            self._csv_filename_prefix = csv_config.get("filename_prefix", self._csv_filename_prefix)


        window = config.get("window", {})
        if window:
            geometry = window.get("geometry")
            if geometry:
                self.restoreGeometry(bytes(geometry, 'ascii'))
            wstate = window.get("state")
            if wstate:
                self.restoreState(bytes(wstate, 'ascii'))


        self._variable_panel.update_watch_list(self._watch_entries)
        self._waveform_view.update_watch_list(self._watch_entries)

    def _auto_save_config(self) -> None:
        """自动保存配置到 ~/.plink/config.json。"""
        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            config = self._build_config()
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            logger.debug("配置已自动保存: %s", _CONFIG_FILE)
        except Exception as e:
            logger.warning("自动保存配置失败: %s", e)

    def _auto_load_config(self) -> None:
        """自动加载 ~/.plink/config.json。"""
        if not _CONFIG_FILE.exists():
            return
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)

            for var_config in config.get("variables", []):
                expr = var_config.get("expression", "")
                if expr:

                    if self._symbol_cache and self._symbol_cache.resolve(expr):
                        self._on_add_variable(expr)
                        for entry in self._watch_entries:
                            if entry.expression == expr:
                                entry.color = var_config.get("color", entry.color)
                                entry.scale = var_config.get("scale", entry.scale)
                                entry.offset = var_config.get("offset", entry.offset)
                                break
                    else:
                        logger.info("配置中的变量在当前 ELF 中未找到，跳过: %s", expr)


            sampling = config.get("sampling", {})
            if sampling:
                self._target_frequency = sampling.get("frequency", self._target_frequency)


            trigger = config.get("trigger", {})
            if trigger:
                self._waveform_view.set_trigger_config(trigger)


            window = config.get("window", {})
            if window:
                geometry = window.get("geometry")
                if geometry:
                    self.restoreGeometry(bytes(geometry, 'ascii'))
                wstate = window.get("state")
                if wstate:
                    self.restoreState(bytes(wstate, 'ascii'))


            self._variable_panel.update_watch_list(self._watch_entries)
            self._waveform_view.update_watch_list(self._watch_entries)

            logger.info("已自动加载配置: %s", _CONFIG_FILE)
        except Exception as e:
            logger.warning("自动加载配置失败: %s", e)

    def _trigger_auto_save(self) -> None:
        """触发防抖自动保存（延迟 1 秒）。"""
        self._auto_save_timer.start(_AUTO_SAVE_DELAY_MS)

    def _export_csv(self) -> None:
        if not self._watch_entries:
            QMessageBox.information(self, "提示", "没有数据可导出")
            return

        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        default_filename = f"{self._csv_filename_prefix}_{timestamp_str}.csv"
        default_path = str(Path(self._csv_export_dir) / default_filename)

        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", default_path, "CSV 文件 (*.csv)"
        )
        if not path:
            return

        try:
            self._do_export_csv(path)
            logger.info("数据已导出: %s", path)
            QMessageBox.information(self, "导出成功", f"数据已导出到:\n{path}")
        except Exception as e:
            logger.error("导出失败: %s", e)
            QMessageBox.warning(self, "导出失败", str(e))

    def _do_export_csv(self, path: str) -> None:
        import numpy as np

        with open(path, "w", encoding="utf-8", newline="") as f:
            headers = ["timestamp_ns", "time_s"]
            for entry in self._watch_entries:
                headers.append(entry.expression)
            f.write(",".join(headers) + "\n")

            all_data: dict[int, tuple] = {}
            max_len = 0
            start_ns = 0

            for entry in self._watch_entries:
                buf = self._buffer_manager.get_buffer(entry.buffer_id)
                if buf:
                    ts, vals = buf.get_all()
                    all_data[entry.buffer_id] = (ts, vals)
                    if len(ts) > 0:
                        if start_ns == 0:
                            start_ns = ts[0]
                        max_len = max(max_len, len(ts))

            for i in range(max_len):
                row = []
                ts_val = ""
                ts_sec = ""
                for entry in self._watch_entries:
                    if entry.buffer_id in all_data:
                        ts, _ = all_data[entry.buffer_id]
                        if i < len(ts):
                            ts_val = str(ts[i])
                            ts_sec = f"{(ts[i] - start_ns) / 1e9:.6f}"
                            break
                row.append(ts_val)
                row.append(ts_sec)

                for entry in self._watch_entries:
                    if entry.buffer_id in all_data:
                        _, vals = all_data[entry.buffer_id]
                        if i < len(vals):
                            row.append(f"{vals[i]:.6g}")
                        else:
                            row.append("")
                    else:
                        row.append("")

                f.write(",".join(row) + "\n")

    def _set_csv_export_path(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        dir_path = QFileDialog.getExistingDirectory(
            self, "选择 CSV 导出目录", self._csv_export_dir
        )
        if dir_path:
            self._csv_export_dir = dir_path

        prefix, ok = QInputDialog.getText(
            self, "设置文件名前缀",
            "CSV 文件名前缀:",
            text=self._csv_filename_prefix,
        )
        if ok and prefix:
            self._csv_filename_prefix = prefix

        logger.info("CSV 导出设置: 目录=%s, 前缀=%s",
                     self._csv_export_dir, self._csv_filename_prefix)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 plink",
            "plink v5.0 - 嵌入式实时波形可视化工具\n\n"
            "版本: 5.0.0\n"
            "技术栈: Python + PyQt6 + pyelftools + pyOCD\n\n"
            "支持 C++ 命名空间、类成员、模板类型的符号解析\n"
            "最高支持 2000Hz 采样率（聚合读取引擎）\n"
            "pyOCD 直连 DAP，无需 OpenOCD 中间层\n"
            "新增：双游标、局部统计、触发系统、配置持久化"
        )



    def closeEvent(self, event) -> None:

        try:
            _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            config = self._build_config()
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

        if self._connection_worker and self._connection_worker.isRunning():
            self._connection_worker.terminate()
            self._connection_worker.wait(2000)
        self._on_disconnect()
        event.accept()
