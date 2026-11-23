"""Planted bugs: small, deterministic changes to one function at a time (ast based).

Why not mutmut directly? mutmut 3 is driven only through its CLI: it copies the whole project
into a ``mutants/`` folder inside the working tree, reads its settings from the project's own
``setup.cfg``/``pyproject.toml``, runs the project's test suite and keeps its own results
database. It has no API to (a) plant bugs only in a chosen list of functions (the protection
report needs exactly the changed functions), or (b) say *which law* caught each bug, and it
does not run on Windows. So lawkeep uses this small mutator, with operators modelled on
mutmut's (comparison, arithmetic, boolean and constant changes, `return None`, statement
deletion). Bugs are planted only in memory, inside the strength worker process: the user's
files are never written. See docs/ARCHITECTURE.md (C) and the workstream report.

Every planted bug is identified by ``(file, qualname, index)``; the same source always gives
the same list, so the parent process and the worker agree on ids without sending code around.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar, cast

_CMP_SWAP: dict[type, type] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
_BIN_SWAP: dict[type, type] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
    ast.FloorDiv: ast.Mult,
    ast.Mod: ast.FloorDiv,
    ast.Pow: ast.Mult,
    ast.LShift: ast.RShift,
    ast.RShift: ast.LShift,
    ast.BitAnd: ast.BitOr,
    ast.BitOr: ast.BitAnd,
    ast.BitXor: ast.BitAnd,
}
_SYMBOL: dict[type, str] = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
    ast.LShift: "<<",
    ast.RShift: ">>",
    ast.BitAnd: "&",
    ast.BitOr: "|",
    ast.BitXor: "^",
    ast.And: "and",
    ast.Or: "or",
}


@dataclass
class Mutation:
    """One planted bug inside one function."""

    index: int
    line: int
    kind: str  # "compare" | "arithmetic" | "boolean" | "negation" | "constant" | "return" | "delete" | "condition"
    description: str  # plain English, e.g. "changed `<=` to `<`"

    def plain(self) -> str:
        return f"line {self.line}: {self.description}"


@dataclass
class _Site:
    line: int
    col: int
    kind: str
    description: str
    apply: Callable[[], None]


def _snippet(node: ast.AST, limit: int = 40) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # pragma: no cover - unparse handles every valid tree
        return "..."
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, (ast.JoinedStr,)) or (
        isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))
    )


class _Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.sites: list[_Site] = []

    # -- helpers ---------------------------------------------------------------------------
    def _add(self, node: ast.AST, kind: str, desc: str, apply: Callable[[], None]) -> None:
        self.sites.append(
            _Site(getattr(node, "lineno", 0), getattr(node, "col_offset", 0), kind, desc, apply)
        )

    def _statements(self, body: list[ast.stmt]) -> None:
        for i, stmt in enumerate(body):
            if i == 0 and _is_docstring(stmt):
                continue
            deletable = isinstance(stmt, ast.AugAssign) or (
                isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            )
            if deletable:

                def apply(body: list[ast.stmt] = body, i: int = i, stmt: ast.stmt = stmt) -> None:
                    body[i] = ast.copy_location(ast.Pass(), stmt)

                self._add(stmt, "delete", f"deleted `{_snippet(stmt)}`", apply)
            self.visit(stmt)

    # -- statements with bodies ---------------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # decorators, defaults and annotations are left alone; only the body is mutated
        self._statements(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._statements(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._statements(node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.body)

    def generic_visit(self, node: ast.AST) -> None:
        for name, value in ast.iter_fields(node):
            if name in ("decorator_list", "annotation", "returns", "type_comment"):
                continue
            if isinstance(value, list):
                items = cast("list[object]", value)
                if items and all(isinstance(v, ast.stmt) for v in items):
                    self._statements(cast("list[ast.stmt]", items))
                else:
                    for item in items:
                        if isinstance(item, ast.AST):
                            self.visit(item)
            elif isinstance(value, ast.AST):
                self.visit(value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        return  # never mutate inside f-strings

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_If(self, node: ast.If) -> None:
        if not isinstance(node.test, (ast.Compare, ast.BoolOp)):
            test = node.test

            def apply(node: ast.If = node, test: ast.expr = test) -> None:
                node.test = ast.copy_location(ast.UnaryOp(op=ast.Not(), operand=test), test)

            self._add(node, "condition", f"reversed the condition `if {_snippet(test)}`", apply)
        self.generic_visit(node)

    # -- expressions -----------------------------------------------------------------------
    def visit_Compare(self, node: ast.Compare) -> None:
        for i, op in enumerate(node.ops):
            new = _CMP_SWAP.get(type(op))
            if new is None:
                continue

            def apply(node: ast.Compare = node, i: int = i, new: type[ast.cmpop] = new) -> None:
                node.ops[i] = new()

            self._add(
                node,
                "compare",
                f"changed `{_SYMBOL[type(op)]}` to `{_SYMBOL[new]}` in `{_snippet(node)}`",
                apply,
            )
        self.generic_visit(node)

    def _arith(
        self, node: ast.AST, op: ast.operator, setter: Callable[[ast.operator], None]
    ) -> None:
        new = _BIN_SWAP.get(type(op))
        if new is None:
            return
        self._add(
            node,
            "arithmetic",
            f"changed `{_SYMBOL[type(op)]}` to `{_SYMBOL[new]}` in `{_snippet(node)}`",
            lambda: setter(new()),
        )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not (_is_str(node.left) or _is_str(node.right)):
            self._arith(node, node.op, lambda new, node=node: setattr(node, "op", new))
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if not _is_str(node.value):
            self._arith(node, node.op, lambda new, node=node: setattr(node, "op", new))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        new = ast.Or if isinstance(node.op, ast.And) else ast.And

        def apply(node: ast.BoolOp = node, new: type[ast.boolop] = new) -> None:
            node.op = new()

        self._add(
            node,
            "boolean",
            f"changed `{_SYMBOL[type(node.op)]}` to `{_SYMBOL[new]}` in `{_snippet(node)}`",
            apply,
        )
        self.generic_visit(node)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, ast.Not):
            operand = node.operand

            def apply(node: ast.UnaryOp = node, operand: ast.expr = operand) -> None:
                # `not x` -> `not not x` (= bool(x)): the negation is gone
                node.operand = ast.copy_location(ast.UnaryOp(op=ast.Not(), operand=operand), node)

            self._add(node, "negation", f"removed the `not` in `{_snippet(node)}`", apply)
        elif isinstance(node.op, ast.USub) and not isinstance(node.operand, ast.Constant):

            def apply_minus(node: ast.UnaryOp = node) -> None:
                node.op = ast.UAdd()

            self._add(
                node, "negation", f"removed the minus sign in `{_snippet(node)}`", apply_minus
            )
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        v = node.value
        if isinstance(v, bool):
            new: bool | int | float = not v
        elif isinstance(v, (int, float)) and not isinstance(v, complex):
            new = v + 1
        else:
            return

        def apply(node: ast.Constant = node, new: bool | int | float = new) -> None:
            node.value = new

        self._add(node, "constant", f"changed `{v!r}` to `{new!r}`", apply)

    def visit_Return(self, node: ast.Return) -> None:
        value = node.value
        if value is not None and not (isinstance(value, ast.Constant) and value.value is None):

            def apply(node: ast.Return = node, value: ast.expr = value) -> None:
                node.value = ast.copy_location(ast.Constant(value=None), value)

            self._add(node, "return", f"returned None instead of `{_snippet(value)}`", apply)
            self.visit(value)


def _sites(func: ast.AST) -> list[_Site]:
    c = _Collector()
    c.visit(func)
    # stable order: by position, ties keep discovery order
    return sorted(c.sites, key=lambda s: (s.line, s.col))


def find_function(tree: ast.Module, qualname: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find a top-level function or a method (``"Cart.total"``) by qualified name."""
    parts = qualname.split(".")

    def search(body: list[ast.stmt], i: int) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        for node in body:
            if isinstance(node, (ast.If, ast.Try)):
                blocks: tuple[list[ast.stmt], list[ast.stmt]] = (node.body, node.orelse)
                for block in blocks:
                    found = search(block, i)
                    if found is not None:
                        return found
                continue
            if getattr(node, "name", None) != parts[i]:
                continue
            if i == len(parts) - 1 and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
            if i < len(parts) - 1 and isinstance(node, ast.ClassDef):
                found = search(node.body, i + 1)
                if found is not None:
                    return found
        return None

    return search(tree.body, 0)


