"""plink v5.0 - pyOCD 内存读取后端

替代 openocd_client.py，通过 pyOCD 直连 CMSIS-DAP/ST-Link 探针读取 MCU 内存。
v5.0: 使用 32 位对齐聚合读取引擎替代 deferred reads，减少 USB 往返次数。

优势：
- 去掉 OpenOCD 中间层，直连 DAP 协议
- 聚合读取：相邻变量合并为单次 read_memory_block32 调用
- CMSIS-DAP V2 下可达 800-1000Hz 稳定采样
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .data_types import VariableInfo

logger = logging.getLogger(__name__)


_RAM_BASE = 0x20000000


_BLOCK_MERGE_GAP = 64


class PyOcdError(Exception):
    """pyOCD 通信异常"""
    pass


class WriteError(Exception):
    """内存写入异常"""
    pass


class PyOcdBackend:
    """pyOCD 内存读取后端

    替代 OpenOCDClient，通过 pyOCD 直连 CMSIS-DAP 探针读取 MCU 内存。
    v5.0 使用 32 位对齐聚合读取引擎，将相邻变量合并为单次块读取。

    典型用法：
        backend = PyOcdBackend()
        probes = backend.list_probes()
        backend.connect(probes[0].unique_id, target_override="stm32f4")
        value = backend.read32(0x20000070)
        backend.disconnect()
    """

    def __init__(self):
        self._session = None
        self._target = None
        self._connected = False
        self._lock = threading.Lock()



    @staticmethod
    def list_probes() -> list:
        """枚举所有已连接的调试探针。

        Returns:
            pyOCD DebugProbe 对象列表，每个包含 description, unique_id 等属性。
        """
        from pyocd.core.helpers import ConnectHelper
        try:
            return ConnectHelper.get_all_connected_probes(blocking=False)
        except Exception as e:
            logger.warning("枚举探针失败: %s", e)
            return []



    def connect(
        self,
        unique_id: str = "",
        target_override: str = "cortex_m",
        frequency: int = 8000000,
        connect_mode: str = "attach",
    ) -> None:
        """连接到调试探针并 attach 到目标 MCU。

        Args:
            unique_id: 探针唯一 ID（空字符串表示自动选择第一个）
            target_override: 目标类型（如 "stm32f4", "cortex_m"）
            frequency: SWD/JTAG 时钟频率（Hz），默认 8MHz
            connect_mode: 连接模式
                - "attach": 附加到运行中的目标（不暂停）← 默认
                - "halt": 连接并暂停目标
                - "under-reset": 复位状态下连接

        Raises:
            PyOcdError: 连接失败
        """
        if self._connected:
            return

        from pyocd.core.helpers import ConnectHelper
        from pyocd.core.session import Session

        try:

            if unique_id:

                probe = None
                for p in self.list_probes():
                    if unique_id in p.unique_id:
                        probe = p
                        break
                if probe is None:
                    raise PyOcdError(f"未找到探针: {unique_id}")
            else:
                probes = self.list_probes()
                if not probes:
                    raise PyOcdError("未找到调试探针，请检查 USB 连接")
                probe = probes[0]

            self._session = Session(
                probe,
                target_override=target_override,
                frequency=frequency,
                connect_mode=connect_mode,
                resume_on_disconnect=True,
            )
            self._session.open()


            soc_target = self._session.target
            core = None
            if hasattr(soc_target, 'selected_core') and soc_target.selected_core:
                core = soc_target.selected_core
            elif hasattr(soc_target, 'cores') and soc_target.cores:
                core = list(soc_target.cores.values())[0]

            self._target = core if core is not None else soc_target
            self._connected = True
            logger.info("pyOCD 已连接: %s (核心: %s, 模式: %s, 时钟: %d Hz)",
                        self._session.probe.description,
                        type(self._target).__name__,
                        connect_mode, frequency)

        except Exception as e:
            self._connected = False
            self._target = None
            if self._session:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
            raise PyOcdError(f"pyOCD 连接失败: {e}") from e

    def disconnect(self) -> None:
        """断开连接，释放探针。目标 MCU 继续运行（resume_on_disconnect=True）。"""
        self._connected = False
        self._target = None
        if self._session:
            try:
                self._session.close()
            except Exception as e:
                logger.debug("pyOCD 断开时异常: %s", e)
            self._session = None
        logger.info("pyOCD 已断开")

    def reconnect(self) -> bool:
        """重新连接。采样过程中出错时调用。

        Returns:
            True 表示重连成功，False 表示失败。
        """

        probe_id = ""
        target = "cortex_m"
        freq = 8000000
        if self._session:
            try:
                probe_id = self._session.probe.unique_id if self._session.probe else ""
                try:
                    target = self._session.options.get("target_override")
                except KeyError:
                    pass
                try:
                    freq = self._session.options.get("frequency")
                except KeyError:
                    pass
            except Exception:
                pass

        try:
            self.disconnect()
            self.connect(
                unique_id=probe_id,
                target_override=target,
                frequency=freq,
                connect_mode="attach",
            )
            logger.info("pyOCD 重连成功")
            return True
        except PyOcdError as e:
            logger.warning("pyOCD 重连失败: %s", e)
            return False

    @property
    def is_connected(self) -> bool:
        """检查连接状态。"""
        return self._connected and self._target is not None

    @property
    def probe_name(self) -> str:
        """获取当前连接的探针名称。"""
        if self._session and self._session.probe:
            return self._session.probe.description
        return "未连接"

    @property
    def session_frequency(self) -> int:
        """获取当前连接的 SWD 频率。"""
        if self._session:
            try:
                return self._session.options.get("frequency")
            except KeyError:
                return 0
        return 0



    def read32(self, address: int) -> int:
        """读取 32 位整数。目标运行中可读。"""
        if not self._connected or not self._target:
            raise PyOcdError("未连接到探针")
        try:
            return self._target.read32(address)
        except Exception as e:
            raise PyOcdError(f"读取 0x{address:08X} 失败: {e}") from e

    def read_memory(self, address: int, size: int) -> bytes:
        """读取原始内存数据（小端序）。目标运行中可读。"""
        if not self._connected or not self._target:
            raise PyOcdError("未连接到探针")
        try:
            data = self._target.read_memory_block8(address, size)
            return bytes(data)
        except Exception as e:
            raise PyOcdError(f"读取 0x{address:08X} ({size}B) 失败: {e}") from e

    def batch_read_variables(
        self,
        variables: list,
        timeout: float = 0.2,
    ) -> list[Optional[bytes]]:
        """批量读取多个变量的内存数据（v5.0 聚合读取引擎）。

        v5.0 变更：使用 32 位对齐聚合读取替代 deferred reads。
        将相邻变量合并为单次 read_memory_block32 调用，减少 USB 往返次数。

        算法：
        1. 地址排序：按 address 升序，保留原始索引映射
        2. 自动聚类：相邻变量间距 < 64 字节合并为同一 Block
        3. 强制对齐：起始地址向下对齐 4，结束地址向上对齐 4
        4. 单次拉取：每个 Block 调用 read_memory_block32(start, word_count)
        5. 解包分发：words → bytearray → 按偏移切片 → bytes

        边界条件：
        - size > 8 或 VarType.UNKNOWN: 独立 read_memory_block8
        - 仅 1 个变量: 直接 read32/16/8，不走块读取
        - Block 跨 Flash 地址 (< 0x20000000): 退化为独立读取

        Args:
            variables: VariableInfo 对象列表
            timeout: 超时时间（秒），保留接口兼容

        Returns:
            每个变量对应一个 bytes 对象，解析失败时对应位置为 None。
        """
        if not self._connected or not self._target:
            return [None] * len(variables)
        if not variables:
            return []

        n = len(variables)


        if n == 1:
            return self._single_read(variables[0])

        with self._lock:
            try:
                return self._aggregated_read(variables)
            except Exception as e:
                logger.warning("pyOCD 批量读取失败: %s", e)
                return [None] * n

    def _single_read(self, var) -> list[Optional[bytes]]:
        """单变量快速读取路径。"""
        try:
            target = self._target
            size = var.size
            if size == 4:
                val = target.read32(var.address)
                return [val.to_bytes(4, 'little')]
            elif size == 2:
                val = target.read16(var.address)
                return [val.to_bytes(2, 'little')]
            elif size == 1:
                val = target.read8(var.address)
                return [val.to_bytes(1, 'little')]
            elif size == 8:
                lo = target.read32(var.address)
                hi = target.read32(var.address + 4)
                return [lo.to_bytes(4, 'little') + hi.to_bytes(4, 'little')]
            else:
                data = target.read_memory_block8(var.address, size)
                return [bytes(data)]
        except Exception:
            return [None]

    def _aggregated_read(self, variables: list) -> list[Optional[bytes]]:
        """聚合读取主逻辑。"""
        target = self._target
        n = len(variables)


        indexed = [(i, v) for i, v in enumerate(variables)]
        indexed.sort(key=lambda x: x[1].address)



        agg_items: list[tuple[int, object]] = []
        solo_items: list[tuple[int, object]] = []

        for orig_idx, var in indexed:
            if var.size > 8 or var.var_type.value == 'unknown' or var.address < _RAM_BASE:
                solo_items.append((orig_idx, var))
            else:
                agg_items.append((orig_idx, var))

        results: list[Optional[bytes]] = [None] * n


        if agg_items:
            blocks = self._cluster_blocks(agg_items)
            for block in blocks:
                self._read_block(block, results)


        for orig_idx, var in solo_items:
            try:
                data = target.read_memory_block8(var.address, var.size)
                results[orig_idx] = bytes(data)
            except Exception:
                results[orig_idx] = None

        return results

    def _cluster_blocks(
        self, items: list[tuple[int, object]]
    ) -> list[list[tuple[int, object]]]:
        """将按地址排序的变量聚类为 Block。

        相邻变量首尾间距 < _BLOCK_MERGE_GAP 时合并为同一 Block。
        """
        if not items:
            return []

        blocks: list[list[tuple[int, object]]] = [[items[0]]]
        prev_end = items[0][1].address + items[0][1].size

        for item in items[1:]:
            var = item[1]
            gap = var.address - prev_end
            if gap < _BLOCK_MERGE_GAP:

                blocks[-1].append(item)
            else:

                blocks.append([item])
            prev_end = var.address + var.size

        return blocks

    def _read_block(
        self,
        block: list[tuple[int, object]],
        results: list[Optional[bytes]],
    ) -> None:
        """读取一个聚合 Block 并分发结果到 results 数组。"""
        target = self._target


        block_start = block[0][1].address
        block_end = block[-1][1].address + block[-1][1].size
        aligned_start = block_start & ~3
        aligned_end = (block_end + 3) & ~3
        word_count = (aligned_end - aligned_start) // 4

        try:

            words = target.read_memory_block32(aligned_start, word_count)

            block_bytes = bytearray()
            for w in words:
                block_bytes.extend(w.to_bytes(4, 'little'))


            for orig_idx, var in block:
                offset = var.address - aligned_start
                var_bytes = bytes(block_bytes[offset:offset + var.size])
                results[orig_idx] = var_bytes

        except Exception:

            for orig_idx, var in block:
                try:
                    data = target.read_memory_block8(var.address, var.size)
                    results[orig_idx] = bytes(data)
                except Exception:
                    results[orig_idx] = None



    def write_variable(self, var: 'VariableInfo', raw_int_value: int) -> None:
        """按变量 size 写入整数值。

        调用方负责 halted/running 状态判断和并发保护（通过 _write_lock）。

        Args:
            var: 变量信息
            raw_int_value: 要写入的整数值

        Raises:
            WriteError: 写入失败
        """
        if not self._connected or not self._target:
            raise WriteError("未连接到探针")

        try:
            target = self._target
            if var.size == 4:
                target.write32(var.address, raw_int_value & 0xFFFFFFFF)
            elif var.size == 2:
                target.write16(var.address, raw_int_value & 0xFFFF)
            elif var.size == 1:
                target.write8(var.address, raw_int_value & 0xFF)
            else:

                data = raw_int_value.to_bytes(var.size, 'little')
                target.write_memory_block8(var.address, list(data))
            target.flush()
        except Exception as e:
            raise WriteError(f"写入 {var.name} @ 0x{var.address:08X} 失败: {e}") from e



    def halt(self) -> bool:
        """暂停目标 MCU。"""
        if not self._connected or not self._target:
            return False
        try:
            self._target.halt()
            return True
        except Exception as e:
            logger.warning("halt 失败: %s", e)
            return False

    def resume(self) -> bool:
        """恢复目标 MCU 运行。"""
        if not self._connected or not self._target:
            return False
        try:
            self._target.resume()
            return True
        except Exception as e:
            logger.warning("resume 失败: %s", e)
            return False

    def reset_and_run(self) -> bool:
        """复位 MCU 并立即运行。

        使用 SYSRESETREQ 触发完整系统复位，确保启动代码重新执行
        （.bss 清零、.data 从 Flash 重新加载）。
        """
        if not self._connected or not self._target:
            return False
        try:
            self._reset_with_sysresetreq(halt_after=False)
            return True
        except Exception as e:
            logger.warning("reset 失败: %s", e)
            return False

    def reset_halt(self) -> bool:
        """复位 MCU 并保持暂停状态（用于精确控制启动时刻）。

        使用 SYSRESETREQ 触发完整系统复位，确保启动代码重新执行。
        """
        if not self._connected or not self._target:
            return False
        try:
            self._reset_with_sysresetreq(halt_after=True)
            return True
        except Exception as e:
            logger.warning("reset_halt 失败: %s", e)
            return False


    _AIRCR_ADDR = 0xE000ED0C

    _AIRCR_SYSRESETREQ = 0x05FA0004


    _DEMCR_ADDR = 0xE000EDFC

    _DEMCR_VC_CORERESET = 0x00000001

    def _reset_with_sysresetreq(self, halt_after: bool = False) -> None:
        """执行完整系统复位，确保启动代码重新执行（.bss 清零、.data 重加载）。

        策略 1（首选）：pyOCD 标准 API — reset_and_halt() / reset()
        策略 2（兜底）：直接操作 ARM 核心寄存器 — AIRCR + DEMCR VC_CORERESET
        """
        target = self._target


        try:
            reset_type = getattr(target, 'ResetType', None)
            if reset_type is not None:
                reset_type = getattr(reset_type, 'SW_SYSTEM', None)

            if halt_after:
                target.reset_and_halt(reset_type=reset_type)
            else:
                target.reset(reset_type=reset_type)

            logger.debug("pyOCD API 复位成功 (halt_after=%s)", halt_after)
            return
        except Exception as e:
            logger.debug("pyOCD API 复位失败 (%s)，回退到寄存器操作", e)


        try:
            saved_demcr = None
            if halt_after:


                saved_demcr = target.read32(self._DEMCR_ADDR)
                target.write32(self._DEMCR_ADDR, saved_demcr | self._DEMCR_VC_CORERESET)


            target.write32(self._AIRCR_ADDR, self._AIRCR_SYSRESETREQ)

            if halt_after:

                target.wait_halted()

                target.write32(self._DEMCR_ADDR, saved_demcr)

            logger.debug("AIRCR 寄存器复位成功 (halt_after=%s)", halt_after)
        except Exception as e:
            logger.error("寄存器复位失败: %s", e)
            raise
