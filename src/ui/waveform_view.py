"""plink v5.0 - 波形显示视图（LinkScope 风格）

参考 LinkScope 的示波器式交互设计：
- sec/Div 时间分辨率控制（×10 格）
- 水平滚动条时间轴导航
- "实时更新" 自动跟随模式
- 双游标（A/B）与 ΔT/ΔV 差值显示
- 左键拖动游标查看值
- Ctrl+滚轮水平缩放，Shift+滚轮垂直缩放
- 间隙过滤（>0.5s 断开连线）
- 无渲染点数上限，显示所有可见范围数据
- 软件边沿触发器（Normal/Single 模式）
- 独立通道缩放与偏移
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, QEvent, pyqtSignal
from PyQt6.QtGui import QColor, QKeyEvent, QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QDoubleSpinBox,
    QScrollBar,
    QComboBox,
)

from ..core.data_types import VarWatchEntry
from ..ring_buffer import MultiChannelRingBuffer

logger = logging.getLogger(__name__)


HORI_DIV = 10
VERT_DIV = 6


GAP_THRESHOLD_S = 0.5


INITIAL_MARGIN_S = 0.5


PRE_TRIGGER_DIV = 2


class TriggerState(Enum):
    """触发状态机"""
    IDLE = "idle"
    ARMED = "armed"
    TRIGGERED = "triggered"
    LOCKED = "locked"


class WaveformView(QWidget):
    """波形显示视图（LinkScope 风格）

    v5.0 新增：
    - 双游标（Cursor A/B）与 ΔT/ΔV 差值显示
    - 独立通道缩放与偏移
    - 软件边沿触发器
    """


    cursor_position_changed = pyqtSignal(float)
    start_requested = pyqtSignal()
    pause_requested = pyqtSignal()

    def __init__(
        self,
        buffer_manager: MultiChannelRingBuffer,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._buffer_manager = buffer_manager
        self._entries: list[VarWatchEntry] = []
        self._plots: dict[int, pg.PlotDataItem] = {}
        self._time_origin_ns = 0


        self._sec_per_div = 0.5
        self._val_per_div = 1.0
        self._vert_offset = 0.0
        self._realtime_update = True


        self._cursor_a: Optional[pg.InfiniteLine] = None
        self._cursor_b: Optional[pg.InfiniteLine] = None
        self._cursor_a_time: Optional[float] = None
        self._cursor_b_time: Optional[float] = None
        self._active_cursor = 'a'
        self._cursor_dragging = False
        self._last_mouse_x: Optional[float] = None


        self._looking = False
        self._cursor_line: Optional[pg.InfiniteLine] = None
        self._selected_var_index = 0


        self._trigger_state = TriggerState.IDLE
        self._trigger_source_idx = -1
        self._trigger_edge = "rising"
        self._trigger_threshold = 0.0
        self._trigger_mode = "Normal"
        self._trigger_enabled = False
        self._trigger_ts_ns: Optional[int] = None
        self._last_trigger_ns = 0
        self._min_retrigger_ns = 0

        self._setup_ui()
        self._setup_timer()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)


        toolbar = QHBoxLayout()

        toolbar.addWidget(QLabel("秒/格:"))
        self._sec_div_spin = QDoubleSpinBox()
        self._sec_div_spin.setRange(0.001, 30.0)
        self._sec_div_spin.setValue(self._sec_per_div)
        self._sec_div_spin.setDecimals(3)
        self._sec_div_spin.setSuffix(" s")
        self._sec_div_spin.valueChanged.connect(self._on_sec_div_changed)
        toolbar.addWidget(self._sec_div_spin)

        toolbar.addWidget(QLabel("视图缩放:"))
        self._val_div_spin = QDoubleSpinBox()
        self._val_div_spin.setRange(0.001, 10000.0)
        self._val_div_spin.setValue(self._val_per_div)
        self._val_div_spin.setDecimals(3)
        self._val_div_spin.valueChanged.connect(self._on_val_div_changed)
        toolbar.addWidget(self._val_div_spin)

        toolbar.addWidget(QLabel("视图偏移:"))
        self._offset_spin = QDoubleSpinBox()
        self._offset_spin.setRange(-10000.0, 10000.0)
        self._offset_spin.setValue(0.0)
        self._offset_spin.valueChanged.connect(self._on_offset_changed)
        toolbar.addWidget(self._offset_spin)

        self._realtime_check = QCheckBox("实时更新")
        self._realtime_check.setChecked(True)
        self._realtime_check.toggled.connect(self._on_realtime_toggled)
        toolbar.addWidget(self._realtime_check)

        fit_btn = QPushButton("适应")
        fit_btn.clicked.connect(self._on_fit_clicked)
        toolbar.addWidget(fit_btn)

        self._start_btn = QPushButton("开始采样")
        self._start_btn.clicked.connect(self.start_requested.emit)
        toolbar.addWidget(self._start_btn)

        self._pause_btn = QPushButton("暂停采样")
        self._pause_btn.clicked.connect(self.pause_requested.emit)
        toolbar.addWidget(self._pause_btn)


        self._cursor_a_btn = QPushButton("放置A")
        self._cursor_a_btn.setCheckable(True)
        self._cursor_a_btn.clicked.connect(lambda: self._on_cursor_btn_clicked('a'))
        toolbar.addWidget(self._cursor_a_btn)

        self._cursor_b_btn = QPushButton("放置B")
        self._cursor_b_btn.setCheckable(True)
        self._cursor_b_btn.clicked.connect(lambda: self._on_cursor_btn_clicked('b'))
        toolbar.addWidget(self._cursor_b_btn)

        toolbar.addStretch()

        self._freq_label = QLabel("0 Hz")
        toolbar.addWidget(self._freq_label)

        layout.addLayout(toolbar)


        trigger_bar = QHBoxLayout()

        self._trigger_check = QCheckBox("触发使能")
        self._trigger_check.toggled.connect(self._on_trigger_toggled)
        trigger_bar.addWidget(self._trigger_check)

        trigger_bar.addWidget(QLabel("源:"))
        self._trigger_source_combo = QComboBox()
        self._trigger_source_combo.currentIndexChanged.connect(self._on_trigger_source_changed)
        trigger_bar.addWidget(self._trigger_source_combo)

        trigger_bar.addWidget(QLabel("边沿:"))
        self._trigger_edge_combo = QComboBox()
        self._trigger_edge_combo.addItems(["上升沿", "下降沿"])
        self._trigger_edge_combo.currentTextChanged.connect(self._on_trigger_edge_changed)
        trigger_bar.addWidget(self._trigger_edge_combo)

        trigger_bar.addWidget(QLabel("阈值:"))
        self._trigger_threshold_spin = QDoubleSpinBox()
        self._trigger_threshold_spin.setRange(-1e9, 1e9)
        self._trigger_threshold_spin.setDecimals(3)
        self._trigger_threshold_spin.valueChanged.connect(self._on_trigger_threshold_changed)
        trigger_bar.addWidget(self._trigger_threshold_spin)

        trigger_bar.addWidget(QLabel("模式:"))
        self._trigger_mode_combo = QComboBox()
        self._trigger_mode_combo.addItems(["Normal", "Single"])
        self._trigger_mode_combo.currentTextChanged.connect(self._on_trigger_mode_changed)
        trigger_bar.addWidget(self._trigger_mode_combo)

        self._trigger_reset_btn = QPushButton("复位触发")
        self._trigger_reset_btn.clicked.connect(self._on_trigger_reset)
        trigger_bar.addWidget(self._trigger_reset_btn)

        trigger_bar.addStretch()

        layout.addLayout(trigger_bar)


        pg.setConfigOptions(antialias=False)
        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setLabel('left', '值')
        self._plot_widget.setLabel('bottom', '时间', 's')
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.addLegend()


        vb = self._plot_widget.getViewBox()
        vb.setMouseEnabled(x=False, y=False)
        vb.setMenuEnabled(False)


        self._plot_widget.viewport().installEventFilter(self)

        layout.addWidget(self._plot_widget)


        self._scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self._scrollbar.setRange(0, 0)
        self._scrollbar.valueChanged.connect(self._on_scroll_changed)
        layout.addWidget(self._scrollbar)


        info_bar = QHBoxLayout()
        self._var_combo = QComboBox()
        self._var_combo.setMinimumWidth(120)
        self._var_combo.currentIndexChanged.connect(self._on_var_combo_changed)
        self._value_label = QLabel("当前值: -")
        self._lookval_label = QLabel("查看值: -")
        self._cursor_a_label = QLabel("游标A: -")
        self._cursor_b_label = QLabel("游标B: -")
        self._cursor_delta_label = QLabel("Δ: -")
        info_bar.addWidget(QLabel("观测:"))
        info_bar.addWidget(self._var_combo)
        info_bar.addWidget(self._value_label)
        info_bar.addWidget(self._lookval_label)
        info_bar.addWidget(self._cursor_a_label)
        info_bar.addWidget(self._cursor_b_label)
        info_bar.addWidget(self._cursor_delta_label)
        info_bar.addStretch()
        layout.addLayout(info_bar)

        self.set_sampling_state(False, False, False)

    def _setup_timer(self) -> None:
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_waveforms)
        self._refresh_timer.start(30)



    def update_watch_list(self, entries: list[VarWatchEntry]) -> None:
        self._entries = entries

        if self._entries:
            self._selected_var_index = min(self._selected_var_index, len(self._entries) - 1)
        else:
            self._selected_var_index = 0

        self._var_combo.blockSignals(True)
        self._var_combo.clear()
        for entry in entries:
            self._var_combo.addItem(entry.expression)
        if self._entries:
            self._var_combo.setCurrentIndex(self._selected_var_index)
        self._var_combo.blockSignals(False)
        self._plot_widget.clear()
        self._plots.clear()


        if self._cursor_a:
            self._plot_widget.addItem(self._cursor_a)
        if self._cursor_b:
            self._plot_widget.addItem(self._cursor_b)

        for entry in entries:
            if entry.enabled:
                plot = self._plot_widget.plot(
                    pen=pg.mkPen(entry.color, width=2),
                    name=entry.expression,
                )
                self._plots[entry.buffer_id] = plot

        if not entries:
            self._time_origin_ns = 0


        self._update_trigger_source_list()

    def set_actual_frequency(self, freq: float) -> None:
        self._freq_label.setText(f"{freq:.0f} Hz")

    def set_sampling_state(self, connected: bool, has_variables: bool, running: bool) -> None:
        self._start_btn.setEnabled(connected and has_variables and not running)
        self._pause_btn.setEnabled(connected and running)

    def clear(self) -> None:
        self._plot_widget.clear()
        self._plots.clear()
        self._time_origin_ns = 0
        self._scrollbar.setRange(0, 0)

        self._cursor_a = None
        self._cursor_b = None
        self._cursor_a_time = None
        self._cursor_b_time = None
        self._cursor_a_label.setText("游标A: -")
        self._cursor_b_label.setText("游标B: -")
        self._cursor_delta_label.setText("Δ: -")

    @property
    def time_origin_ns(self) -> int:
        """时间原点（纳秒）。"""
        return self._time_origin_ns

    def get_visible_time_range(self) -> Optional[tuple[float, float]]:
        """获取当前可见时间范围（相对秒）。返回 (t_start, t_end)。"""
        visible_time = self._sec_per_div * HORI_DIV
        total_time_s = self._get_total_time_s()

        if self._trigger_state == TriggerState.TRIGGERED or self._trigger_state == TriggerState.LOCKED:

            if self._trigger_ts_ns and self._time_origin_ns:
                t_trigger = (self._trigger_ts_ns - self._time_origin_ns) / 1e9
                t_start = t_trigger - PRE_TRIGGER_DIV * self._sec_per_div
                t_end = t_start + visible_time
                return (t_start, t_end)

        if self._realtime_update:
            t_end = total_time_s
            t_start = max(-INITIAL_MARGIN_S, t_end - visible_time)
        else:
            scroll_val = self._scrollbar.value()
            t_start = scroll_val / 1000.0 - INITIAL_MARGIN_S
            t_end = t_start + visible_time

        return (t_start, t_end)

    def _get_total_time_s(self) -> float:
        """获取所有通道的最大时间范围（秒）。"""
        total = 0.0
        for entry in self._entries:
            if not entry.enabled:
                continue
            buf = self._buffer_manager.get_buffer(entry.buffer_id)
            if buf and buf.count > 0:
                ts, _ = buf.get_latest(1)
                if len(ts) > 0:

                    latest_ts = ts[-1]
                    t = float((latest_ts - self._time_origin_ns) / 1e9) if self._time_origin_ns else 0.0
                    total = max(total, t)
        return total



    def _refresh_waveforms(self) -> None:
        if not self._entries:
            return

        visible_time = self._sec_per_div * HORI_DIV
        value_range = self._val_per_div * VERT_DIV


        series_data: list[tuple[pg.PlotDataItem, np.ndarray, np.ndarray]] = []
        total_time_s = 0.0

        for entry in self._entries:
            if not entry.enabled or entry.buffer_id not in self._plots:
                continue

            plot = self._plots[entry.buffer_id]
            buf = self._buffer_manager.get_buffer(entry.buffer_id)
            if not buf:
                continue


            count = buf.count
            if count == 0:
                continue

            ts, values = buf.get_latest(count)
            if len(ts) == 0:
                continue


            if self._time_origin_ns == 0:
                self._time_origin_ns = int(ts[0])


            t_relative = (ts - self._time_origin_ns) / 1_000_000_000


            if entry.scale != 1.0 or entry.offset != 0.0:
                values = (values + entry.offset) * entry.scale


            if len(t_relative) > 0:
                ch_total = float(t_relative[-1])
                if ch_total > total_time_s:
                    total_time_s = ch_total

            series_data.append((plot, t_relative, values))

        if not series_data:
            return


        if self._trigger_enabled and self._trigger_state == TriggerState.ARMED:
            self._check_trigger(series_data)


        total_time_ms = int(total_time_s * 1000)
        visible_time_ms = int(visible_time * 1000)
        scroll_max = max(0, total_time_ms - visible_time_ms)


        self._scrollbar.blockSignals(True)
        old_max = self._scrollbar.maximum()
        self._scrollbar.setMaximum(scroll_max)
        if self._realtime_update and self._trigger_state not in (
            TriggerState.TRIGGERED, TriggerState.LOCKED
        ):
            self._scrollbar.setValue(scroll_max)
        elif scroll_max > 0 and old_max > 0:

            ratio = self._scrollbar.value() / old_max if old_max > 0 else 1.0
            self._scrollbar.setValue(int(ratio * scroll_max))
        self._scrollbar.blockSignals(False)


        if self._trigger_state in (TriggerState.TRIGGERED, TriggerState.LOCKED):

            if self._trigger_ts_ns and self._time_origin_ns:
                t_trigger = (self._trigger_ts_ns - self._time_origin_ns) / 1e9
                t_start = t_trigger - PRE_TRIGGER_DIV * self._sec_per_div
                t_end = t_start + visible_time
            else:
                t_start = -INITIAL_MARGIN_S
                t_end = visible_time - INITIAL_MARGIN_S
        elif self._realtime_update:
            t_end = total_time_s
            t_start = max(-INITIAL_MARGIN_S, t_end - visible_time)
        else:
            scroll_val = self._scrollbar.value()
            t_start = scroll_val / 1000.0 - INITIAL_MARGIN_S
            t_end = t_start + visible_time


        for plot, t_relative, values in series_data:

            mask = (t_relative >= t_start) & (t_relative <= t_end)
            t_vis = t_relative[mask]
            v_vis = values[mask]

            if len(t_vis) < 2:
                plot.setData([], [])
                continue


            dt = np.diff(t_vis)
            gap_indices = np.where(dt >= GAP_THRESHOLD_S)[0]
            if len(gap_indices) > 0:

                insert_pos = gap_indices + 1
                t_with_gaps = np.insert(t_vis, insert_pos, np.nan)
                v_with_gaps = np.insert(v_vis, insert_pos, np.nan)
                plot.setData(t_with_gaps, v_with_gaps, skipFiniteCheck=True)
            else:
                plot.setData(t_vis, v_vis, skipFiniteCheck=True)


        min_value = -value_range / 2 - self._vert_offset
        max_value = value_range / 2 - self._vert_offset
        self._plot_widget.setXRange(t_start, t_end, padding=0)
        self._plot_widget.setYRange(min_value, max_value, padding=0)


        self._update_info_bar(t_start, t_end)


        if (self._trigger_state == TriggerState.TRIGGERED
                and self._trigger_mode == "Normal"):
            now_ns = int(self._time_origin_ns + total_time_s * 1e9)
            if now_ns - self._last_trigger_ns >= self._min_retrigger_ns:
                self._trigger_state = TriggerState.ARMED



    def _check_trigger(
        self,
        series_data: list[tuple[pg.PlotDataItem, np.ndarray, np.ndarray]],
    ) -> None:
        """检查触发条件是否满足。"""
        if self._trigger_source_idx < 0 or self._trigger_source_idx >= len(self._entries):
            return


        source_entry = self._entries[self._trigger_source_idx]
        source_data = None
        for plot, t_rel, vals in series_data:

            for entry in self._entries:
                if entry.buffer_id in self._plots and self._plots[entry.buffer_id] is plot:
                    if entry is source_entry:
                        source_data = (t_rel, vals)
                        break
            if source_data:
                break

        if not source_data:
            return

        t_rel, vals = source_data
        if len(vals) < 2:
            return


        check_count = min(50, len(vals) - 1)
        for i in range(len(vals) - 1, len(vals) - 1 - check_count, -1):
            if i < 1:
                break
            prev_val = vals[i - 1]
            curr_val = vals[i]

            if self._trigger_edge == "rising":
                triggered = prev_val < self._trigger_threshold <= curr_val
            else:
                triggered = prev_val > self._trigger_threshold >= curr_val

            if triggered:
                self._trigger_ts_ns = self._time_origin_ns + int(t_rel[i] * 1e9)
                self._last_trigger_ns = self._trigger_ts_ns
                self._trigger_state = TriggerState.TRIGGERED
                self._realtime_update = False
                self._realtime_check.setChecked(False)

                if self._trigger_mode == "Single":
                    self._trigger_state = TriggerState.LOCKED

                logger.info("触发: t=%.6f s, edge=%s, threshold=%.3f",
                            t_rel[i], self._trigger_edge, self._trigger_threshold)
                break

    def _update_trigger_source_list(self) -> None:
        """更新触发源下拉列表。"""
        self._trigger_source_combo.blockSignals(True)
        self._trigger_source_combo.clear()
        for entry in self._entries:
            self._trigger_source_combo.addItem(entry.expression)
        if self._trigger_source_idx < self._trigger_source_combo.count():
            self._trigger_source_combo.setCurrentIndex(self._trigger_source_idx)
        self._trigger_source_combo.blockSignals(False)

    def _on_trigger_toggled(self, checked: bool) -> None:
        self._trigger_enabled = checked
        if checked:
            self._trigger_state = TriggerState.ARMED

            self._min_retrigger_ns = int(self._sec_per_div * HORI_DIV * 0.5e9)
        else:
            self._trigger_state = TriggerState.IDLE

    def _on_trigger_source_changed(self, idx: int) -> None:
        self._trigger_source_idx = idx

    def _on_trigger_edge_changed(self, text: str) -> None:
        self._trigger_edge = "rising" if "上升" in text else "falling"

    def _on_trigger_threshold_changed(self, value: float) -> None:
        self._trigger_threshold = value

    def _on_trigger_mode_changed(self, text: str) -> None:
        self._trigger_mode = text

    def _on_trigger_reset(self) -> None:
        """复位触发状态到 ARMED。"""
        if self._trigger_enabled:
            self._trigger_state = TriggerState.ARMED
            self._realtime_update = True
            self._realtime_check.setChecked(True)

    def get_trigger_config(self) -> dict:
        """获取触发配置（用于持久化）。"""
        return {
            "source_index": self._trigger_source_idx,
            "edge": self._trigger_edge,
            "threshold": self._trigger_threshold,
            "mode": self._trigger_mode,
            "enabled": self._trigger_enabled,
        }

    def set_trigger_config(self, config: dict) -> None:
        """设置触发配置（从持久化加载）。"""
        self._trigger_source_idx = config.get("source_index", -1)
        self._trigger_edge = config.get("edge", "rising")
        self._trigger_threshold = config.get("threshold", 0.0)
        self._trigger_mode = config.get("mode", "Normal")
        self._trigger_enabled = config.get("enabled", False)


        if self._trigger_source_idx < self._trigger_source_combo.count():
            self._trigger_source_combo.setCurrentIndex(self._trigger_source_idx)
        edge_text = "上升沿" if self._trigger_edge == "rising" else "下降沿"
        idx = self._trigger_edge_combo.findText(edge_text)
        if idx >= 0:
            self._trigger_edge_combo.setCurrentIndex(idx)
        self._trigger_threshold_spin.setValue(self._trigger_threshold)
        idx = self._trigger_mode_combo.findText(self._trigger_mode)
        if idx >= 0:
            self._trigger_mode_combo.setCurrentIndex(idx)
        self._trigger_check.setChecked(self._trigger_enabled)



    def _set_active_cursor(self, cursor: str) -> None:
        """设置当前激活的游标。"""
        self._active_cursor = cursor
        self._cursor_a_btn.setChecked(cursor == 'a')
        self._cursor_b_btn.setChecked(cursor == 'b')

    def _switch_selected_var(self, delta: int) -> None:
        """切换游标观测的变量（delta: +1 下一个, -1 上一个）。"""
        if not self._entries:
            return
        self._selected_var_index = (self._selected_var_index + delta) % len(self._entries)
        self._var_combo.blockSignals(True)
        self._var_combo.setCurrentIndex(self._selected_var_index)
        self._var_combo.blockSignals(False)
        self._update_cursor_labels()

    def _on_var_combo_changed(self, index: int) -> None:
        """下拉框切换观测变量。"""
        if 0 <= index < len(self._entries):
            self._selected_var_index = index
            self._update_cursor_labels()

    def _on_cursor_btn_clicked(self, cursor: str) -> None:
        """游标按钮点击：设置激活游标并在鼠标最后位置放置。"""
        self._set_active_cursor(cursor)
        x = self._cursor_line.value() if self._cursor_line else self._last_mouse_x
        if x is not None:
            self._place_cursor(x, cursor)

    def _place_cursor(self, x: float, cursor: str) -> None:
        """在指定时间位置放置游标。"""
        if cursor == 'a':
            if self._cursor_a:
                self._plot_widget.removeItem(self._cursor_a)
            self._cursor_a = pg.InfiniteLine(
                pos=x, angle=90,
                pen=pg.mkPen(QColor(0, 120, 255), width=1, style=Qt.PenStyle.DashLine),
                movable=True,
            )
            self._cursor_a.sigPositionChanged.connect(
                lambda: self._on_cursor_moved('a'))
            self._plot_widget.addItem(self._cursor_a)
            self._cursor_a_time = x
        else:
            if self._cursor_b:
                self._plot_widget.removeItem(self._cursor_b)
            self._cursor_b = pg.InfiniteLine(
                pos=x, angle=90,
                pen=pg.mkPen(QColor(255, 60, 60), width=1, style=Qt.PenStyle.DashLine),
                movable=True,
            )
            self._cursor_b.sigPositionChanged.connect(
                lambda: self._on_cursor_moved('b'))
            self._plot_widget.addItem(self._cursor_b)
            self._cursor_b_time = x

        self._update_cursor_labels()

    def _on_cursor_moved(self, cursor: str) -> None:
        """游标被拖拽后更新。"""
        if cursor == 'a' and self._cursor_a:
            self._cursor_a_time = self._cursor_a.value()
        elif cursor == 'b' and self._cursor_b:
            self._cursor_b_time = self._cursor_b.value()
        self._update_cursor_labels()

    def _update_cursor_labels(self) -> None:
        """更新游标信息标签。"""

        if self._cursor_a_time is not None:
            val_a = self._get_cursor_value(self._cursor_a_time)
            self._cursor_a_label.setText(
                f"游标A: t={self._cursor_a_time:.6f}s  V={val_a:.6g}" if val_a is not None
                else f"游标A: t={self._cursor_a_time:.6f}s"
            )
        else:
            self._cursor_a_label.setText("游标A: -")


        if self._cursor_b_time is not None:
            val_b = self._get_cursor_value(self._cursor_b_time)
            self._cursor_b_label.setText(
                f"游标B: t={self._cursor_b_time:.6f}s  V={val_b:.6g}" if val_b is not None
                else f"游标B: t={self._cursor_b_time:.6f}s"
            )
        else:
            self._cursor_b_label.setText("游标B: -")


        if (self._cursor_a_time is not None and self._cursor_b_time is not None):
            dt = self._cursor_b_time - self._cursor_a_time
            val_a = self._get_cursor_value(self._cursor_a_time)
            val_b = self._get_cursor_value(self._cursor_b_time)
            if val_a is not None and val_b is not None:
                dv = val_b - val_a
                self._cursor_delta_label.setText(f"ΔT={dt:.6f}s  ΔV={dv:.6g}")
            else:
                self._cursor_delta_label.setText(f"ΔT={dt:.6f}s")
        else:
            self._cursor_delta_label.setText("Δ: -")

    def _get_cursor_value(self, t_rel: float) -> Optional[float]:
        """获取游标位置对应的变量值。"""
        if not self._entries:
            return None

        idx = min(self._selected_var_index, len(self._entries) - 1)
        if idx < 0:
            return None
        entry = self._entries[idx]
        buf = self._buffer_manager.get_buffer(entry.buffer_id)
        if not buf or buf.count == 0:
            return None

        ts, vals = buf.get_latest(buf.count)
        t_rel_arr = (ts - self._time_origin_ns) / 1_000_000_000
        idx_arr = np.searchsorted(t_rel_arr, t_rel)
        if idx_arr > 0 and idx_arr < len(t_rel_arr):
            if abs(t_rel_arr[idx_arr] - t_rel) < abs(t_rel_arr[idx_arr - 1] - t_rel):
                return float(vals[idx_arr])
            else:
                return float(vals[idx_arr - 1])
        elif idx_arr == 0 and len(t_rel_arr) > 0:
            return float(vals[0])
        return None



    def _update_info_bar(self, t_start: float, t_end: float) -> None:
        """更新底部变量名、当前值、查看值显示"""
        if not self._entries:
            self._value_label.setText("当前值: -")
            self._lookval_label.setText("查看值: -")
            return


        idx = min(self._selected_var_index, len(self._entries) - 1)
        if idx < 0:
            return
        entry = self._entries[idx]


        buf = self._buffer_manager.get_buffer(entry.buffer_id)
        if buf:
            last = buf.get_last_value()
            if last:
                _, val = last

                display_val = (val + entry.offset) * entry.scale
                self._value_label.setText(f"当前值: {display_val:.6g}")


        if self._looking and self._cursor_line is not None:
            look_time = self._cursor_line.value()
            if buf:
                count = buf.count
                if count > 0:
                    ts, vals = buf.get_latest(count)
                    t_rel = (ts - self._time_origin_ns) / 1_000_000_000

                    idx_arr = np.searchsorted(t_rel, look_time)
                    if idx_arr > 0 and idx_arr < len(t_rel):

                        if abs(t_rel[idx_arr] - look_time) < abs(t_rel[idx_arr - 1] - look_time):
                            raw_val = float(vals[idx_arr])
                        else:
                            raw_val = float(vals[idx_arr - 1])
                        display_val = (raw_val + entry.offset) * entry.scale
                        self._lookval_label.setText(f"查看值: {display_val:.6g}")
                    elif idx_arr == 0 and len(t_rel) > 0:
                        raw_val = float(vals[0])
                        display_val = (raw_val + entry.offset) * entry.scale
                        self._lookval_label.setText(f"查看值: {display_val:.6g}")
                    else:
                        self._lookval_label.setText("查看值: -")
        else:
            self._lookval_label.setText("查看值: -")


        self._update_cursor_labels()



    def eventFilter(self, obj, event: QEvent) -> bool:
        """拦截绘图区域的鼠标和滚轮事件，实现 LinkScope 风格交互"""
        if obj is not self._plot_widget.viewport():
            return super().eventFilter(obj, event)

        etype = event.type()


        if etype == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event
            key = key_event.key()
            if key == Qt.Key.Key_A:

                x = self._cursor_line.value() if self._cursor_line else self._last_mouse_x
                if x is not None:
                    self._place_cursor(x, 'a')
                return True
            elif key == Qt.Key.Key_B:

                x = self._cursor_line.value() if self._cursor_line else self._last_mouse_x
                if x is not None:
                    self._place_cursor(x, 'b')
                return True
            elif key == Qt.Key.Key_Escape:

                if self._trigger_state == TriggerState.LOCKED:
                    self._on_trigger_reset()
                else:
                    self._clear_cursors()
                return True
            elif key == Qt.Key.Key_Up:

                self._switch_selected_var(-1)
                return True
            elif key == Qt.Key.Key_Down:

                self._switch_selected_var(1)
                return True


        if etype == QEvent.Type.Wheel:
            wheel: QWheelEvent = event
            modifiers = wheel.modifiers()

            if modifiers & Qt.KeyboardModifier.ShiftModifier:

                delta = self._val_per_div * 0.01
                if wheel.angleDelta().y() > 0:
                    self._val_div_spin.setValue(max(0.001, self._val_per_div - delta))
                else:
                    self._val_div_spin.setValue(self._val_per_div + delta)
                return True

            elif modifiers & Qt.KeyboardModifier.ControlModifier:

                delta = self._sec_per_div * 0.01
                if wheel.angleDelta().y() > 0:
                    self._sec_div_spin.setValue(max(0.001, self._sec_per_div - delta))
                else:
                    self._sec_div_spin.setValue(min(30.0, self._sec_per_div + delta))
                return True

            else:

                dist = int(self._sec_per_div * 100)
                if wheel.angleDelta().y() > 0:
                    self._scrollbar.setValue(max(0, self._scrollbar.value() - dist))
                else:
                    self._scrollbar.setValue(min(
                        self._scrollbar.maximum(),
                        self._scrollbar.value() + dist
                    ))
                return True


        elif etype == QEvent.Type.MouseButtonPress:
            me: QMouseEvent = event
            if me.button() == Qt.MouseButton.LeftButton:

                scene_pos = self._plot_widget.mapToScene(me.pos())
                for item in self._plot_widget.scene().items(scene_pos):
                    if isinstance(item, pg.InfiniteLine) and item.movable:
                        return super().eventFilter(obj, event)
                self._looking = True
                self._update_cursor(me.position())
                return True


        elif etype == QEvent.Type.MouseMove:
            me: QMouseEvent = event

            if self._plot_widget.sceneBoundingRect().contains(me.position()):
                mouse_point = self._plot_widget.plotItem.vb.mapSceneToView(me.position())
                self._last_mouse_x = mouse_point.x()
            if self._looking:
                self._update_cursor(me.position())
                return True


        elif etype == QEvent.Type.MouseButtonRelease:
            me: QMouseEvent = event
            if me.button() == Qt.MouseButton.LeftButton and self._looking:
                self._looking = False
                if self._cursor_line:
                    self._plot_widget.removeItem(self._cursor_line)
                    self._cursor_line = None
                self._lookval_label.setText("查看值: -")
                return True

        return super().eventFilter(obj, event)

    def _update_cursor(self, pos) -> None:
        """更新临时游标线位置"""
        if self._plot_widget.sceneBoundingRect().contains(pos):
            mouse_point = self._plot_widget.plotItem.vb.mapSceneToView(pos)
            x = mouse_point.x()
            self._last_mouse_x = x

            if self._cursor_line:
                self._plot_widget.removeItem(self._cursor_line)
            self._cursor_line = pg.InfiniteLine(
                pos=x, angle=90,
                pen=pg.mkPen(QColor(184, 184, 184), width=2),
                movable=False,
            )
            self._plot_widget.addItem(self._cursor_line)
            self.cursor_position_changed.emit(x)

    def _clear_cursors(self) -> None:
        """清除所有游标。"""
        if self._cursor_a:
            self._plot_widget.removeItem(self._cursor_a)
            self._cursor_a = None
        if self._cursor_b:
            self._plot_widget.removeItem(self._cursor_b)
            self._cursor_b = None
        self._cursor_a_time = None
        self._cursor_b_time = None
        self._cursor_a_label.setText("游标A: -")
        self._cursor_b_label.setText("游标B: -")
        self._cursor_delta_label.setText("Δ: -")
        self._cursor_a_btn.setChecked(False)
        self._cursor_b_btn.setChecked(False)



    def _on_sec_div_changed(self, value: float) -> None:
        self._sec_per_div = value

        self._min_retrigger_ns = int(self._sec_per_div * HORI_DIV * 0.5e9)

    def _on_val_div_changed(self, value: float) -> None:
        self._val_per_div = value

    def _on_offset_changed(self, value: float) -> None:
        self._vert_offset = value

    def _on_realtime_toggled(self, checked: bool) -> None:
        self._realtime_update = checked
        self._scrollbar.setEnabled(not checked)

    def _on_scroll_changed(self, value: int) -> None:

        if self._realtime_update:
            self._realtime_check.setChecked(False)

    def _on_fit_clicked(self) -> None:
        """适应按钮：自动调整 sec/Div 和 val/Div 以显示所有数据"""
        if not self._entries:
            return

        min_val = float('inf')
        max_val = float('-inf')
        total_time = 0.0

        for entry in self._entries:
            if not entry.enabled:
                continue
            buf = self._buffer_manager.get_buffer(entry.buffer_id)
            if not buf:
                continue
            count = buf.count
            if count == 0:
                continue
            ts, vals = buf.get_latest(count)

            if entry.scale != 1.0 or entry.offset != 0.0:
                vals = (vals + entry.offset) * entry.scale
            if len(vals) > 0:
                min_val = min(min_val, float(np.min(vals)))
                max_val = max(max_val, float(np.max(vals)))
            if len(ts) > 0:
                t = float((ts[-1] - ts[0]) / 1_000_000_000)
                total_time = max(total_time, t)

        if min_val < max_val:
            val_range = max_val - min_val
            self._val_div_spin.setValue(val_range / VERT_DIV)
            self._offset_spin.setValue(-(max_val + min_val) / 2)

        if total_time > 0:
            self._sec_div_spin.setValue(total_time / HORI_DIV)
            self._realtime_check.setChecked(True)