def list_mutations(source: str, qualname: str) -> list[Mutation]:
    """Every planted bug lawkeep can make in ``qualname`` (empty when not found)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    func = find_function(tree, qualname)
    if func is None:
        return []
    return [Mutation(i, s.line, s.kind, s.description) for i, s in enumerate(_sites(func))]


def mutated_module(source: str, qualname: str, index: int) -> ast.Module:
    """A module AST holding only ``qualname``'s definition with bug number ``index`` planted.

    Line numbers match the original file, so tracebacks point at the real lines.
    Raises ``LookupError`` when the function or the bug does not exist.
    """
    tree = ast.parse(source)
    func = find_function(tree, qualname)
    if func is None:
        raise LookupError(f"function {qualname} not found")
    func = copy.deepcopy(func)
    sites = _sites(func)
    if not 0 <= index < len(sites):
        raise LookupError(f"{qualname} has no planted bug #{index}")
    sites[index].apply()
    module = ast.Module(body=[func], type_ignores=[])
    ast.fix_missing_locations(module)
    return module


_T = TypeVar("_T")


def spread(items: Sequence[_T], limit: int | None) -> list[_T]:
    """At most ``limit`` items, evenly spread over the list (deterministic)."""
    if limit is None or limit <= 0 or len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[0]]
    step = (len(items) - 1) / (limit - 1)
    picked = sorted({round(i * step) for i in range(limit)})
    return [items[i] for i in picked]
