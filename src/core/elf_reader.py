

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from elftools.elf.elffile import ELFFile
from elftools.common.exceptions import ELFError
from elftools.dwarf.locationlists import LocationParser, LocationExpr

from .data_types import VariableInfo, VarType, infer_var_type

logger = logging.getLogger(__name__)


DW_ATE_boolean       = 0x02
DW_ATE_signed        = 0x05
DW_ATE_unsigned      = 0x07
DW_ATE_unsigned_char = 0x08
DW_ATE_float         = 0x04


_TYPE_MAP: dict[tuple[int, int], VarType] = {
    (DW_ATE_float, 4):    VarType.F32,
    (DW_ATE_float, 8):    VarType.F64,
    (DW_ATE_signed, 4):   VarType.I32,
    (DW_ATE_unsigned, 4): VarType.U32,
    (DW_ATE_signed, 2):   VarType.I16,
    (DW_ATE_unsigned, 2): VarType.U16,
    (DW_ATE_signed, 1):   VarType.I8,
    (DW_ATE_unsigned, 1): VarType.U8,
    (DW_ATE_unsigned_char, 1): VarType.U8,
    (DW_ATE_boolean, 1):  VarType.U8,
}


_TRANSPARENT_TAGS = frozenset({
    'DW_TAG_const_type',
    'DW_TAG_volatile_type',
    'DW_TAG_typedef',
    'DW_TAG_restrict_type',
    'DW_TAG_atomic_type',
})

