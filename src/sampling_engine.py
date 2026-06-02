"""plink v5.0 - 高速采样引擎

通过 pyOCD 直连 CMSIS-DAP 探针高速读取 MCU 内存数据，支持最高 2000Hz 采样率。
v5.0: 聚合读取引擎、写入互斥锁、连接异常分类与信号通知。

v3.0 变更：
- 替换 OpenOCD 为 pyOCD 直连 DAP，去除 TCP/Tcl 协议层
- 使用 deferred reads + flush 实现批量内存读取
- 预编译 struct.Struct，热路径零内存分配

v5.0 变更：
- 集成 _write_lock 与 PyOcdBackend 的聚合读取引擎
- 区分 pyOCD 异常类型（USBError/TransferError/TargetError）
- 新增 connection_lost 信号用于 UI 层连接状态指示
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Optional

from .core.data_types import (
    VarType,
    VariableInfo,
    current_timestamp_ns,
)
from .core.pyocd_backend import PyOcdBackend
from .ring_buffer import MultiChannelRingBuffer

logger = logging.getLogger(__name__)


_STRUCT_FMT_MAP: dict[VarType, str] = {
    VarType.F32: '<f',
    VarType.F64: '<d',
    VarType.I32: '<i',
    VarType.U32: '<I',
    VarType.I16: '<h',
    VarType.U16: '<H',
    VarType.I8:  '<b',
    VarType.U8:  '<B',
}


class SamplingEngine:
    """v5.0 高速采样引擎

    使用 pyOCD 聚合读取引擎批量内存读取，支持最高 2000Hz 采样率。
    热路径零内存分配：预编译 struct.Struct，使用预分配 RingBuffer。

    v5.0 新增：
    - _write_lock: 与 UI 线程的内存写入操作互斥
    - connection_lost 信号: 通知 UI 层连接异常类型

    典型用法：
        engine = SamplingEngine(backend, ring_buffer)
        engine.add_variable(var_info)
        engine.start(frequency=2000)
        ...
        engine.stop()
    """




    on_connection_lost: Optional[callable] = None

    def __init__(
        self,
        backend: PyOcdBackend,
        buffer_manager: MultiChannelRingBuffer,
    ):
        self._backend = backend
        self._buffer_manager = buffer_manager


        self._vars: list[VariableInfo] = []
        self._buffer_ids: list[int] = []
        self._vars_lock = threading.Lock()


        self._write_lock = threading.Lock()


        self._unpackers: list[struct.Struct] = []
        self._hot_path_dirty = True


        self._cached_vars: list[VariableInfo] = []
        self._cached_bids: list[int] = []
        self._cached_unpackers: list[struct.Struct] = []


        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._frequency = 1000
        self._interval_ns = 1_000_000_000 // self._frequency
        self._stopped_event = threading.Event()


        self._sample_count = 0
        self._error_count = 0
        self._consecutive_error_count = 0
        self._actual_frequency = 0.0
        self._last_error_log_ns = 0
        self._read_timeout_s = 0.2


        self._connection_state = "disconnected"



    def add_variable(self, entry_or_var, buffer_id: int = -1) -> int:
        """添加变量到监控列表。无需 halt MCU，立即生效。

        Args:
            entry_or_var: VariableInfo 或 VarWatchEntry 对象
            buffer_id: 缓冲区 ID，-1 表示自动分配

        Returns:
            分配的缓冲区 ID
        """

        if hasattr(entry_or_var, 'var_info'):
            var = entry_or_var.var_info
            if buffer_id < 0:
                buffer_id = entry_or_var.buffer_id if entry_or_var.buffer_id >= 0 else \
                    self._buffer_manager.allocate()
        else:
            var = entry_or_var
            if buffer_id < 0:
                buffer_id = self._buffer_manager.allocate()

        with self._vars_lock:

            if any(v.address == var.address for v in self._vars):
                return buffer_id
            self._vars.append(var)
            self._buffer_ids.append(buffer_id)
            self._hot_path_dirty = True

        logger.info("添加变量: %s @ 0x%08X (通道: %d)", var.name, var.address, buffer_id)
        return buffer_id

    def remove_variable(self, name_or_buffer_id) -> None:
        """从监控列表移除变量。"""
        with self._vars_lock:
            if isinstance(name_or_buffer_id, int):

                idx = None
                for i, bid in enumerate(self._buffer_ids):
                    if bid == name_or_buffer_id:
                        idx = i
                        break
                if idx is not None:
                    self._buffer_manager.release(self._buffer_ids[idx])
                    self._vars.pop(idx)
                    self._buffer_ids.pop(idx)
                    self._hot_path_dirty = True
            else:

                for i, v in enumerate(self._vars):
                    if v.name == name_or_buffer_id:
                        self._buffer_manager.release(self._buffer_ids[i])
                        self._vars.pop(i)
                        self._buffer_ids.pop(i)
                        self._hot_path_dirty = True
                        break

    def clear_variables(self) -> None:
        """清空所有监控变量。"""
        with self._vars_lock:
            for bid in self._buffer_ids:
                self._buffer_manager.release(bid)
            self._vars.clear()
            self._buffer_ids.clear()
            self._hot_path_dirty = True

    def get_var_count(self) -> int:
        """获取当前监控变量数量。"""
        with self._vars_lock:
            return len(self._vars)



    def set_frequency(self, hz: int) -> None:
        """设置采样频率，范围 1~2000Hz。"""
        hz = max(1, min(2000, hz))
        self._interval_ns = int(1_000_000_000 / hz)
        self._frequency = hz

    def start(self, frequency: int = 0) -> None:
        """启动采样线程。"""
        if self._running:
            return
        if self._thread and self._thread.is_alive():
            logger.warning("采样线程尚未退出，忽略重复启动请求")
            return
        if frequency > 0:
            self.set_frequency(frequency)

        self._running = True
        self._stopped_event.clear()
        self._sample_count = 0
        self._error_count = 0
        self._consecutive_error_count = 0
        self._last_error_log_ns = 0
        self._connection_state = "connected"

        self._thread = threading.Thread(
            target=self._sample_loop,
            daemon=True,
            name="sampling-engine",
        )
        self._thread.start()
        logger.info("采样引擎已启动: %d Hz", self._frequency)

    def stop(self, timeout: float = 2.0) -> None:
        """停止采样线程，等待线程退出。"""
        self._running = False
        thread = self._thread
        if thread and thread.is_alive():
            stopped = self._stopped_event.wait(timeout=timeout)
            if not stopped:
                thread.join(timeout=timeout)
        if thread and thread.is_alive():
            logger.warning("采样线程未在 %.1f 秒内退出", timeout)
            return
        self._thread = None
        logger.info("采样引擎已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def actual_frequency(self) -> float:
        return self._actual_frequency

    @property
    def connection_state(self) -> str:
        """连接状态: "connected", "reconnecting", "disconnected" """
        return self._connection_state

    def get_write_lock(self) -> threading.Lock:
        """获取写入互斥锁，供 UI 线程调用 write_variable 时使用。"""
        return self._write_lock



    def _rebuild_hot_path(self) -> None:
        """当变量列表发生变化时调用。预编译 struct.Struct 并缓存快照。"""
        with self._vars_lock:
            self._unpackers = [
                struct.Struct(_STRUCT_FMT_MAP.get(v.var_type, '<f'))
                for v in self._vars
            ]
            self._cached_vars = list(self._vars)
            self._cached_bids = list(self._buffer_ids)
            self._cached_unpackers = list(self._unpackers)
        self._hot_path_dirty = False



    def _classify_error(self, exc: Exception) -> tuple[str, str]:
        """分类 pyOCD 异常，返回 (error_type, message)。

        error_type:
        - "usb_disconnect": USB 断开，需手动重连
        - "swd_timeout": SWD 通信超时，可自动重试
        - "target_reset": 目标复位，可自动重连
        - "address_error": 地址访问错误（如 ELF 不匹配）
        - "unknown": 未知异常
        """
        exc_str = str(exc).lower()
        exc_type = type(exc).__name__


        if 'usb' in exc_str or 'device not found' in exc_str or 'entity not found' in exc_str:
            return "usb_disconnect", f"USB 连接断开: {exc}"


        if 'transfer' in exc_type.lower() or 'timeout' in exc_str or 'swd' in exc_str:
            return "swd_timeout", f"SWD 通信异常: {exc}"


        if 'target' in exc_type.lower() or 'target' in exc_str:
            return "target_reset", f"目标已复位: {exc}"


        if 'address' in exc_type.lower() or 'address' in exc_str or 'fault' in exc_str:
            return "address_error", f"地址访问错误: {exc}"

        return "unknown", f"未知异常: {exc}"

    def _notify_connection_lost(self, error_type: str, message: str) -> None:
        """通知 UI 层连接异常。"""
        self._connection_state = "disconnected" if error_type == "usb_disconnect" else "reconnecting"
        if self.on_connection_lost:
            try:
                self.on_connection_lost(error_type, message)
            except Exception:
                pass



    def _sample_loop(self) -> None:
        """2000Hz 采样主循环。必须在独立线程中运行。"""
        logger.info("采样循环开始")

        if not self._backend.is_connected:
            logger.error("pyOCD 未连接")
            self._running = False
            self._stopped_event.set()
            return


        stats_interval = 1.0
        cycle_count = 0
        last_stats = time.perf_counter()

        interval = self._interval_ns
        next_tick = time.perf_counter_ns() + interval

        while self._running:
            try:

                if self._hot_path_dirty:
                    self._rebuild_hot_path()
                    interval = self._interval_ns


                vars_snapshot = self._cached_vars
                bids_snapshot = self._cached_bids
                unpackers_snapshot = self._cached_unpackers

                if not vars_snapshot:
                    time.sleep(0.01)
                    continue


                with self._write_lock:
                    raw_list = self._backend.batch_read_variables(
                        vars_snapshot,
                        timeout=self._read_timeout_s,
                    )


                ts = current_timestamp_ns()
                wrote_any = False
                for i in range(len(vars_snapshot)):
                    raw = raw_list[i]
                    if raw is not None and len(raw) >= unpackers_snapshot[i].size:
                        try:
                            val = unpackers_snapshot[i].unpack(raw)[0]
                            self._buffer_manager.append(bids_snapshot[i], ts, float(val))
                            wrote_any = True
                        except struct.error:
                            pass


                if wrote_any:
                    self._sample_count += 1
                    cycle_count += 1
                    self._consecutive_error_count = 0
                    if self._connection_state != "connected":
                        self._connection_state = "connected"
                else:

                    self._consecutive_error_count += 1
                    self._error_count += 1
                    if self._consecutive_error_count >= 5:
                        logger.warning("连续 %d 次读取失败，尝试重连 pyOCD",
                                       self._consecutive_error_count)
                        self._connection_state = "reconnecting"
                        if self.on_connection_lost:
                            self.on_connection_lost("swd_timeout",
                                                    f"连续 {self._consecutive_error_count} 次读取失败")
                        self._backend.reconnect()
                        self._consecutive_error_count = 0
                    time.sleep(min(self._consecutive_error_count * 0.01, 0.1))
                    next_tick = time.perf_counter_ns() + interval
                    continue


                now = time.perf_counter()
                if now - last_stats >= stats_interval:
                    self._actual_frequency = cycle_count / (now - last_stats)
                    cycle_count = 0
                    last_stats = now


                self._busy_wait_until(next_tick)
                next_tick += interval


                now_ns = time.perf_counter_ns()
                if next_tick < now_ns - interval * 2:
                    next_tick = now_ns + interval

            except Exception as e:
                self._error_count += 1
                self._consecutive_error_count += 1


                error_type, message = self._classify_error(e)


                if error_type == "usb_disconnect":
                    logger.error("USB 断开，停止采样: %s", e)
                    self._notify_connection_lost(error_type, message)
                    self._running = False
                    break


                if error_type == "swd_timeout":
                    if self._consecutive_error_count >= 5:
                        self._notify_connection_lost(error_type, message)


                if error_type == "target_reset":
                    logger.warning("目标复位，尝试重连: %s", e)
                    self._notify_connection_lost(error_type, message)
                    self._backend.reconnect()
                    self._consecutive_error_count = 0

                now_log_ns = time.perf_counter_ns()
                if (
                    self._last_error_log_ns == 0
                    or now_log_ns - self._last_error_log_ns >= 1_000_000_000
                ):
                    logger.warning(
                        "采样异常: %s (连续错误: %d, 类型: %s)",
                        e,
                        self._consecutive_error_count,
                        error_type,
                    )
                    self._last_error_log_ns = now_log_ns


                time.sleep(min(interval / 1e9, 0.05))
                next_tick = time.perf_counter_ns() + interval

        logger.info("采样循环结束 (采样: %d, 错误: %d)",
                     self._sample_count, self._error_count)
        self._stopped_event.set()

    def _busy_wait_until(self, target_ns: int) -> None:
        """高精度忙等待。先 sleep 大部分时间，最后 busy-wait 微调。"""
        remaining = target_ns - time.perf_counter_ns()

        if remaining > 2_000_000:
            time.sleep((remaining - 1_000_000) / 1e9)
        while time.perf_counter_ns() < target_ns:
            pass
