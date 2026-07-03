




from __future__ import annotations

import logging
import os
from typing import Optional

from .data_types import VariableInfo
from .elf_reader import ElfSymbolReader

logger = logging.getLogger(__name__)

_MAX_RECENT = 20


class SymbolCache:




    def __init__(self, reader: ElfSymbolReader):
        self._reader = reader
        self._monitored: list[VariableInfo] = []
        self._recent: list[str] = []

    def get_tree(self) -> dict[str, list[VariableInfo]]:
        """返回按来源文件分组的变量树。

        key: 文件名（去掉路径前缀，空文件名归入 "globals" 组）
        value: 该文件中的变量列表（不含结构体成员，成员通过展开查看）
        """
        tree: dict[str, list[VariableInfo]] = {}
        for var in self._reader.list_globals():


            if '.' in var.name or '[' in var.name:
                continue
            key = var.source_file if var.source_file else "globals"

            key = os.path.basename(key) if '/' in key or '\\' in key else key
            if key not in tree:
                tree[key] = []
            tree[key].append(var)
        return tree

    def get_struct_members(self, var_name: str) -> list[VariableInfo]:
        """返回结构体/类变量的直接子成员列表。"""
        return self._reader.get_struct_members(var_name)

    def get_array_elements(self, var_name: str) -> list[VariableInfo]:
        """返回数组变量的所有元素列表。"""
        return self._reader.get_array_elements(var_name)

    def get_struct_member_groups(
        self, var_name: str
    ) -> dict[str, list[VariableInfo]]:
        """返回结构体/类成员，按来源类型分组（继承分组）。"""
        return self._reader.get_struct_member_groups(var_name)

    def get_nested_types(self, type_name: str) -> list[str]:
        """返回结构体/类的嵌套类型名列表。"""
        return self._reader.get_nested_types(type_name)

    def get_struct_def_members(self, type_name: str) -> list[VariableInfo]:
        """返回结构体的成员定义（用于浏览嵌套类型）。"""
        return self._reader.get_struct_def_members(type_name)

    def get_enum_values(self, type_name: str) -> list[tuple[str, int]]:
        """返回枚举类型的所有枚举值 [(name, value), ...]。"""
        return self._reader.get_enum_values(type_name)

    def search(self, pattern: str) -> list[VariableInfo]:
        """fnmatch 模糊搜索，不区分大小写。"""
        return self._reader.search(f"*{pattern}*") if '*' not in pattern else self._reader.search(pattern)

    def resolve(self, name: str) -> Optional[VariableInfo]:
        """按名称精确查找变量。"""
        return self._reader.resolve(name)

    def add_to_monitor(self, var_or_name) -> Optional[VariableInfo]:
        """将变量加入监控列表。

        接受 VariableInfo 对象或变量名字符串（自动 resolve）。
        返回加入的 VariableInfo，失败返回 None。
        同时更新 _recent 列表。
        """
        if isinstance(var_or_name, VariableInfo):
            var = var_or_name
        else:
            var = self._reader.resolve(var_or_name)

        if var is None:
            return None


        if any(v.address == var.address for v in self._monitored):
            return var

        self._monitored.append(var)


        if var.name in self._recent:
            self._recent.remove(var.name)
        self._recent.insert(0, var.name)
        if len(self._recent) > _MAX_RECENT:
            self._recent = self._recent[:_MAX_RECENT]

        logger.info("添加监控变量: %s @ 0x%08X", var.name, var.address)
        return var

    def remove_from_monitor(self, name: str) -> bool:
        """从监控列表移除变量，返回是否成功。"""
        for i, v in enumerate(self._monitored):
            if v.name == name:
                self._monitored.pop(i)
                logger.info("移除监控变量: %s", name)
                return True
        return False

    def clear_monitor(self) -> None:
        """清空监控列表。"""
        self._monitored.clear()

    @property
    def monitored_variables(self) -> list[VariableInfo]:
        """当前监控变量列表，供 SamplingEngine 使用。只读。"""
        return list(self._monitored)

    @property
    def recent_names(self) -> list[str]:
        """最近添加的变量名列表。"""
        return list(self._recent)

    @property
    def reader(self) -> ElfSymbolReader:
        """底层 reader 引用。"""
        return self._reader

    @property
    def variable_count(self) -> int:
        """全局变量总数。"""
        return len(self._reader.list_globals())
