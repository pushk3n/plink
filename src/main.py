"""plink v2.0 - 应用入口

嵌入式实时波形可视化工具，用于调试 STM32 C++ 工程。
支持 DAPLink / STLink / JLink 等调试探针，最高 2000Hz 采样率。
v2.0: 移除 GDB 依赖，使用 pyelftools 静态解析 ELF 符号。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from .ui.main_window import MainWindow


def setup_logging() -> None:
    """配置日志系统"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # 设置 pyqtgraph 日志级别
    logging.getLogger("pyqtgraph").setLevel(logging.WARNING)


def main() -> int:
    """主函数

    Returns:
        退出码
    """
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("plink 启动")

    # 创建 QApplication
    app = QApplication(sys.argv)
    app.setApplicationName("plink")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("plink")

    # 设置高 DPI 支持
    app.setStyle("Fusion")

    # 创建主窗口
    window = MainWindow()
    window.show()

    # 运行事件循环
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
