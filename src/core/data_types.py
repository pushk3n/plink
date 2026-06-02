"""plink - 数据类型定义模块

定义系统中使用的核心数据结构，包括变量信息、采样数据、连接配置等。
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def normalize_path(path: str) -> str:
    """将 Windows 路径规范化为 GDB/OpenOCD 兼容格式

    GDB MI2 和 OpenOCD Tcl 协议中，反斜杠 \\ 是转义字符。
    Windows 路径中的 \\ 会被错误解析（如 \\r 变成回车符）。
    此函数将路径转为绝对路径并使用正斜杠。

    Args:
        path: 文件路径（Windows 或 Unix 格式）

    Returns:
        规范化后的路径字符串（使用正斜杠）
    """
    return str(Path(path).resolve()).replace("\\", "/")


class VarType(Enum):
    """变量基础类型枚举"""
    U8 = "u8"
    U16 = "u16"
    U32 = "u32"
    I8 = "i8"
    I16 = "i16"
    I32 = "i32"
    F32 = "f32"
    F64 = "f64"
    UNKNOWN = "unknown"



VAR_TYPE_INFO: dict[VarType, tuple[str, int]] = {
    VarType.U8:  ("<B", 1),
    VarType.U16: ("<H", 2),
    VarType.U32: ("<I", 4),
    VarType.I8:  ("<b", 1),
    VarType.I16: ("<h", 2),
    VarType.I32: ("<i", 4),
    VarType.F32: ("<f", 4),
    VarType.F64: ("<d", 8),
}


@dataclass
class VariableInfo:
    """变量信息 - 从 ELF/DWARF 或 GDB MI 解析出的变量元数据"""
    name: str
    address: int = 0
    type_name: str = ""
    size: int = 0
    var_type: VarType = VarType.UNKNOWN
    children: list[VariableInfo] = field(default_factory=list)
    is_pointer: bool = False
    is_struct: bool = False
    is_array: bool = False
    array_size: int = 0
    source_file: str = ""
    enum_values: dict[int, list[str]] = field(default_factory=dict)

    @property
    def struct_fmt(self) -> str:
        """返回 struct.unpack 格式字符串"""
        if self.var_type in VAR_TYPE_INFO:
            return VAR_TYPE_INFO[self.var_type][0]
        return f"<{self.size}s"

    @property
    def type_size(self) -> int:
        """返回类型字节数"""
        if self.var_type in VAR_TYPE_INFO:
            return VAR_TYPE_INFO[self.var_type][1]
        return self.size


def infer_var_type(type_name: str, size: int) -> VarType:
    """从 GDB 类型名推断 VarType"""
    t = type_name.lower().strip()


    import re as _re
    t = _re.sub(r'\b(volatile|const|register|__volatile__|__const__)\b', '', t).strip()


    if t in ("float",):
        return VarType.F32
    if t in ("double",):
        return VarType.F64


    if t in ("int8_t", "signed char", "char", "int8"):
        return VarType.I8
    if t in ("int16_t", "short", "short int", "signed short", "int16"):
        return VarType.I16
    if t in ("int32_t", "int", "long", "long int", "signed int", "int32"):
        return VarType.I32


    if t in ("uint8_t", "unsigned char", "uint8"):
        return VarType.U8
    if t in ("uint16_t", "unsigned short", "unsigned short int", "uint16"):
        return VarType.U16
    if t in ("uint32_t", "unsigned int", "unsigned long", "uint32"):
        return VarType.U32


    if size == 1:
        return VarType.U8
    if size == 2:
        return VarType.U16
    if size == 4:
        return VarType.F32
    if size == 8:
        return VarType.F64

    return VarType.UNKNOWN


@dataclass
class SamplePoint:
    """单个采样数据点"""
    timestamp_ns: int
    value: float


@dataclass
class VarWatchEntry:
    """变量监控条目 - 用户添加到监控列表的变量"""
    expression: str
    var_info: VariableInfo | None = None
    enabled: bool = True
    color: str = "#00FF00"
    buffer_id: int = -1
    scale: float = 1.0
    offset: float = 0.0

    @property
    def address(self) -> int:
        return self.var_info.address if self.var_info else 0

    @property
    def dtype(self) -> VarType:
        return self.var_info.var_type if self.var_info else VarType.UNKNOWN

    @property
    def struct_fmt(self) -> str:
        return self.var_info.struct_fmt if self.var_info else "<f"


@dataclass
class ConnectionConfig:
    """连接配置（v3.0 - pyOCD 直连）"""

    probe_unique_id: str = ""
    target_override: str = "cortex_m"
    swd_frequency: int = 8000000


    elf_path: str = ""


@dataclass
class MIRecord:
    """GDB MI2 输出记录"""
    record_type: str = ""
    record_class: str = ""
    token: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    raw_line: str = ""

    @property
    def is_done(self) -> bool:
        return self.record_class == "done"

    @property
    def is_error(self) -> bool:
        return self.record_class == "error"

    @property
    def error_message(self) -> str:
        if self.is_error:
            return self.payload.get("msg", "Unknown error")
        return ""

    @property
    def value(self) -> str | None:
        """获取 value 字段 (常见于 -data-evaluate-expression 的返回)"""
        return self.payload.get("value")


def current_timestamp_ns() -> int:
    """获取当前高精度时间戳 (纳秒)"""
    return time.perf_counter_ns()
