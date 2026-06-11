"""plink v5.0 - 变量列表/树形选择器

提供变量浏览、手动输入表达式、监控列表管理等功能。
v5.0: 枚举语义化显示、独立通道缩放/偏移、局部统计列。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal, QThread, QModelIndex
from PyQt6.QtGui import QColor, QBrush, QStandardItemModel, QStandardItem
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMenu,
    QInputDialog,
    QColorDialog,
    QSplitter,
    QMessageBox,
)

from ..core.data_types import VarWatchEntry, VariableInfo, VarType

logger = logging.getLogger(__name__)

# TYPE_CHECKING 导入，避免运行时循环导入
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..core.symbol_cache import SymbolCache

# 预设颜色列表
PRESET_COLORS = [
    "#00FF00", "#FF0000", "#0000FF", "#FFFF00", "#FF00FF", "#00FFFF",
    "#FF8000", "#8000FF", "#00FF80", "#FF0080", "#80FF00", "#0080FF",
]


class VariableTreeWidget(QWidget):
    """变量树形浏览器

    左侧显示全局变量树 (按文件分组)，支持结构体/类懒展开（含继承成员）。
    """

    variable_selected = pyqtSignal(str)  # 变量表达式被选中

    # 占位子节点标记，用于显示展开箭头
    _PLACEHOLDER_TAG = "__placeholder__"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()
        self._variables: list[dict] = []
        self._cache: Optional['SymbolCache'] = None

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 树形视图
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["变量", "类型"])
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        layout.addWidget(self._tree)

    def load_variables(self, variables: list[dict]) -> None:
        """加载全局变量列表 (v1.0 兼容接口)

        Args:
            variables: 变量列表 (来自 SymbolParser.browse_global_variables)
        """
        self._variables = variables
        self._tree.clear()

        # 按文件分组
        file_groups: dict[str, list[dict]] = {}
        for var in variables:
            file_name = var.get("file", "unknown")
            if file_name not in file_groups:
                file_groups[file_name] = []
            file_groups[file_name].append(var)

        # 构建树
        for file_name, vars_list in file_groups.items():
            file_item = QTreeWidgetItem(self._tree, [file_name])
            file_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "file"})

            for var in vars_list:
                var_name = var.get("name", "")
                var_type = var.get("type", "")
                var_item = QTreeWidgetItem(file_item, [var_name, var_type])
                var_item.setData(0, Qt.ItemDataRole.UserRole, {
                    "type": "variable",
                    "name": var_name,
                    "expression": var_name,
                })

    def populate(self, cache: 'SymbolCache') -> None:
        """用 SymbolCache 的数据填充树形视图 (v2.0 接口)。

        树结构（不自动展开）：
          └─ 文件名（来源文件，按字母排序）
               └─ 变量名 [类型] @ 地址  (结构体/类带展开箭头)
        """
        self._cache = cache
        self._tree.clear()
        tree = cache.get_tree()
        for filename in sorted(tree.keys()):
            file_item = QTreeWidgetItem(self._tree, [filename])
            file_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "file"})
            for var in sorted(tree[filename], key=lambda v: v.name):
                self._add_var_item(file_item, var)

    def _add_var_item(self, parent: QTreeWidgetItem, var: VariableInfo) -> QTreeWidgetItem:
        """添加变量节点，如果是结构体/类则加占位子节点以显示展开箭头。"""
        label = f"{var.name}  [{var.type_name}]  @ 0x{var.address:08x}"
        item = QTreeWidgetItem(parent, [label])
        item.setData(0, Qt.ItemDataRole.UserRole, {
            "type": "variable",
            "name": var.name,
            "expression": var.name,
            "var_info": var,
        })
        # 结构体/类：加占位子节点，让 QTreeWidget 显示 ▶ 箭头
        if var.is_struct and self._cache:
            placeholder = QTreeWidgetItem(item, ["..."])
            placeholder.setData(0, Qt.ItemDataRole.UserRole, {"type": self._PLACEHOLDER_TAG})
        return item

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        """双击变量项"""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data.get("type") == "variable":
            self.variable_selected.emit(data["expression"])

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        """展开节点：懒加载结构体/类的成员，以及嵌套类型和枚举值。"""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return

        item_type = data.get("type")

        # 普通变量或嵌套类型成员：检查占位符
        if item_type == "variable":
            if item.childCount() == 1:
                child_data = item.child(0).data(0, Qt.ItemDataRole.UserRole)
                if child_data and child_data.get("type") == self._PLACEHOLDER_TAG:
                    item.removeChild(item.child(0))
                    expression = data.get("expression", data.get("name", ""))
                    self._load_struct_children(item, expression)

        # 嵌套类型节点（如 "InnerStruct [struct]"）：展开成员
        elif item_type == "nested_type":
            if item.childCount() == 1:
                child_data = item.child(0).data(0, Qt.ItemDataRole.UserRole)
                if child_data and child_data.get("type") == self._PLACEHOLDER_TAG:
                    item.removeChild(item.child(0))
                    parent_prefix = data.get("parent_prefix", "")
                    type_name = data.get("type_name", "")
                    self._load_nested_type_children(item, parent_prefix, type_name)

    def _load_struct_children(self, item: QTreeWidgetItem, var_name: str) -> None:
        """加载结构体/类/枚举/数组/指针的成员子节点。"""
        if not self._cache:
            return

        var_info = self._cache.resolve(var_name)
        if not var_info:
            return

        # 数组类型：显示各元素
        if var_info.is_array:
            elements = self._cache.get_array_elements(var_name)
            for elem in elements:
                self._add_var_item(item, elem)
            return

        # 指针类型：显示指向的目标地址和类型
        if var_info.is_pointer:
            ptr_item = QTreeWidgetItem(item, [
                f"→ @ 0x{var_info.address:08x}  [{var_info.type_name}]"
            ])
            ptr_item.setData(0, Qt.ItemDataRole.UserRole, {
                "type": "pointer_target",
                "name": var_name,
            })
            return

        # 枚举类型：显示枚举值
        enum_values = self._cache.get_enum_values(var_info.type_name)
        if enum_values:
            for ename, evalue in enum_values:
                label = f"{ename} = {evalue}"
                val_item = QTreeWidgetItem(item, [label])
                val_item.setData(0, Qt.ItemDataRole.UserRole, {
                    "type": "enum_value",
                    "name": ename,
                    "value": evalue,
                })
            return

        # 尝试按来源类型分组（继承场景）
        groups = self._cache.get_struct_member_groups(var_name)
        if groups:
            # 多个来源类型 -> 分组显示
            for type_name, members in groups.items():
                if type_name == "self":
                    label = "▸ Members"
                else:
                    label = f"▸ [{type_name}]"
                self._add_group_node(item, label, members)
        else:
            # 单类型或无继承 -> 平铺显示
            members = self._cache.get_struct_members(var_name)
            for member in members:
                self._add_var_item(item, member)

        # 嵌套类型
        nested = self._cache.get_nested_types(var_info.type_name)
        if nested:
            self._add_nested_types_node(item, nested, var_name)

    def _add_group_node(
        self,
        parent: QTreeWidgetItem,
        label: str,
        members: list,
    ) -> None:
        """添加分组节点（如继承的基类），子节点为该组的成员。"""
        group_item = QTreeWidgetItem(parent, [label])
        group_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "group"})
        group_item.setForeground(0, QBrush(QColor("#888888")))
        for member in members:
            self._add_var_item(group_item, member)

    def _add_nested_types_node(
        self,
        parent: QTreeWidgetItem,
        nested_names: list[str],
        parent_var_name: str,
    ) -> None:
        """添加嵌套类型节点，使用缓存中的成员数据以支持递归展开。"""
        header = QTreeWidgetItem(parent, ["▸ Nested Types"])
        header.setData(0, Qt.ItemDataRole.UserRole, {"type": "group"})
        header.setForeground(0, QBrush(QColor("#888888")))
        for tname in nested_names:
            # 查找嵌套类型在缓存中的成员名（可能为 parent_var_name.nested_member）
            member_var_name = self._find_nested_member_var(parent_var_name, tname)
            if member_var_name:
                # 嵌套类型成员已在缓存中，使用 _add_var_item 以支持递归展开
                member_info = self._cache.resolve(member_var_name)
                if member_info:
                    type_item = QTreeWidgetItem(header, [f"{tname}  [{member_info.type_name}]"])
                    type_item.setData(0, Qt.ItemDataRole.UserRole, {
                        "type": "variable",
                        "name": member_var_name,
                        "expression": member_var_name,
                        "var_info": member_info,
                    })
                    if member_info.is_struct:
                        placeholder = QTreeWidgetItem(type_item, ["..."])
                        placeholder.setData(0, Qt.ItemDataRole.UserRole, {"type": self._PLACEHOLDER_TAG})
                    continue
            # 回退：显示为只读嵌套类型（无缓存数据）
            item = QTreeWidgetItem(header, [tname])
            item.setData(0, Qt.ItemDataRole.UserRole, {
                "type": "nested_type",
                "type_name": tname,
                "parent_prefix": parent_var_name,
            })
            # 加占位子节点以显示展开箭头
            placeholder = QTreeWidgetItem(item, ["..."])
            placeholder.setData(0, Qt.ItemDataRole.UserRole, {"type": self._PLACEHOLDER_TAG})

    def _find_nested_member_var(self, parent_var_name: str, nested_type_name: str) -> str:
        """在缓存中查找嵌套类型对应的成员变量完整名。

        遍历 parent_var_name 的直接子成员，找到类型名匹配的成员。
        """
        if not self._cache:
            return ""
        members = self._cache.get_struct_members(parent_var_name)
        for m in members:
            if m.type_name == nested_type_name or m.type_name.endswith('::' + nested_type_name):
                return m.name
        return ""

    def _load_nested_type_children(
        self,
        item: QTreeWidgetItem,
        parent_prefix: str,
        type_name: str,
    ) -> None:
        """加载嵌套类型的成员子节点（回退路径，当缓存中无数据时使用）。"""
        if not self._cache:
            return
        members = self._cache.get_struct_def_members(type_name)
        for m in members:
            label = f"{m.name}  [{m.type_name}]  @ +0x{m.address:x}"
            m_item = QTreeWidgetItem(item, [label])
            m_item.setData(0, Qt.ItemDataRole.UserRole, {
                "type": "variable",
                "name": m.name,
                "expression": m.name,
                "var_info": m,
            })
            if m.is_struct:
                placeholder = QTreeWidgetItem(m_item, ["..."])
                placeholder.setData(0, Qt.ItemDataRole.UserRole, {"type": self._PLACEHOLDER_TAG})

    def _on_context_menu(self, pos) -> None:
        """右键菜单"""
        item = self._tree.itemAt(pos)
        if not item:
            return

        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return

        menu = QMenu(self)
        if data.get("type") == "variable":
            add_action = menu.addAction("添加到监控")
            action = menu.exec(self._tree.mapToGlobal(pos))
            if action == add_action:
                self.variable_selected.emit(data["expression"])


class _SelectableTable(QTableWidget):
    """支持单击选中、点击空白处取消选中、Delete 键删除的表格。"""

    delete_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

    def mousePressEvent(self, event):
        """单击：点到行时选中，点到空白处取消选中。"""
        index = self.indexAt(event.pos())
        if index.isValid():
            super().mousePressEvent(event)
        else:
            self.clearSelection()

    def keyPressEvent(self, event):
        """Delete 键：触发删除信号。"""
        if event.key() == Qt.Key.Key_Delete:
            self.delete_requested.emit()
        else:
            super().keyPressEvent(event)


class WatchTableWidget(QWidget):
    """变量监控表格

    v5.0: 11 列 — 启用、变量名、当前值、类型、地址、颜色、Scale、Offset、Max、Min、Avg
    """

    variable_removed = pyqtSignal(int)  # buffer_id
    variable_color_changed = pyqtSignal(int, str)  # buffer_id, color
    variable_enabled_changed = pyqtSignal(int, bool)  # buffer_id, enabled
    variable_value_changed = pyqtSignal(str, str)  # expression, value
    variable_scale_changed = pyqtSignal(int, float, float)  # buffer_id, scale, offset

    # 列索引常量
    COL_ENABLED = 0
    COL_NAME = 1
    COL_VALUE = 2
    COL_TYPE = 3
    COL_ADDRESS = 4
    COL_COLOR = 5
    COL_SCALE = 6
    COL_OFFSET = 7
    COL_MAX = 8
    COL_MIN = 9
    COL_AVG = 10

    _HEADER_LABELS = ["启用", "变量名", "当前值", "类型", "地址", "颜色", "Scale", "Offset", "Max", "Min", "Avg"]

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._entries: list[VarWatchEntry] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 表格
        self._table = _SelectableTable()
        self._table.setColumnCount(len(self._HEADER_LABELS))
        self._table.setHorizontalHeaderLabels(self._HEADER_LABELS)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_VALUE, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self._table.delete_requested.connect(self._on_delete_key)
        layout.addWidget(self._table)

    def update_entries(self, entries: list[VarWatchEntry]) -> None:
        """更新监控列表"""
        self._entries = entries
        self._table.setRowCount(len(entries))

        for i, entry in enumerate(entries):
            # 启用复选框
            check = QTableWidgetItem()
            check.setCheckState(
                Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked
            )
            self._table.setItem(i, self.COL_ENABLED, check)

            # 变量名
            self._table.setItem(i, self.COL_NAME, QTableWidgetItem(entry.expression))

            # 当前值 (稍后更新)
            self._table.setItem(i, self.COL_VALUE, QTableWidgetItem("---"))

            # 类型
            type_name = entry.var_info.type_name if entry.var_info else "unknown"
            self._table.setItem(i, self.COL_TYPE, QTableWidgetItem(type_name))

            # 地址
            addr_str = f"0x{entry.address:08X}" if entry.address else "---"
            self._table.setItem(i, self.COL_ADDRESS, QTableWidgetItem(addr_str))

            # 颜色
            color_item = QTableWidgetItem("■")
            color_item.setForeground(QBrush(QColor(entry.color)))
            self._table.setItem(i, self.COL_COLOR, color_item)

            # Scale
            self._table.setItem(i, self.COL_SCALE, QTableWidgetItem(f"{entry.scale:.6g}"))

            # Offset
            self._table.setItem(i, self.COL_OFFSET, QTableWidgetItem(f"{entry.offset:.6g}"))

            # Max/Min/Avg (稍后更新)
            self._table.setItem(i, self.COL_MAX, QTableWidgetItem("---"))
            self._table.setItem(i, self.COL_MIN, QTableWidgetItem("---"))
            self._table.setItem(i, self.COL_AVG, QTableWidgetItem("---"))

    def update_value(self, buffer_id: int, value: float) -> None:
        """更新变量值（含枚举语义化显示）。"""
        for i, entry in enumerate(self._entries):
            if entry.buffer_id == buffer_id:
                value_item = self._table.item(i, self.COL_VALUE)
                if value_item:
                    # 枚举语义化显示
                    if entry.var_info and entry.var_info.enum_values:
                        int_val = int(value)
                        names = entry.var_info.enum_values.get(int_val)
                        if names:
                            value_item.setText(f"{names[0]}({int_val})")
                        else:
                            value_item.setText(f"{value:.6g}")
                    else:
                        value_item.setText(f"{value:.6g}")
                break

    def update_stats(self, buffer_id: int, min_v: float, max_v: float, avg_v: float) -> None:
        """更新局部统计值（Max/Min/Avg）。"""
        for i, entry in enumerate(self._entries):
            if entry.buffer_id == buffer_id:
                max_item = self._table.item(i, self.COL_MAX)
                if max_item:
                    max_item.setText(f"{max_v:.6g}")
                min_item = self._table.item(i, self.COL_MIN)
                if min_item:
                    min_item.setText(f"{min_v:.6g}")
                avg_item = self._table.item(i, self.COL_AVG)
                if avg_item:
                    avg_item.setText(f"{avg_v:.6g}")
                break

    def _on_context_menu(self, pos) -> None:
        """右键菜单"""
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._entries):
            return

        entry = self._entries[row]
        menu = QMenu(self)

        color_action = menu.addAction("更改颜色")
        remove_action = menu.addAction("删除")
        menu.addSeparator()
        modify_action = menu.addAction("修改值")
        copy_addr_action = menu.addAction("复制地址")

        action = menu.exec(self._table.mapToGlobal(pos))

        if action == color_action:
            color = QColorDialog.getColor(QColor(entry.color), self)
            if color.isValid():
                self.variable_color_changed.emit(entry.buffer_id, color.name())
        elif action == remove_action:
            self.variable_removed.emit(entry.buffer_id)
        elif action == modify_action:
            self._modify_value(entry)
        elif action == copy_addr_action:
            from PyQt6.QtWidgets import QApplication
            clipboard = QApplication.clipboard()
            if clipboard:
                clipboard.setText(f"0x{entry.address:08X}")

    def _on_delete_key(self) -> None:
        """处理 Delete 键：删除当前选中的变量。"""
        selected_rows = {idx.row() for idx in self._table.selectedIndexes()}
        for row in sorted(selected_rows, reverse=True):
            if 0 <= row < len(self._entries):
                self.variable_removed.emit(self._entries[row].buffer_id)
                break  # 单选模式下只删一个

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        """双击单元格"""
        if row < 0 or row >= len(self._entries):
            return

        entry = self._entries[row]

        # 当前值列：修改值
        if col == self.COL_VALUE:
            self._modify_value(entry)

        # Scale 列：编辑缩放因子
        elif col == self.COL_SCALE:
            self._edit_scale_offset(row, entry, is_scale=True)

        # Offset 列：编辑偏移
        elif col == self.COL_OFFSET:
            self._edit_scale_offset(row, entry, is_scale=False)

    def _modify_value(self, entry: VarWatchEntry) -> None:
        """修改变量值（含枚举选择支持）。"""
        # 枚举类型：使用下拉列表选择
        if entry.var_info and entry.var_info.enum_values:
            items = []
            for val, names in sorted(entry.var_info.enum_values.items()):
                items.append(f"{names[0]}({val})")
            if items:
                value, ok = QInputDialog.getItem(
                    self, "修改变量值",
                    f"选择 {entry.expression} 的新值:",
                    items, 0, False,
                )
                if ok and value:
                    self.variable_value_changed.emit(entry.expression, value)
                return

        # 普通类型：文本输入
        value, ok = QInputDialog.getText(
            self, "修改变量值",
            f"输入 {entry.expression} 的新值:"
        )
        if ok and value:
            self.variable_value_changed.emit(entry.expression, value)

    def _edit_scale_offset(self, row: int, entry: VarWatchEntry, is_scale: bool) -> None:
        """编辑 Scale 或 Offset 值。"""
        col = self.COL_SCALE if is_scale else self.COL_SCALE
        current = entry.scale if is_scale else entry.offset
        label = "缩放因子 (Scale)" if is_scale else "偏移量 (Offset)"

        value, ok = QInputDialog.getText(
            self, f"修改{label}",
            f"输入 {entry.expression} 的{label}:",
            text=f"{current:.6g}",
        )
        if not ok or not value:
            return

        try:
            new_val = float(value)
        except ValueError:
            QMessageBox.warning(self, "输入错误", "请输入有效的数字")
            return

        if is_scale:
            if abs(new_val) < 1e-6:
                QMessageBox.warning(self, "输入错误", "Scale 不能为零（会导致除零）")
                return
            entry.scale = new_val
        else:
            entry.offset = new_val

        # 更新表格显示
        self._table.setItem(row, self.COL_SCALE, QTableWidgetItem(f"{entry.scale:.6g}"))
        self._table.setItem(row, self.COL_OFFSET, QTableWidgetItem(f"{entry.offset:.6g}"))

        # 通知主窗口
        self.variable_scale_changed.emit(entry.buffer_id, entry.scale, entry.offset)


class VariableListPanel(QWidget):
    """变量列表面板（变量树浏览器 + 表达式输入）

    观察表格已拆分为独立的 WatchTableWidget，由主窗口通过 QDockWidget 管理，
    支持拖拽停靠到任意位置（类似 Keil 的 Watch 窗口）。
    """

    add_variable_requested = pyqtSignal(str)  # 表达式

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 手动输入区
        input_widget = QWidget()
        input_layout = QHBoxLayout(input_widget)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.addWidget(QLabel("表达式:"))
        self._expr_edit = QLineEdit()
        self._expr_edit.setPlaceholderText("输入变量表达式 (如 myVar, obj.member)")
        self._expr_edit.returnPressed.connect(self._on_add_variable)
        input_layout.addWidget(self._expr_edit)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._on_add_variable)
        input_layout.addWidget(add_btn)
        layout.addWidget(input_widget)

        # 变量树
        self._tree = VariableTreeWidget()
        self._tree.variable_selected.connect(self.add_variable_requested)
        layout.addWidget(self._tree, 1)  # stretch=1，变量树占据所有剩余空间

    def _on_add_variable(self) -> None:
        """添加变量"""
        expr = self._expr_edit.text().strip()
        if expr:
            self.add_variable_requested.emit(expr)
            self._expr_edit.clear()

    def load_variables(self, variables: list[dict]) -> None:
        """加载全局变量到树形视图 (v1.0 兼容接口)"""
        self._tree.load_variables(variables)

    def populate_tree(self, cache: 'SymbolCache') -> None:
        """用 SymbolCache 填充变量树 (v2.0 接口)"""
        self._tree.populate(cache)
