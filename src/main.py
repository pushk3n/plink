"""plink v2.0 - 应用入口

嵌入式实时波形可视化工具，用于调试 STM32 C++ 工程。
支持 DAPLink / STLink / JLink 等调试探针，采样率受变量布局、探针链路与绘图负载影响。
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


    logging.getLogger("pyqtgraph").setLevel(logging.WARNING)


def main() -> int:
    """主函数

    Returns:
        退出码
    """
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("plink 启动")


    app = QApplication(sys.argv)
    app.setApplicationName("plink")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("plink")


    app.setStyle("Fusion")


    window = MainWindow()
    window.show()


    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