class ElfSymbolReader:
    """ELF/AXF 文件符号解析器

    纯静态解析，不依赖硬件。典型耗时 ~50ms（STM32 工程）。
    解析 .symtab 和 DWARF 调试信息，提取全局变量的名称、地址、类型和大小。

    典型用法：
        reader = ElfSymbolReader()
        reader.load("build/project.elf")
        for var in reader.list_globals():
            print(f"{var.name} @ 0x{var.address:08X} ({var.type_name})")
    """

    def __init__(self):
        self._loaded = False
        self._var_cache: dict[str, VariableInfo] = {}
        self._globals: list[VariableInfo] = []
        self._dwarf_info = None
        self._location_parser: LocationParser | None = None

        self._struct_defs: dict[str, list[tuple[str, int, object, object]]] = {}
        self._struct_parents: dict[str, list[tuple[str, int, object]]] = {}
        self._struct_nested: dict[str, list[str]] = {}
        self._member_origin: dict[str, str] = {}

        self._enum_defs: dict[str, list[tuple[str, int]]] = {}
        self._typedef_map: dict[str, object] = {}
        self._anon_struct_counter: int = 0

    def load(self, elf_path: str) -> None:
        """解析 ELF/AXF 文件，建立内部符号缓存。

        解析流程：.symtab 提取地址/大小 → DWARF 提取类型/命名空间 → symtab 补漏
        → 结构体成员后处理 → 全局变量列表去重排序。

        Args:
            elf_path: ELF 或 AXF 文件路径

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 文件格式无法解析
        """
        path = Path(elf_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {elf_path}")


        self._var_cache.clear()
        self._globals.clear()
        self._dwarf_info = None
        self._struct_defs.clear()
        self._struct_parents.clear()
        self._struct_nested.clear()
        self._member_origin.clear()
        self._enum_defs.clear()
        self._typedef_map.clear()
        self._anon_struct_counter = 0


        symtab_cache: dict[str, tuple[int, int]] = {}

        with open(path, 'rb') as f:
            try:
                elf = ELFFile(f)
            except ELFError as e:
                raise ValueError(
                    f"无法解析 ELF/AXF 文件：{e}\n"
                    f"请确认文件是有效的编译产物（非 HEX/BIN）"
                ) from e


            symtab = elf.get_section_by_name('.symtab')
            if symtab:
                for sym in symtab.iter_symbols():
                    if sym['st_info']['type'] == 'STT_OBJECT':
                        addr = sym['st_value']
                        size = sym['st_size']
                        name = sym.name
                        if name and size > 0:
                            symtab_cache[name] = (addr, size)


            has_dwarf = False
            if elf.has_dwarf_info():
                dwarf = elf.get_dwarf_info()
                self._dwarf_info = dwarf
                has_dwarf = True

                try:
                    from elftools.dwarf.locationlists import LocationLists, LocationListsPair
                    loc_sec = dwarf.location_lists()
                    if loc_sec is not None:
                        if isinstance(loc_sec, (LocationLists, LocationListsPair)):
                            self._location_parser = LocationParser(loc_sec)
                        else:
                            self._location_parser = LocationParser(None)
                    else:
                        self._location_parser = LocationParser(None)
                except Exception:
                    self._location_parser = LocationParser(None)

                for CU in dwarf.iter_CUs():
                    top_die = CU.get_top_DIE()
                    self._walk_die(top_die, CU, [], symtab_cache)



        for name, (addr, size) in symtab_cache.items():
            if name in self._var_cache:
                continue
            vt = infer_var_type("", size)
            self._var_cache[name] = VariableInfo(
                name=name,
                address=addr,
                type_name="",
                size=size,
                var_type=vt,
                source_file="(symtab)" if has_dwarf else "",
            )


        self._resolve_struct_members()


        seen_addrs: set[int] = set()
        self._globals = []
        for info in self._var_cache.values():
            if info.address not in seen_addrs:
                seen_addrs.add(info.address)
                self._globals.append(info)
        self._globals.sort(key=lambda v: (v.source_file, v.name))

        self._loaded = True
        logger.info("ELF 解析完成: %s, 共 %d 个全局变量", elf_path, len(self._globals))

    def resolve(self, name: str) -> Optional[VariableInfo]:
        """按名称查找变量，O(1) 字典查找。

        支持完整名（含命名空间）、短名、或 dot-notation（如 obj.member）。
        """
        info = self._var_cache.get(name)
        if info is not None:
            return info

        for key, val in self._var_cache.items():
            if key.endswith('::' + name) or key.endswith('.' + name) or key == name:
                return val
        return None

    def list_globals(self) -> list[VariableInfo]:
        """返回所有全局/静态变量列表，按 source_file 分组排序。"""
        return list(self._globals)

    def get_struct_members(self, var_name: str) -> list[VariableInfo]:
        """返回结构体/类变量的直接子成员列表。

        查找 _var_cache 中以 "var_name." 为前缀的条目，
        排除数组元素（以 [ 开头的后缀）和深层嵌套成员。
        """
        prefix = var_name + '.'
        members = []
        seen_addrs: set[int] = set()
        for key, val in self._var_cache.items():
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]

            if not suffix or '[' in suffix or '.' in suffix:
                continue
            if val.address not in seen_addrs:
                seen_addrs.add(val.address)
                members.append(val)
        return sorted(members, key=lambda v: v.address)

    def get_array_elements(self, var_name: str) -> list[VariableInfo]:
        """返回数组变量的所有元素列表。

        查找 _var_cache 中以 "var_name[" 为前缀的条目。
        """
        prefix = var_name + '['
        elements = []
        for key, val in self._var_cache.items():
            if key.startswith(prefix):
                elements.append(val)
        return sorted(elements, key=lambda v: v.address)

    def get_struct_member_groups(
        self, var_name: str
    ) -> dict[str, list[VariableInfo]]:
        """返回结构体/类成员，按来源类型分组。

        key: 类型名（如 "Derived", "Base"），"self" 表示自身成员。
        value: 该类型中的成员列表（按地址排序）。

        用于 UI 中展示继承分组。
        """
        all_members = self.get_struct_members(var_name)
        if not all_members:
            return {}


        origins: set[str] = set()
        for m in all_members:
            origin = self._member_origin.get(m.name)
            if origin:
                origins.add(origin)

        if len(origins) <= 1:
            return {}


        var_info = self.resolve(var_name)
        self_type = var_info.type_name if var_info else ""

        groups: dict[str, list[VariableInfo]] = {}
        for m in all_members:
            origin = self._member_origin.get(m.name)
            if origin and origin == self_type:
                key = "self"
            elif origin:
                key = origin
            else:
                key = "self"
            groups.setdefault(key, []).append(m)


        for key in groups:
            groups[key].sort(key=lambda v: v.address)

        return groups

    def get_nested_types(self, type_name: str) -> list[str]:
        """返回结构体/类的嵌套类型名列表。"""
        nested = self._struct_nested.get(type_name, [])
        if nested:
            return list(nested)

        for tname, nlist in self._struct_nested.items():
            if type_name.endswith('::' + tname) or tname == type_name:
                return list(nlist)
        return []

    def get_struct_def_members(self, type_name: str) -> list[VariableInfo]:
        """从 _struct_defs 中直接查询结构体成员定义（不需要全局变量实例）。

        用于浏览嵌套类型的成员。
        """
        members = self._find_struct_def(type_name)
        if not members:
            return []
        result = []
        seen: set[int] = set()
        for mname, offset, die, cu in members:
            var_type, resolved_type = self._resolve_die_type(die, cu)
            is_struct = False
            type_attr = die.attributes.get('DW_AT_type')
            if type_attr:
                try:
                    ref_die = self._get_type_die(type_attr, cu)
                    if ref_die:
                        base_die = self._resolve_base_type(ref_die, cu)
                        if base_die and base_die.tag in (
                            'DW_TAG_structure_type', 'DW_TAG_class_type'
                        ):
                            is_struct = True
                except Exception:
                    pass
            if offset not in seen:
                seen.add(offset)
                info = VariableInfo(
                    name=mname,
                    address=offset,
                    size=4,
                    var_type=var_type,
                    type_name=resolved_type,
                    is_struct=is_struct,
                )
                result.append(info)
        return result

    def _find_enum_def(self, type_name: str) -> list[tuple[str, int]]:
        """根据类型名查找枚举定义，支持命名空间后缀匹配。"""
        values = self._enum_defs.get(type_name)
        if values:
            return values
        for tname, vdefs in self._enum_defs.items():
            if type_name.endswith('::' + tname) or tname == type_name:
                return vdefs
        return []

    def get_enum_values(self, type_name: str) -> list[tuple[str, int]]:
        """返回枚举类型的所有枚举值 [(name, value), ...]。"""
        return self._find_enum_def(type_name)

    def search(self, pattern: str) -> list[VariableInfo]:
        """fnmatch 模式搜索，支持 * 通配符。"""
        results = []
        seen: set[int] = set()
        for var in self._globals:
            if fnmatch.fnmatch(var.name, pattern) and var.address not in seen:
                seen.add(var.address)
                results.append(var)
        return results

    @property
    def is_loaded(self) -> bool:
        return self._loaded



    def _walk_die(
        self,
        die,
        CU,
        namespace_stack: list[str],
        symtab_cache: dict[str, tuple[int, int]],
        enc_type_stack: list[str] | None = None,
        parent_die=None,
    ) -> None:
        """递归遍历 DIE 树，维护命名空间路径和嵌套类型上下文。

        按 DIE 标签分派处理：namespace 推入栈、struct/class 收集成员定义、
        typedef 建立别名映射、enum 收集枚举值、variable 提取全局变量信息。
        """
        if enc_type_stack is None:
            enc_type_stack = []
        tag = die.tag


        if tag == 'DW_TAG_namespace':
            ns_attr = die.attributes.get('DW_AT_name')
            ns_name = ns_attr.value.decode('utf-8', errors='replace') if ns_attr else '(anon)'
            for child in die.iter_children():
                self._walk_die(child, CU, namespace_stack + [ns_name], symtab_cache, enc_type_stack, parent_die=die)


        elif tag in ('DW_TAG_structure_type', 'DW_TAG_class_type'):
            type_attr = die.attributes.get('DW_AT_name')
            tname = type_attr.value.decode('utf-8', errors='replace') if type_attr else ''


            is_anonymous = not tname
            if is_anonymous:
                self._anon_struct_counter += 1
                tname = f'__anon_struct_{self._anon_struct_counter}'

                self._typedef_map[tname] = die


            if enc_type_stack and not is_anonymous:
                parent_type = enc_type_stack[-1]
                nested_list = self._struct_nested.setdefault(parent_type, [])
                if tname not in nested_list:
                    nested_list.append(tname)

            if tname not in self._struct_defs:
                members = []
                parents = []
                for child in die.iter_children():
                    if child.tag == 'DW_TAG_member':
                        mname_attr = child.attributes.get('DW_AT_name')
                        if not mname_attr:
                            continue
                        mname = mname_attr.value.decode('utf-8', errors='replace')
                        loc_attr = child.attributes.get('DW_AT_data_member_location')
                        offset = self._decode_member_location(loc_attr)
                        members.append((mname, offset, child, CU))
                    elif child.tag == 'DW_TAG_inheritance':

                        inh_type = child.attributes.get('DW_AT_type')
                        if inh_type:
                            try:
                                parent_die = self._get_type_die(inh_type, CU)
                                if parent_die:
                                    pname_attr = parent_die.attributes.get('DW_AT_name')
                                    if pname_attr:
                                        pname = pname_attr.value.decode('utf-8', errors='replace')
                                        loc_attr = child.attributes.get('DW_AT_data_member_location')
                                        offset = self._decode_member_location(loc_attr)
                                        parents.append((pname, offset, CU))
                            except Exception:
                                pass
                if members:
                    self._struct_defs[tname] = members
                if parents:
                    self._struct_parents[tname] = parents

            for child in die.iter_children():
                if child.tag not in ('DW_TAG_member', 'DW_TAG_inheritance'):
                    self._walk_die(child, CU, namespace_stack, symtab_cache,
                                   (enc_type_stack + [tname]) if tname and not is_anonymous else enc_type_stack,
                                   parent_die=die)


        elif tag == 'DW_TAG_typedef':
            typedef_name_attr = die.attributes.get('DW_AT_name')
            if typedef_name_attr:
                typedef_name = typedef_name_attr.value.decode('utf-8', errors='replace')
                type_ref = die.attributes.get('DW_AT_type')
                if type_ref:
                    try:
                        ref_die = self._get_type_die(type_ref, CU)
                        if ref_die:

                            if ref_die.tag in ('DW_TAG_structure_type', 'DW_TAG_class_type'):
                                base_type_attr = ref_die.attributes.get('DW_AT_name')
                                base_type_name = base_type_attr.value.decode('utf-8', errors='replace') if base_type_attr else ''

                                if not base_type_name:


                                    anon_key = None
                                    for key, stored_die in self._typedef_map.items():
                                        if key.startswith('__anon_struct_') and stored_die is ref_die:
                                            anon_key = key
                                            break

                                    if anon_key and anon_key in self._struct_defs:

                                        self._struct_defs[typedef_name] = self._struct_defs[anon_key]
                                        logger.debug("typedef %s -> 匿名结构体 %s，已复制成员", typedef_name, anon_key)
                                    else:

                                        members = []
                                        for child in ref_die.iter_children():
                                            if child.tag == 'DW_TAG_member':
                                                mname_attr = child.attributes.get('DW_AT_name')
                                                if not mname_attr:
                                                    continue
                                                mname = mname_attr.value.decode('utf-8', errors='replace')
                                                loc_attr = child.attributes.get('DW_AT_data_member_location')
                                                offset = self._decode_member_location(loc_attr)
                                                members.append((mname, offset, child, CU))
                                        if members:
                                            self._struct_defs[typedef_name] = members
                                            logger.debug("typedef %s -> 匿名结构体，收集了 %d 个成员", typedef_name, len(members))
                                else:

                                    if base_type_name in self._struct_defs:
                                        self._struct_defs[typedef_name] = self._struct_defs[base_type_name]
                                        logger.debug("typedef %s -> %s (别名)", typedef_name, base_type_name)
                    except Exception as e:
                        logger.debug("解析 typedef %s 失败: %s", typedef_name, e)


        elif tag == 'DW_TAG_enumeration_type':
            type_attr = die.attributes.get('DW_AT_name')
            tname = type_attr.value.decode('utf-8', errors='replace') if type_attr else ''
            if tname:

                if enc_type_stack:
                    parent_type = enc_type_stack[-1]
                    nested_list = self._struct_nested.setdefault(parent_type, [])
                    if tname not in nested_list:
                        nested_list.append(tname)

                if tname not in self._enum_defs:
                    enumerators = []
                    for child in die.iter_children():
                        if child.tag == 'DW_TAG_enumerator':
                            ename_attr = child.attributes.get('DW_AT_name')
                            if not ename_attr:
                                continue
                            ename = ename_attr.value.decode('utf-8', errors='replace')
                            val_attr = child.attributes.get('DW_AT_const_value')
                            val = val_attr.value if val_attr and isinstance(val_attr.value, int) else 0
                            enumerators.append((ename, val))
                    if enumerators:
                        self._enum_defs[tname] = enumerators


        elif tag == 'DW_TAG_variable':
            self._process_variable_die(die, CU, namespace_stack, symtab_cache, parent_die)


        else:
            for child in die.iter_children():
                self._walk_die(child, CU, namespace_stack, symtab_cache, parent_die=die)

    def _process_variable_die(
        self,
        die,
        CU,
        namespace_stack: list[str],
        symtab_cache: dict[str, tuple[int, int]],
        parent_die=None,
    ) -> None:
        """处理 DW_TAG_variable：提取全局变量信息。

        地址获取策略（DWARF 优先，兼容 AC6/GCC）：
        1. 优先从 DWARF DW_AT_location 提取地址（DW_OP_addr）
        2. 失败则 fallback 到 symtab_cache
        3. 都失败则跳过该变量

        同时解析变量的类型、大小、是否为结构体/数组/指针，并建立完整名和短名的别名映射。
        """
        name_attr = die.attributes.get('DW_AT_name')
        if not name_attr:
            return

        var_name = name_attr.value.decode('utf-8', errors='replace')


        func_name = None
        if parent_die:
            try:
                p = parent_die
                while p:
                    if p.tag == 'DW_TAG_subprogram':
                        fattr = p.attributes.get('DW_AT_name')
                        if fattr:
                            func_name = fattr.value.decode('utf-8', errors='replace')
                        break
                    p = p.get_parent()
            except Exception:
                pass

        if func_name:
            full_name = '::'.join(namespace_stack + [func_name, var_name])
        elif namespace_stack:
            full_name = '::'.join(namespace_stack + [var_name])
        else:
            full_name = var_name


        addr = self._extract_addr_from_location(die)
        size = 0

        if addr is not None:

            size = self._resolve_die_byte_size(die, CU)
            if size <= 0:
                size = 1
        else:

            sym_info = symtab_cache.get(var_name)
            if sym_info is None:
                sym_info = symtab_cache.get(full_name)
            if sym_info is None:
                return
            addr, size = sym_info


        var_type, type_name = self._resolve_die_type(die, CU)


        is_struct = self._is_struct_type(die, CU) or self._is_enum_type(die, CU)


        is_array = False
        array_size = 0
        if not is_struct and self._is_array_type(die, CU):
            arr_info = self._resolve_array_info(die, CU)
            if arr_info:
                elem_type_name, elem_size, count, elem_vt = arr_info
                is_array = True
                array_size = count

                type_name = f"{elem_type_name}[{count}]"


        is_pointer = False
        if not is_struct and not is_array:
            if self._is_pointer_type(die, CU):
                is_pointer = True
                type_name = self._resolve_pointer_target(die, CU)


        source = self._get_source_file(die, CU)


        enum_values: dict[int, list[str]] = {}
        if not is_struct and not is_array and not is_pointer:
            base_type_attr = die.attributes.get('DW_AT_type')
            if base_type_attr:
                try:
                    ref_die = self._get_type_die(base_type_attr, CU)
                    if ref_die:
                        base_die = self._resolve_base_type(ref_die, CU)
                        if base_die and base_die.tag == 'DW_TAG_enumeration_type':
                            ename_attr = base_die.attributes.get('DW_AT_name')
                            if ename_attr:
                                etype_name = ename_attr.value.decode('utf-8', errors='replace')
                                edefs = self._find_enum_def(etype_name)
                                for ename, evalue in edefs:
                                    enum_values.setdefault(evalue, []).append(ename)
                except Exception:
                    pass

        info = VariableInfo(
            name=full_name,
            address=addr,
            size=size,
            var_type=var_type,
            type_name=type_name,
            source_file=source,
            is_struct=is_struct or is_array or is_pointer,
            is_array=is_array,
            array_size=array_size,
            is_pointer=is_pointer,
            enum_values=enum_values,
        )
        self._var_cache[full_name] = info
        if var_name != full_name:
            self._var_cache[var_name] = info


        if is_array and array_size > 0:
            self._resolve_array_elements(info)



    def _find_struct_def(self, type_name: str) -> list[tuple[str, int, object, object]]:
        """根据类型名查找结构体成员定义，支持命名空间后缀匹配。"""
        members = self._struct_defs.get(type_name)
        if members:
            return members

        for tname, mdefs in self._struct_defs.items():
            if type_name.endswith('::' + tname) or tname == type_name:
                return mdefs
        return []

    def _collect_all_members(
        self,
        type_name: str,
        base_offset: int = 0,
        visited: set[str] | None = None,
        origin_type: str | None = None,
    ) -> list[tuple[str, int, object, object, str]]:
        """递归收集结构体的所有成员（含继承的父类成员）。

        沿继承链递归展开，将父类成员的偏移叠加到 base_offset 上。
        visited 集合防止循环继承导致无限递归。

        Args:
            type_name: 结构体类型名
            base_offset: 累积偏移（继承链上的偏移叠加）
            visited: 已访问的类型名集合（防止循环继承）
            origin_type: 当前递归的起始类型名（用于标记成员来源）

        Returns:
            [(member_name, absolute_offset, DIE, CU, origin_type_name), ...]
        """
        if visited is None:
            visited = set()
        if type_name in visited:
            return []
        visited.add(type_name)
        if origin_type is None:
            origin_type = type_name

        result = []
        members = self._find_struct_def(type_name)
        for mname, offset, die, cu in members:
            result.append((mname, base_offset + offset, die, cu, origin_type))


        parents = self._struct_parents.get(type_name, [])
        if not parents:

            for tname, plist in self._struct_parents.items():
                if type_name.endswith('::' + tname) or tname == type_name:
                    parents = plist
                    break

        for parent_name, parent_offset, parent_cu in parents:
            result.extend(
                self._collect_all_members(
                    parent_name, base_offset + parent_offset, visited, parent_name
                )
            )

        return result

    def _resolve_struct_members(self) -> None:
        """后处理：为所有结构体类型的全局变量解析成员地址（含继承成员）。

        必须在所有 DWARF CU 遍历完成后调用，此时 _var_cache 已包含所有全局变量。
        分两轮处理：
        1. 修复数组及其元素的 is_struct 标志（DWARF 遍历时 typedef 可能还未就绪）
        2. 递归解析所有结构体变量的成员（包括修复后的数组元素）
        """



        arrays_to_fix: list[VariableInfo] = []
        for var_info in list(self._var_cache.values()):
            if var_info.is_array:
                elem_type_name = var_info.type_name
                bracket_pos = elem_type_name.rfind('[')
                if bracket_pos > 0:
                    elem_type_name = elem_type_name[:bracket_pos]
                if self._find_struct_def(elem_type_name):
                    if not var_info.is_struct:
                        var_info.is_struct = True
                    arrays_to_fix.append(var_info)


        for arr_info in arrays_to_fix:

            elem_type_name = arr_info.type_name
            bracket_pos = elem_type_name.rfind('[')
            if bracket_pos > 0:
                elem_type_name = elem_type_name[:bracket_pos]
            elem_has_struct = bool(self._find_struct_def(elem_type_name))
            if elem_has_struct:
                for i in range(arr_info.array_size):
                    elem_name = f"{arr_info.name}[{i}]"
                    existing = self._var_cache.get(elem_name)
                    if existing and not existing.is_struct:
                        existing.is_struct = True

            self._resolve_array_elements(arr_info)


        for var_info in list(self._var_cache.values()):
            if not var_info.is_struct:
                continue
            self._resolve_struct_members_recursive(var_info)

    def _resolve_struct_members_recursive(self, var_info: VariableInfo) -> None:
        """递归解析结构体变量的成员（含嵌套结构体、数组、指针）。"""
        all_members = self._collect_all_members(var_info.type_name)
        if not all_members:
            return

        for member_name, offset, die, cu, origin_type in all_members:
            member_addr = var_info.address + offset

            dot_name = f"{var_info.name}.{member_name}"
            if dot_name in self._var_cache:
                continue

            var_type, type_name = self._resolve_die_type(die, cu)

            byte_size = 4
            is_member_struct = False
            is_member_array = False
            member_array_size = 0
            is_member_pointer = False
            type_attr = die.attributes.get('DW_AT_type')
            if type_attr:
                try:
                    ref_die = self._get_type_die(type_attr, cu)
                    if ref_die:
                        base_die = self._resolve_base_type(ref_die, cu)
                        if base_die:
                            sz = base_die.attributes.get('DW_AT_byte_size')
                            if sz:
                                byte_size = sz.value
                            if base_die.tag in (
                                'DW_TAG_structure_type', 'DW_TAG_class_type',
                            ):
                                is_member_struct = True
                            elif base_die.tag == 'DW_TAG_enumeration_type':
                                is_member_struct = True
                            elif base_die.tag == 'DW_TAG_array_type':
                                is_member_array = True
                                arr_info = self._resolve_array_info(die, cu)
                                if arr_info:
                                    elem_tname, elem_sz, count, elem_vt = arr_info
                                    member_array_size = count
                                    type_name = f"{elem_tname}[{count}]"
                                    byte_size = elem_sz * count
                            elif base_die.tag == 'DW_TAG_pointer_type':
                                is_member_pointer = True
                                type_name = self._resolve_pointer_target(die, cu)
                except Exception:
                    pass


            member_enum_values: dict[int, list[str]] = {}
            if type_attr and not is_member_struct and not is_member_array and not is_member_pointer:
                try:
                    ref_die = self._get_type_die(type_attr, cu)
                    if ref_die:
                        base_die = self._resolve_base_type(ref_die, cu)
                        if base_die and base_die.tag == 'DW_TAG_enumeration_type':
                            ename_attr = base_die.attributes.get('DW_AT_name')
                            if ename_attr:
                                etype_name = ename_attr.value.decode('utf-8', errors='replace')
                                edefs = self._find_enum_def(etype_name)
                                for ename, evalue in edefs:
                                    member_enum_values.setdefault(evalue, []).append(ename)
                except Exception:
                    pass

            info = VariableInfo(
                name=dot_name,
                address=member_addr,
                size=byte_size,
                var_type=var_type,
                type_name=type_name,
                source_file=var_info.source_file,
                is_struct=is_member_struct or is_member_array or is_member_pointer,
                is_array=is_member_array,
                array_size=member_array_size,
                is_pointer=is_member_pointer,
                enum_values=member_enum_values,
            )
            self._var_cache[dot_name] = info
            self._member_origin[dot_name] = origin_type


            if is_member_struct:
                self._resolve_struct_members_recursive(info)


            if is_member_array and member_array_size > 0:
                self._resolve_array_elements(info)

    def _resolve_array_elements(self, array_var: VariableInfo) -> None:
        """为数组变量创建各元素的 VariableInfo 条目。

        创建 arr[0], arr[1], ... 等条目，每个元素的地址按元素大小递增。
        如果元素类型是结构体/枚举，设置 is_struct 并递归解析成员。
        """
        count = array_var.array_size
        if count <= 0:
            return


        elem_type_name = array_var.type_name
        bracket_pos = elem_type_name.rfind('[')
        if bracket_pos > 0:
            elem_type_name = elem_type_name[:bracket_pos]


        elem_size = array_var.size // count if count > 0 else 1


        from .data_types import infer_var_type
        elem_vt = infer_var_type(elem_type_name, elem_size)


        elem_is_struct = bool(self._find_struct_def(elem_type_name))

        for i in range(count):
            elem_name = f"{array_var.name}[{i}]"
            if elem_name in self._var_cache:
                continue
            elem_info = VariableInfo(
                name=elem_name,
                address=array_var.address + i * elem_size,
                size=elem_size,
                var_type=elem_vt,
                type_name=elem_type_name,
                source_file=array_var.source_file,
                is_struct=elem_is_struct,
            )
            self._var_cache[elem_name] = elem_info


            if elem_is_struct:
                self._resolve_struct_members_recursive(elem_info)



    def _is_struct_type(self, die, CU) -> bool:
        """判断变量的底层类型是否为结构体或类（DW_TAG_structure_type / class_type）。"""
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return False
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return False
            base_die = self._resolve_base_type(ref_die, CU)
            return base_die is not None and base_die.tag in (
                'DW_TAG_structure_type', 'DW_TAG_class_type'
            )
        except Exception:
            return False

    def _is_enum_type(self, die, CU) -> bool:
        """判断变量的底层类型是否为枚举（DW_TAG_enumeration_type）。"""
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return False
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return False
            base_die = self._resolve_base_type(ref_die, CU)
            return base_die is not None and base_die.tag == 'DW_TAG_enumeration_type'
        except Exception:
            return False

    def _is_array_type(self, die, CU) -> bool:
        """判断变量的底层类型是否为数组（DW_TAG_array_type）。"""
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return False
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return False

            base_die = self._resolve_base_type(ref_die, CU)
            return base_die is not None and base_die.tag == 'DW_TAG_array_type'
        except Exception:
            return False

    def _is_pointer_type(self, die, CU) -> bool:
        """判断变量的底层类型是否为指针（DW_TAG_pointer_type）。"""
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return False
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return False
            base_die = self._resolve_base_type(ref_die, CU)
            return base_die is not None and base_die.tag == 'DW_TAG_pointer_type'
        except Exception:
            return False

    def _resolve_array_info(self, die, CU) -> tuple[str, int, int, VarType] | None:
        """解析数组类型信息。

        Returns:
            (element_type_name, element_size, array_count, element_var_type) 或 None
        """
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return None
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return None

            base_die = self._resolve_base_type(ref_die, CU)
            if base_die is None or base_die.tag != 'DW_TAG_array_type':
                return None


            elem_type_attr = base_die.attributes.get('DW_AT_type')
            if not elem_type_attr:
                return None
            elem_die = self._get_type_die(elem_type_attr, CU)
            if elem_die is None:
                return None


            elem_type_name = None
            cur = elem_die
            while cur and cur.tag in _TRANSPARENT_TAGS:
                if cur.tag == 'DW_TAG_typedef':
                    tname_attr = cur.attributes.get('DW_AT_name')
                    if tname_attr:
                        elem_type_name = tname_attr.value.decode('utf-8', errors='replace')
                        break
                inner_attr = cur.attributes.get('DW_AT_type')
                if not inner_attr:
                    break
                cur = self._get_type_die(inner_attr, CU)

            elem_base = self._resolve_base_type(elem_die, CU)
            if elem_type_name:
                elem_var_type, _ = self._die_to_vartype(elem_base, CU)
            else:
                elem_var_type, elem_type_name = self._die_to_vartype(elem_base, CU)


            elem_size = 1
            if elem_base:
                sz = elem_base.attributes.get('DW_AT_byte_size')
                if sz:
                    elem_size = sz.value


            count = 0
            for child in base_die.iter_children():
                if child.tag == 'DW_TAG_subrange_type':

                    count_attr = child.attributes.get('DW_AT_count')
                    if count_attr and isinstance(count_attr.value, int):
                        count = count_attr.value
                        break

                    ub_attr = child.attributes.get('DW_AT_upper_bound')
                    if ub_attr and isinstance(ub_attr.value, int):
                        count = ub_attr.value + 1
                        break

            if count <= 0:
                return None
            return elem_type_name, elem_size, count, elem_var_type
        except Exception:
            return None

    def _resolve_pointer_target(self, die, CU) -> str:
        """解析指针指向的目标类型名。"""
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return 'void *'
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return 'void *'
            base_die = self._resolve_base_type(ref_die, CU)
            if base_die is None:
                return 'void *'
            _, tname = self._die_to_vartype(base_die, CU)
            return tname + ' *'
        except Exception:
            return 'void *'

    def _resolve_die_type(self, die, CU) -> tuple[VarType, str]:
        """从 DIE 解析变量类型，返回 (VarType, 类型名)。

        对于 typedef 类型，返回 typedef 名称（如 Vision_Protocol_t）而不是底层类型。
        会逐层剥离 const/volatile/restrict 等透明标签，遇到 typedef 即采用其名称。
        """
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return VarType.UNKNOWN, 'void'

        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return VarType.UNKNOWN, 'unknown'



            cur = ref_die
            while cur and cur.tag in _TRANSPARENT_TAGS:
                if cur.tag == 'DW_TAG_typedef':
                    typedef_name_attr = cur.attributes.get('DW_AT_name')
                    if typedef_name_attr:
                        typedef_name = typedef_name_attr.value.decode('utf-8', errors='replace')
                        base_die = self._resolve_base_type(cur, CU)
                        var_type, _ = self._die_to_vartype(base_die, CU)
                        return var_type, typedef_name

                inner_attr = cur.attributes.get('DW_AT_type')
                if not inner_attr:
                    break
                cur = self._get_type_die(inner_attr, CU)


            base_die = self._resolve_base_type(ref_die, CU)
            return self._die_to_vartype(base_die, CU)
        except Exception:
            return VarType.UNKNOWN, 'unknown'

    def _get_type_die(self, attr, CU):
        """从 DW_AT_type 属性获取目标 DIE。"""
        try:

            return attr.get_DIE_from_attribute()
        except Exception:

            try:
                ref_offset = attr.value
                if hasattr(attr, 'form') and 'ref' in str(attr.form):
                    absolute_offset = CU.cu_offset + ref_offset
                    return CU.get_DIE_from_refaddr(absolute_offset)
            except Exception:
                pass
            return None

    def _resolve_base_type(self, die, CU, depth: int = 0) -> Optional[object]:
        """递归剥除 const/volatile/typedef，返回底层 base_type DIE。"""
        if depth > 16:
            return die
        if die.tag not in _TRANSPARENT_TAGS:
            return die
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return None
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return None
            return self._resolve_base_type(ref_die, CU, depth + 1)
        except Exception:
            return None

    def _die_to_vartype(self, base_die, CU=None) -> tuple[VarType, str]:
        """返回 (VarType 枚举, 人类可读类型名)。"""
        if base_die is None:
            return VarType.UNKNOWN, 'unknown'

        if base_die.tag == 'DW_TAG_base_type':
            enc_attr = base_die.attributes.get('DW_AT_encoding')
            size_attr = base_die.attributes.get('DW_AT_byte_size')
            name_attr = base_die.attributes.get('DW_AT_name')

            if enc_attr and size_attr:
                enc = enc_attr.value
                size = size_attr.value
                key = (enc, size)
                vtype = _TYPE_MAP.get(key, VarType.UNKNOWN)
                tname = name_attr.value.decode('utf-8', errors='replace') if name_attr else 'unknown'
                return vtype, tname

        if base_die.tag in ('DW_TAG_structure_type', 'DW_TAG_class_type'):
            name_attr = base_die.attributes.get('DW_AT_name')
            tname = name_attr.value.decode('utf-8', errors='replace') if name_attr else 'struct'
            return VarType.UNKNOWN, tname

        if base_die.tag == 'DW_TAG_pointer_type':

            type_attr = base_die.attributes.get('DW_AT_type')
            if type_attr:
                try:
                    target_die = self._get_type_die(type_attr, CU)
                    if target_die:
                        target_base = self._resolve_base_type(target_die, CU)
                        _, tname = self._die_to_vartype(target_base, CU)
                        return VarType.U32, tname + ' *'
                except Exception:
                    pass
            return VarType.U32, 'void *'

        if base_die.tag == 'DW_TAG_enumeration_type':
            ename_attr = base_die.attributes.get('DW_AT_name')
            ename = ename_attr.value.decode('utf-8', errors='replace') if ename_attr else 'enum'
            return VarType.I32, ename

        if base_die.tag == 'DW_TAG_array_type':

            type_attr = base_die.attributes.get('DW_AT_type')
            if type_attr:
                try:
                    elem_die = self._get_type_die(type_attr, CU)
                    if elem_die:
                        elem_base = self._resolve_base_type(elem_die, CU)
                        _, tname = self._die_to_vartype(elem_base, CU)
                        return VarType.UNKNOWN, tname + '[]'
                except Exception:
                    pass
            return VarType.UNKNOWN, 'array[]'

        return VarType.UNKNOWN, 'unknown'



    @staticmethod
    def _decode_uleb128(byte_list: list[int]) -> int:
        """轻量级 ULEB128 解码器。

        将 ULEB128 编码的字节序列解码为无符号整数。
        用于解析 DW_AT_data_member_location 中的 DW_OP_plus_uconst 操作数。
        """
        result = 0
        shift = 0
        for b in byte_list:
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result

    def _decode_member_location(self, loc_attr) -> int:
        """解析 DW_AT_data_member_location，兼容 AC6 和 GCC 的不同编码形式。

        支持两种编码：
        - DWARF3+ DW_FORM_data* → attr.value 是 int（直接偏移）
        - DWARF2 / DW_FORM_exprloc → attr.value 是 list[int]（DWARF 表达式）
          常见形式：[0x23, uleb128...] → DW_OP_plus_uconst
        """
        if loc_attr is None:
            return 0
        if isinstance(loc_attr.value, int):
            return loc_attr.value
        if isinstance(loc_attr.value, list) and len(loc_attr.value) > 0:

            if loc_attr.value[0] == 0x23:
                return self._decode_uleb128(loc_attr.value[1:])

            if loc_attr.value[0] == 0x10 and len(loc_attr.value) >= 2:
                return self._decode_uleb128(loc_attr.value[1:])
        return 0

    def _extract_addr_from_location(self, die) -> int | None:
        """从 DW_AT_location 属性提取绝对内存地址（DW_OP_addr）。

        兼容 AC6（DW_FORM_exprloc）和 GCC（DW_FORM_sec_offset 位置列表）。

        Returns:
            绝对内存地址，或 None（如果无法提取，如变量在寄存器中）。
        """
        loc_attr = die.attributes.get('DW_AT_location')
        if loc_attr is None:
            return None

        try:


            if hasattr(loc_attr, 'value') and isinstance(loc_attr.value, list):
                expr = loc_attr.value


                if len(expr) >= 5 and expr[0] == 0x03:
                    addr_bytes = bytes(expr[1:5])
                    addr = int.from_bytes(addr_bytes, byteorder='little')

                    if addr == 0:
                        return None
                    return addr

                if len(expr) >= 9 and expr[0] == 0x03:
                    addr_bytes = bytes(expr[1:9])
                    addr = int.from_bytes(addr_bytes, byteorder='little')
                    if addr == 0:
                        return None
                    return addr



            if isinstance(loc_attr.value, int) and self._location_parser is not None:
                try:
                    dwarf_ver = die.cu['version']
                    loc_list = self._location_parser.parse_from_attribute(
                        loc_attr, dwarf_ver, die
                    )
                    if isinstance(loc_list, LocationExpr):

                        loc_expr = loc_list.loc_expr
                        if len(loc_expr) >= 5 and loc_expr[0] == 0x03:
                            addr_bytes = bytes(loc_expr[1:5])
                            return int.from_bytes(addr_bytes, byteorder='little')
                    elif isinstance(loc_list, list) and len(loc_list) > 0:

                        for entry in loc_list:
                            if hasattr(entry, 'loc_expr'):
                                loc_expr = entry.loc_expr
                                if len(loc_expr) >= 5 and loc_expr[0] == 0x03:
                                    addr_bytes = bytes(loc_expr[1:5])
                                    return int.from_bytes(addr_bytes, byteorder='little')
                except Exception:
                    pass
        except Exception:
            pass

        return None

    def _resolve_die_byte_size(self, die, CU) -> int:
        """从 DW_AT_type 链推导变量的字节大小。

        当地址来自 DWARF（而非 symtab）时，需要此方法推导 size。
        """
        type_attr = die.attributes.get('DW_AT_type')
        if not type_attr:
            return 0
        try:
            ref_die = self._get_type_die(type_attr, CU)
            if ref_die is None:
                return 0
            base_die = self._resolve_base_type(ref_die, CU)
            if base_die is None:
                return 0
            sz = base_die.attributes.get('DW_AT_byte_size')
            if sz and isinstance(sz.value, int):
                return sz.value

            if base_die.tag == 'DW_TAG_array_type':
                arr_info = self._resolve_array_info(die, CU)
                if arr_info:
                    _, elem_size, count, _ = arr_info
                    return elem_size * count
        except Exception:
            pass
        return 0



    def _get_source_file(self, die, CU) -> str:
        """获取变量声明所在的源文件路径。

        从 DWARF line program 的文件表中查找 DW_AT_decl_file 对应的文件名。
        返回去掉目录前缀的文件名，如 "main.c" 或 "sensor.cpp"。
        """
        decl_file = die.attributes.get('DW_AT_decl_file')
        if not decl_file:
            return ''

        try:
            file_index = decl_file.value
            if file_index == 0:
                return ''


            dwarf_info = self._dwarf_info
            lineprog = dwarf_info.line_program_for_CU(CU)
            if lineprog is None:
                return ''

            header = lineprog.header
            file_entries = header.file_entry


            if file_index < 1 or file_index > len(file_entries):
                return ''

            entry = file_entries[file_index - 1]
            file_name = entry.name
            if isinstance(file_name, bytes):
                file_name = file_name.decode('utf-8', errors='replace')


            dir_index = entry.dir_index
            comp_dir = ''
            top = CU.get_top_DIE()
            cd_attr = top.attributes.get('DW_AT_comp_dir')
            if cd_attr:
                comp_dir = cd_attr.value.decode('utf-8', errors='replace')

            if dir_index > 0 and dir_index <= len(header.include_directory):
                inc_dir = header.include_directory[dir_index - 1]
                if isinstance(inc_dir, bytes):
                    inc_dir = inc_dir.decode('utf-8', errors='replace')

                full_path = os.path.join(inc_dir, file_name)

                if comp_dir and full_path.startswith(comp_dir):
                    full_path = full_path[len(comp_dir):].lstrip('/\\')
                file_name = full_path

            return os.path.basename(file_name) if file_name else ''
        except Exception:
            return ''


def load_elf(elf_path: str) -> ElfSymbolReader:
    """便捷函数：加载 ELF/AXF 文件并返回已解析的 reader。"""
    reader = ElfSymbolReader()
    reader.load(elf_path)
    return reader
