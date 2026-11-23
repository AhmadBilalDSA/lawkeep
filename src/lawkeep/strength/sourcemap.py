"""Where the functions are: a static (ast) index of the project's source files.

No project code runs here. Used by the strength check (coverage lines -> functions, gaps),
the planted-bug generator and the protection report (changed lines -> functions).

Function names in data are ``"module.path:qualname"`` (``"billing.refunds:refund"``,
``"billing.cart:Cart.total"``). Only top-level functions and methods of (possibly nested)
classes are indexed; functions defined inside other functions belong to their parent.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from lawkeep import paths
from lawkeep.config import Config

#: Folders never treated as project source.
EXCLUDED_DIRS = frozenset(
    {
        "laws",
        "tests",
        "test",
        "testing",
        "venv",
        "env",
        "build",
        "dist",
        "node_modules",
        "__pycache__",
        "site-packages",
        "mutants",
    }
)
#: Files never treated as project source.
EXCLUDED_FILES = frozenset({"conftest.py", "setup.py", "noxfile.py", "fabfile.py"})


@dataclass
class FunctionInfo:
    qualname: str  # "refund" or "Cart.total"
    file: str  # relative to the project root, forward slashes
    lineno: int  # the `def` line
    first_line: int  # first decorator line (or the def line)
    body_start: int  # first statement of the body (after the docstring, if any)
    end: int  # last line
    is_method: bool = False
    is_public: bool = True
    uses_zero_arg_super: bool = False
    decorators: list[str] = field(default_factory=list[str])

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]

    def contains(self, line: int) -> bool:
        return self.first_line <= line <= self.end

    def body_contains(self, line: int) -> bool:
        return self.body_start <= line <= self.end


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _uses_zero_arg_super(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "super"
            and not sub.args
        ):
            return True
    return False


def _body_start(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    body = node.body
    if (
        len(body) > 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1].lineno
    return body[0].lineno


def functions_in_tree(tree: ast.Module, file: str) -> list[FunctionInfo]:
    out: list[FunctionInfo] = []

    def visit(body: list[ast.stmt], prefix: str, in_class: bool, public: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{node.name}"
                first = min([d.lineno for d in node.decorator_list] + [node.lineno])
                out.append(
                    FunctionInfo(
                        qualname=qual,
                        file=file,
                        lineno=node.lineno,
                        first_line=first,
                        body_start=_body_start(node),
                        end=node.end_lineno or node.lineno,
                        is_method=in_class,
                        is_public=public and not node.name.startswith("_"),
                        uses_zero_arg_super=in_class and _uses_zero_arg_super(node),
                        decorators=[_decorator_name(d) for d in node.decorator_list],
                    )
                )
            elif isinstance(node, ast.ClassDef):
                visit(
                    node.body,
                    f"{prefix}{node.name}.",
                    True,
                    public and not node.name.startswith("_"),
                )
            elif isinstance(node, (ast.If, ast.Try)) and not in_class:
                # `if TYPE_CHECKING:` / `try: ... except ImportError:` blocks at module level
                for block in (getattr(node, "body", []), getattr(node, "orelse", [])):
                    visit(block, prefix, in_class, public)

    visit(tree.body, "", False, True)
    return out


def functions_in_source(source: str, file: str) -> list[FunctionInfo]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return functions_in_tree(tree, file)


def functions_in_file(root: Path, path: Path) -> list[FunctionInfo]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return functions_in_source(source, paths.rel(root, path))


def function_at(funcs: list[FunctionInfo], line: int) -> FunctionInfo | None:
    """The innermost indexed function whose lines (decorators to end) contain ``line``."""
    best: FunctionInfo | None = None
    for f in funcs:
        if f.contains(line) and (best is None or f.first_line >= best.first_line):
            best = f
    return best


def function_running(funcs: list[FunctionInfo], line: int) -> FunctionInfo | None:
    """The innermost function whose *body* contains ``line`` (a line that runs at call time)."""
    best: FunctionInfo | None = None
    for f in funcs:
        if f.body_contains(line) and (best is None or f.body_start >= best.body_start):
            best = f
    return best


# ---------------------------------------------------------------------------------------------
# Project source files and module names


def _is_excluded(parts: tuple[str, ...]) -> bool:
    for part in parts[:-1]:
        if part in EXCLUDED_DIRS or part.startswith("."):
            return True
    name = parts[-1]
    if name in EXCLUDED_FILES:
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return False


def source_files(root: Path, cfg: Config) -> list[Path]:
    """Python files holding the code under test (``[project] source``), minus laws and tests."""
    root = Path(root).resolve()
    out: list[Path] = []
    seen: set[Path] = set()
    for sdir in cfg.project.source_dirs(root):
        if sdir.is_file() and sdir.suffix == ".py":
            candidates = [sdir]
        elif sdir.is_dir():
            candidates = sorted(sdir.rglob("*.py"))
        else:
            continue
        for p in candidates:
            try:
                rel_parts = p.resolve().relative_to(root).parts
            except ValueError:
                rel_parts = p.parts
            if _is_excluded(rel_parts):
                continue
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(rp)
    return out


def is_source_file(root: Path, cfg: Config, path: Path) -> bool:
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    p = p.resolve()
    if p.suffix != ".py":
        return False
    try:
        parts = p.relative_to(Path(root).resolve()).parts
    except ValueError:
        return False
    if _is_excluded(parts):
        return False
    for sdir in cfg.project.source_dirs(Path(root)):
        if p == sdir or sdir in p.parents:
            return True
    return False


def module_name(root: Path, cfg: Config, file: str) -> str:
    """``"src/billing/refunds.py"`` -> ``"billing.refunds"`` (relative to its source folder)."""
    p = (Path(root) / file).resolve()
    base = Path(root).resolve()
    for sdir in sorted(cfg.project.source_dirs(Path(root)), key=lambda d: -len(d.parts)):
        if sdir.is_dir() and (p == sdir or sdir in p.parents):
            base = sdir
            break
    try:
        parts = list(p.relative_to(base).with_suffix("").parts)
    except ValueError:
        parts = list(Path(file).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or Path(file).stem


def display_name(root: Path, cfg: Config, file: str, qualname: str) -> str:
    return f"{module_name(root, cfg, file)}:{qualname}"


@dataclass
class SourceIndex:
    """All indexed functions of the project, by file and by display name."""

    root: Path
    by_file: dict[str, list[FunctionInfo]] = field(default_factory=dict[str, list["FunctionInfo"]])
    by_name: dict[str, FunctionInfo] = field(default_factory=dict[str, "FunctionInfo"])
    names: dict[tuple[str, str], str] = field(
        default_factory=dict[tuple[str, str], str]
    )  # (file, qualname) -> name

    def name_of(self, file: str, qualname: str) -> str:
        return self.names.get((file, qualname), f"{file}:{qualname}")

    def get(self, name: str) -> FunctionInfo | None:
        return self.by_name.get(name)


def build_index(root: Path, cfg: Config, files: list[Path] | None = None) -> SourceIndex:
    root = Path(root).resolve()
    idx = SourceIndex(root=root)
    for p in files if files is not None else source_files(root, cfg):
        funcs = functions_in_file(root, p)
        rel = paths.rel(root, p)
        idx.by_file[rel] = funcs
        for f in funcs:
            name = display_name(root, cfg, rel, f.qualname)
            idx.by_name[name] = f
            idx.names[(rel, f.qualname)] = name
    return idx


def short(name: str) -> str:
    """``"billing.refunds:Cart.total"`` -> ``"Cart.total()"`` for plain-English messages."""
    return name.split(":", 1)[-1] + "()"
