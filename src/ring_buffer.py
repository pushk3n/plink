"""plink - 线程安全环形缓冲区

高性能环形缓冲区，用于暂存采样引擎捞上来的数据，供 UI 线程消费。
支持 2000Hz 采样率下的高频写入，使用 numpy 数组实现零拷贝读取。
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np


class RingBuffer:
    """线程安全环形缓冲区

    固定容量的环形缓冲区，使用 numpy 数组存储时间戳和值。
    支持高频写入 (2000Hz) 和批量读取，用于解耦采样线程和 UI 线程。

    典型用法：
        buf = RingBuffer(capacity=20000)
        buf.append(time_ns, value)
        timestamps, values = buf.get_latest(1000)
    """

    def __init__(self, capacity: int = 20000):
        """初始化环形缓冲区

        Args:
            capacity: 缓冲区容量 (默认 20000 点 = 2000Hz × 10秒)
        """
        self._capacity = capacity
        self._timestamps = np.zeros(capacity, dtype=np.int64)
        self._values = np.zeros(capacity, dtype=np.float64)
        self._head = 0        # 写入位置
        self._count = 0       # 已写入数量
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """缓冲区容量"""
        return self._capacity

    @property
    def count(self) -> int:
        """当前数据点数量（GIL 保护 int 读取原子性）"""
        return self._count

    @property
    def is_full(self) -> bool:
        """缓冲区是否已满"""
        return self._count >= self._capacity

    def append(self, timestamp_ns: int, value: float) -> None:
        """添加一个数据点

        单写者场景下去锁：GIL 保护 numpy 标量赋值和 Python int 更新的原子性。
        UI 线程偶尔读到中间状态（时间戳/值不配对）对波形显示可接受。

        Args:
            timestamp_ns: 纳秒级时间戳
            value: 采样值
        """
        self._timestamps[self._head] = timestamp_ns
        self._values[self._head] = value
        self._head = (self._head + 1) % self._capacity
        if self._count < self._capacity:
            self._count += 1

    def append_batch(
        self, timestamps_ns: np.ndarray, values: np.ndarray
    ) -> None:
        """批量添加数据点

        Args:
            timestamps_ns: 纳秒级时间戳数组
            values: 采样值数组
        """
        n = len(timestamps_ns)
        if n == 0:
            return

        with self._lock:
            # 计算写入范围
            available = self._capacity - self._count
            if n > available:
                # 数据超出容量，只保留最新的
                overflow = n - available
                timestamps_ns = timestamps_ns[overflow:]
                values = values[overflow:]
                n = len(timestamps_ns)

            # 分段写入 (可能需要绕回)
            end = self._head + n
            if end <= self._capacity:
                self._timestamps[self._head:end] = timestamps_ns
                self._values[self._head:end] = values
            else:
                # 绕回写入
                first_part = self._capacity - self._head
                self._timestamps[self._head:] = timestamps_ns[:first_part]
                self._values[self._head:] = values[:first_part]
                remaining = n - first_part
                self._timestamps[:remaining] = timestamps_ns[first_part:]
                self._values[:remaining] = values[first_part:]

            self._head = (self._head + n) % self._capacity
            self._count = min(self._count + n, self._capacity)

    def get_latest(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """获取最新的 n 个数据点（返回 view，避免拷贝）

        Args:
            n: 要获取的数据点数量

        Returns:
            (timestamps, values) 元组，按时间顺序排列。数据为 numpy view，不要长期持有。
        """
        count = self._count
        n = min(n, count)
        if n == 0:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float64)

        head = self._head
        if count < self._capacity:
            # 缓冲区未满，数据连续
            start = head - n
            if start < 0:
                start = 0
            return self._timestamps[start:head], self._values[start:head]
        else:
            # 缓冲区已满，可能需要绕回
            start = (head - n) % self._capacity
            if start < head:
                return self._timestamps[start:head], self._values[start:head]
            else:
                # 绕回情况（必须 concatenate，无法避免拷贝）
                ts = np.concatenate([
                    self._timestamps[start:],
                    self._timestamps[:head]
                ])
                vals = np.concatenate([
                    self._values[start:],
                    self._values[:head]
                ])
                return ts, vals

    def get_all(self) -> tuple[np.ndarray, np.ndarray]:
        """获取所有数据点

        Returns:
            (timestamps, values) 元组，按时间顺序排列
        """
        return self.get_latest(self._count)

    def get_since(
        self, timestamp_ns: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """获取指定时间戳之后的所有数据点（view + 布尔索引）

        Args:
            timestamp_ns: 起始时间戳 (纳秒)

        Returns:
            (timestamps, values) 元组
        """
        count = self._count
        if count == 0:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float64)

        # 获取有序数据（view 或 concatenate）
        if count < self._capacity:
            ts = self._timestamps[:count]
            vals = self._values[:count]
        else:
            head = self._head
            ts = np.concatenate([self._timestamps[head:], self._timestamps[:head]])
            vals = np.concatenate([self._values[head:], self._values[:head]])

        # 布尔索引过滤（返回拷贝，因为后续写入可能覆盖）
        mask = ts >= timestamp_ns
        return ts[mask].copy(), vals[mask].copy()

    def get_range(
        self, start_ns: int, end_ns: int
    ) -> tuple[np.ndarray, np.ndarray]:
        """获取指定时间范围内的数据点。

        使用 np.searchsorted 二分查找，O(log N) 复杂度。
        若范围跨越环形缓冲区 _head，返回 concatenate 结果（拷贝）；
        否则返回 slice 视图（零拷贝）。

        Args:
            start_ns: 起始时间戳（纳秒，包含）
            end_ns: 结束时间戳（纳秒，包含）

        Returns:
            (timestamps, values) 元组，按时间顺序排列
        """
        count = self._count
        if count == 0:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float64)

        # 获取有序数据
        if count < self._capacity:
            # 数据连续，直接用 view
            ts = self._timestamps[:count]
            vals = self._values[:count]
        else:
            # 数据可能绕回，需要重排
            head = self._head
            ts = np.concatenate([self._timestamps[head:], self._timestamps[:head]])
            vals = np.concatenate([self._values[head:], self._values[:head]])

        # 二分查找范围
        idx_start = np.searchsorted(ts, start_ns, side='left')
        idx_end = np.searchsorted(ts, end_ns, side='right')

        if idx_start >= idx_end:
            return np.array([], dtype=np.int64), np.array([], dtype=np.float64)

        return ts[idx_start:idx_end], vals[idx_start:idx_end]

    def get_last_value(self) -> Optional[tuple[int, float]]:
        """获取最新的一个数据点

        Returns:
            (timestamp_ns, value) 元组，或 None 如果缓冲区为空
        """
        with self._lock:
            if self._count == 0:
                return None
            idx = (self._head - 1) % self._capacity
            return int(self._timestamps[idx]), float(self._values[idx])

    def clear(self) -> None:
        """清空缓冲区"""
        with self._lock:
            self._head = 0
            self._count = 0

    def get_stats(self) -> dict:
        """获取缓冲区统计信息

        Returns:
            包含 count, capacity, min_value, max_value 等统计信息的字典
        """
        ts, vals = self.get_all()
        if len(vals) == 0:
            return {
                "count": 0,
                "capacity": self._capacity,
                "min_value": 0.0,
                "max_value": 0.0,
                "mean_value": 0.0,
            }
        return {
            "count": len(vals),
            "capacity": self._capacity,
            "min_value": float(np.min(vals)),
            "max_value": float(np.max(vals)),
            "mean_value": float(np.mean(vals)),
        }


class MultiChannelRingBuffer:
    """多通道环形缓冲区管理器

    管理多个独立的 RingBuffer 实例，每个变量对应一个通道。
    """

    def __init__(self, capacity: int = 20000):
        """初始化多通道缓冲区

        Args:
            capacity: 每个通道的缓冲区容量
        """
        self._capacity = capacity
        self._buffers: dict[int, RingBuffer] = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def allocate(self) -> int:
        """分配一个新的通道

        Returns:
            通道 ID
        """
        with self._lock:
            channel_id = self._next_id
            self._next_id += 1
            self._buffers[channel_id] = RingBuffer(self._capacity)
            return channel_id

    def release(self, channel_id: int) -> None:
        """释放一个通道

        Args:
            channel_id: 通道 ID
        """
        with self._lock:
            self._buffers.pop(channel_id, None)

    def get_buffer(self, channel_id: int) -> Optional[RingBuffer]:
        """获取指定通道的缓冲区

        Args:
            channel_id: 通道 ID

        Returns:
            RingBuffer 实例，或 None 如果通道不存在
        """
        with self._lock:
            return self._buffers.get(channel_id)

    def append(self, channel_id: int, timestamp_ns: int, value: float) -> None:
        """向指定通道添加数据点（直接查找，避免 get_buffer 的锁开销）

        Args:
            channel_id: 通道 ID
            timestamp_ns: 时间戳
            value: 采样值
        """
        buf = self._buffers.get(channel_id)
        if buf:
            buf.append(timestamp_ns, value)

    def clear_all(self) -> None:
        """清空所有通道"""
        with self._lock:
            for buf in self._buffers.values():
                buf.clear()

    def get_all_stats(self) -> dict[int, dict]:
        """获取所有通道的统计信息

        Returns:
            {channel_id: stats} 字典
        """
        with self._lock:
            return {
                cid: buf.get_stats()
                for cid, buf in self._buffers.items()
            }
