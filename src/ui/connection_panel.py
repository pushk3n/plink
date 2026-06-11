"""plink v3.0 - 连接配置面板

提供 pyOCD 探针选择、目标类型配置、ELF 文件选择等功能。
v3.0 移除 OpenOCD 配置，改为 pyOCD 直连。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QComboBox,
    QSpinBox,
    QMessageBox,
)

from ..core.data_types import ConnectionConfig, normalize_path

logger = logging.getLogger(__name__)


class ConnectionPanel(QWidget):
    """连接配置面板（v3.0 - pyOCD 直连）"""

    _CONFIG_DIR = Path.home() / ".plink"
    _CONFIG_FILE = _CONFIG_DIR / "last_config.json"

    # 信号
    connect_requested = pyqtSignal(ConnectionConfig)
    disconnect_requested = pyqtSignal()
    flash_requested = pyqtSignal()  # 烧录请求
    connection_status_changed = pyqtSignal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._config = ConnectionConfig()
        self._connected = False
        self._load_last_config()
        self._setup_ui()

    def _load_last_config(self) -> None:
        """加载上次保存的配置"""
        try:
            if self._CONFIG_FILE.exists():
                with open(self._CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # v3.0 字段
                self._config.probe_unique_id = data.get("probe_unique_id", "")
                self._config.target_override = data.get("target_override", "cortex_m")
                self._config.swd_frequency = data.get("swd_frequency", 8000000)
                self._config.elf_path = data.get("elf_path", "")
                # 兼容 v2.0 配置（忽略 OpenOCD 相关字段）
                logger.info("已加载上次配置: %s", self._CONFIG_FILE)
        except Exception as e:
            logger.warning("加载配置失败: %s", e)

    def _save_last_config(self) -> None:
        """保存当前配置"""
        try:
            self._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "probe_unique_id": self._config.probe_unique_id,
                "target_override": self._config.target_override,
                "swd_frequency": self._config.swd_frequency,
                "elf_path": self._config.elf_path,
            }
            with open(self._CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.debug("配置已保存: %s", self._CONFIG_FILE)
        except Exception as e:
            logger.warning("配置失败: %s", e)

    def _setup_ui(self) -> None:
        """设置 UI 布局"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # 探针配置组
        probe_group = QGroupBox("调试探针")
        probe_layout = QVBoxLayout()

        # 探针选择
        probe_row = QHBoxLayout()
        probe_row.addWidget(QLabel("探针:"))
        self._probe_combo = QComboBox()
        self._probe_combo.setMinimumWidth(200)
        probe_row.addWidget(self._probe_combo, 1)
        self._refresh_btn = QPushButton("刷新列表")
        self._refresh_btn.clicked.connect(self._refresh_probes)
        probe_row.addWidget(self._refresh_btn)
        probe_layout.addLayout(probe_row)

        # 目标类型（下拉列表）
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("目标:"))
        self._target_combo = QComboBox()
        self._target_combo.setEditable(True)
        self._target_combo.setMinimumWidth(200)
        # 通用目标 + 常用 STM32 系列
        common_targets = [
            ("cortex_m", "自动检测 (推荐)"),
            ("stm32f051", "STM32F051"),
            ("stm32f103rc", "STM32F103RC"),
            ("stm32f412xg", "STM32F412xG"),
            ("stm32f429xi", "STM32F429xI"),
            ("stm32f439xi", "STM32F439xI"),
            ("stm32f767zi", "STM32F767xx"),
            ("stm32h743xx", "STM32H743xx"),
            ("stm32h750xx", "STM32H750xx"),
            ("stm32l432kc", "STM32L432xC"),
            ("stm32l475xg", "STM32L475xG"),
        ]
        for target_id, label in common_targets:
            self._target_combo.addItem(f"{label} ({target_id})", target_id)
        # 设置当前值
        idx = next(
            (i for i, (tid, _) in enumerate(common_targets) if tid == self._config.target_override),
            0,
        )
        self._target_combo.setCurrentIndex(idx)
        target_row.addWidget(self._target_combo, 1)
        probe_layout.addLayout(target_row)

        # SWD 时钟频率
        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("时钟:"))
        self._freq_spin = QSpinBox()
        self._freq_spin.setRange(100000, 20000000)
        self._freq_spin.setSingleStep(1000000)
        self._freq_spin.setValue(self._config.swd_frequency)
        self._freq_spin.setSuffix(" Hz")
        freq_row.addWidget(self._freq_spin)
        probe_layout.addLayout(freq_row)

        probe_group.setLayout(probe_layout)
        layout.addWidget(probe_group)

        # ELF 文件配置组
        elf_group = QGroupBox("符号文件")
        elf_layout = QVBoxLayout()

        elf_path_layout = QHBoxLayout()
        elf_path_layout.addWidget(QLabel("ELF/AXF:"))
        self._elf_edit = QLineEdit(self._config.elf_path)
        elf_path_layout.addWidget(self._elf_edit, 1)
        elf_browse = QPushButton("浏览")
        elf_browse.clicked.connect(self._browse_elf)
        elf_path_layout.addWidget(elf_browse)
        elf_layout.addLayout(elf_path_layout)

        elf_group.setLayout(elf_layout)
        layout.addWidget(elf_group)

        # 连接/断开/烧录按钮
        btn_layout = QHBoxLayout()
        self._connect_btn = QPushButton("连接")
        self._connect_btn.clicked.connect(self._on_connect)
        btn_layout.addWidget(self._connect_btn)
        self._disconnect_btn = QPushButton("断开")
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        self._disconnect_btn.setEnabled(False)
        btn_layout.addWidget(self._disconnect_btn)
        self._flash_btn = QPushButton("烧录")
        self._flash_btn.setToolTip("将已加载的 ELF/AXF 固件烧录到目标 MCU")
        self._flash_btn.clicked.connect(self._on_flash)
        self._flash_btn.setEnabled(False)
        btn_layout.addWidget(self._flash_btn)
        layout.addLayout(btn_layout)

        # 进度状态标签
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self._progress_label)

        # 连接状态标签
        self._status_label = QLabel("未连接")
        layout.addWidget(self._status_label)

    def _browse_elf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ELF/AXF 文件", "",
            "ELF 文件 (*.elf);;AXF 文件 (*.axf);;所有文件 (*)"
        )
        if path:
            self._elf_edit.setText(path)

    def _refresh_probes(self) -> None:
        """刷新探针列表"""
        from ..core.pyocd_backend import PyOcdBackend
        self._probe_combo.clear()
        try:
            probes = PyOcdBackend.list_probes()
            for probe in probes:
                label = f"{probe.product_name} ({probe.unique_id[:12]}...)"
                self._probe_combo.addItem(label, probe.unique_id)
            if not probes:
                self._probe_combo.addItem("未找到探针", "")
                self._progress_label.setText("请连接调试探针到 USB 端口")
            else:
                self._progress_label.setText(f"找到 {len(probes)} 个探针")
        except Exception as e:
            self._probe_combo.addItem("枚举失败", "")
            self._progress_label.setText(f"探针枚举失败: {e}")

    def _on_connect(self) -> None:
        self._update_config()
        self._save_last_config()
        self.connect_requested.emit(self._config)

    def _on_disconnect(self) -> None:
        self.disconnect_requested.emit()

    def _on_flash(self) -> None:
        """烧录按钮点击：发出烧录请求信号。"""
        self.flash_requested.emit()

    def _update_config(self) -> None:
        """从 UI 更新配置"""
        self._config.probe_unique_id = self._probe_combo.currentData() or ""
        self._config.target_override = self._target_combo.currentData() or self._target_combo.currentText().strip() or "cortex_m"
        self._config.swd_frequency = self._freq_spin.value()
        self._config.elf_path = self._elf_edit.text().strip()

    def set_connected(self, connected: bool) -> None:
        """更新连接状态"""
        self._connected = connected
        self._connect_btn.setEnabled(not connected)
        self._disconnect_btn.setEnabled(connected)
        self._flash_btn.setEnabled(connected)
        self._probe_combo.setEnabled(not connected)
        self._refresh_btn.setEnabled(not connected)
        self._target_combo.setEnabled(not connected)
        self._freq_spin.setEnabled(not connected)
        self._elf_edit.setEnabled(not connected)
        self._status_label.setText("已连接" if connected else "未连接")
        if connected:
            self._progress_label.setText("")
        self.connection_status_changed.emit(connected)

    def set_progress(self, text: str) -> None:
        """更新进度状态标签（由 ConnectionWorker 信号调用）。"""
        self._progress_label.setText(text)

    def get_config(self) -> ConnectionConfig:
        self._update_config()
        return self._config
