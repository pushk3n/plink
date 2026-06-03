#!/usr/bin/env python3
"""Simple code cleaner: removes comments and pseudo block comments."""

import ast
import tokenize
import io
from pathlib import Path


def remove_comments_tokenize(source: str) -> str:
    """Use tokenize to remove comments."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenError:
        return source

    cleaned_tokens = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        cleaned_tokens.append(tok)

    return tokenize.untokenize(cleaned_tokens)


def _get_string_expr_value(node: ast.AST) -> str | None:
    """Return string value when node is a standalone string expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _collect_docstring_nodes(tree: ast.AST) -> set[int]:
    """Collect ids of real docstring expression nodes."""
    docstring_nodes: set[int] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not node.body:
            continue

        first_stmt = node.body[0]
        if not isinstance(first_stmt, ast.Expr):
            continue
        if _get_string_expr_value(first_stmt.value) is None:
            continue

        docstring_nodes.add(id(first_stmt))

    return docstring_nodes


def _remove_source_spans(source: str, spans: list[tuple[int, int, int, int]]) -> str:
    """Remove source spans identified by line and column offsets."""
    if not spans:
        return source

    lines = source.splitlines(keepends=True)
    line_offsets = [0]
    total = 0
    for line in lines:
        total += len(line)
        line_offsets.append(total)

    def to_offset(line_no: int, col_no: int) -> int:
        return line_offsets[line_no - 1] + col_no

    ranges: list[tuple[int, int]] = []
    for start_line, start_col, end_line, end_col in spans:
        start = to_offset(start_line, start_col)
        end = to_offset(end_line, end_col)
        if end > start:
            ranges.append((start, end))

    if not ranges:
        return source

    ranges.sort()
    merged: list[list[int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    result = []
    cursor = 0
    for start, end in merged:
        result.append(source[cursor:start])
        cursor = end
    result.append(source[cursor:])
    return ''.join(result)


def remove_pseudo_block_comments(source: str) -> str:
    """Remove standalone string expressions that are not docstrings."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    docstring_nodes = _collect_docstring_nodes(tree)
    spans: list[tuple[int, int, int, int]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        if id(node) in docstring_nodes:
            continue
        if _get_string_expr_value(node.value) is None:
            continue

        end_lineno = getattr(node, 'end_lineno', None)
        end_col_offset = getattr(node, 'end_col_offset', None)
        if end_lineno is None or end_col_offset is None:
            continue

        spans.append((node.lineno, node.col_offset, end_lineno, end_col_offset))

    return _remove_source_spans(source, spans)


def remove_trailing_whitespace(source: str) -> str:
    """Remove trailing whitespace from each line."""
    lines = source.split('\n')
    return '\n'.join(line.rstrip() for line in lines)


def clean_file(file_path: str) -> bool:
    """Clean a single file. Returns True if successful."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            source = f.read()

        # Step 1: Remove standalone pseudo block comments, keep docstrings
        cleaned = remove_pseudo_block_comments(source)

        # Step 2: Remove line comments
        cleaned = remove_comments_tokenize(cleaned)

        # Step 3: Clean up trailing whitespace left by comment removal
        cleaned = remove_trailing_whitespace(cleaned)

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(cleaned)

        # Verify syntax
        try:
            compile(cleaned, file_path, 'exec')
            print(f"✓ {file_path}")
            return True
        except SyntaxError as e:
            print(f"✗ {file_path}: {e}")
            return False

    except Exception as e:
        print(f"✗ {file_path}: {e}")
        return False


def main():
    base_dir = Path(__file__).parent
    success = 0
    fail = 0

    for file_path in sorted(base_dir.rglob('*.py')):
        if file_path.name == 'clean_code.py':
            continue
        if clean_file(str(file_path)):
            success += 1
        else:
            fail += 1

    print(f"\n完成: {success} 成功, {fail} 失败")


if __name__ == '__main__':
    main()
