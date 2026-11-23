"""Find laws without running any code (reads Python files with ``ast``).

Used by the lock (per-law sentence and hash), the integrity check (which must not run code),
reports and drafting. Recognises::

    @law("...")                 # from lawkeep import law
    @lawkeep.law("...")         # import lawkeep
    @lk.law("...")              # import lawkeep as lk
    @keep("...")                # from lawkeep import law as keep

on functions (sync or async) and classes. Law ids match ``lawkeep._law.make_law_id``.
"""

from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from lawkeep._law import make_law_id
from lawkeep.models import LawInfo
from lawkeep.paths import DRAFTS_DIRNAME, laws_dir, rel

#: Files under laws/ that hold helpers, not laws (they are still fingerprinted).
NON_LAW_FILES = ("conftest.py", "strategies.py")
SKIP_DIRS = ("__pycache__", ".hypothesis", ".pytest_cache", ".mypy_cache", ".ruff_cache")


@dataclass
class DiscoveryResult:
    laws: list[LawInfo] = field(default_factory=list["LawInfo"])
    problems: list[str] = field(default_factory=list[str])  # plain English

    def by_id(self) -> dict[str, LawInfo]:
        return {law.law_id: law for law in self.laws}


def _law_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Names bound to ``lawkeep.law`` and names bound to the ``lawkeep`` module."""
    direct: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("lawkeep", "lawkeep._law"):
            for alias in node.names:
                if alias.name == "law":
                    direct.add(alias.asname or "law")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "lawkeep":
                    modules.add(alias.asname or "lawkeep")
    return direct, modules


def _law_call(dec: ast.expr, direct: set[str], modules: set[str]) -> ast.Call | None:
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if isinstance(func, ast.Name) and func.id in direct:
        return dec
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "law"
        and isinstance(func.value, ast.Name)
        and func.value.id in modules
    ):
        return dec
    return None


def _segment_hash(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    lines = source.splitlines(keepends=True)
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    end = node.end_lineno or node.lineno
    text = "".join(lines[start - 1 : end])
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def laws_in_source(
    source: str,
    *,
    file: str | os.PathLike[str],
    root: str | os.PathLike[str],
    filename_for_errors: str | None = None,
) -> DiscoveryResult:
    """Find laws in one file's source text. ``file`` is its path (absolute or root-relative)."""
    root = Path(root)
    path = Path(file) if Path(file).is_absolute() else root / file
    rel_file = rel(root, path)
    result = DiscoveryResult()
    try:
        tree = ast.parse(source, filename=filename_for_errors or rel_file)
    except SyntaxError as exc:
        result.problems.append(
            f"{rel_file} line {exc.lineno}: this law file has a syntax error ({exc.msg})."
        )
        return result
    direct, modules = _law_names(tree)
    if not direct and not modules:
        return result
    ldir = laws_dir(root)
    is_draft = rel_file.startswith(f"laws/{DRAFTS_DIRNAME}/")

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            qualname = f"{prefix}{node.name}"
            for dec in node.decorator_list:
                call = _law_call(dec, direct, modules)
                if call is None:
                    continue
                sentence = None
                if (
                    call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    sentence = " ".join(call.args[0].value.split())
                else:
                    result.problems.append(
                        f"{rel_file} line {node.lineno}: the sentence of law {qualname} is not "
                        f"a plain string, so lawkeep cannot show it without running code."
                    )
                explicit = None
                for kw in call.keywords:
                    if (
                        kw.arg == "id"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        explicit = kw.value.value
                law_id = explicit or make_law_id(qualname, file=path, laws_dir=ldir)
                kind = "function"
                if isinstance(node, ast.ClassDef):
                    bases = {
                        b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                        for b in node.bases
                    }
                    kind = "state_machine" if "RuleBasedStateMachine" in bases else "class"
                result.laws.append(
                    LawInfo(
                        law_id=law_id,
                        sentence=sentence,
                        file=rel_file,
                        qualname=qualname,
                        line=node.lineno,
                        kind=kind,
                        source_hash=_segment_hash(source, node),
                        is_draft=is_draft,
                        explicit_id=explicit is not None,
                    )
                )
                break
            if isinstance(node, ast.ClassDef):
                visit(node.body, f"{qualname}.")

    visit(tree.body, "")
    return result


def laws_in_file(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> DiscoveryResult:
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return DiscoveryResult(problems=[f"Cannot read {rel(Path(root), p)}: {exc}"])
    return laws_in_source(source, file=p, root=root)


def iter_python_files(root: str | os.PathLike[str], *, include_drafts: bool = False) -> list[Path]:
    """All ``*.py`` files under ``laws/`` that may hold laws, sorted."""
    ldir = laws_dir(Path(root))
    if not ldir.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(ldir.rglob("*.py")):
        parts = p.relative_to(ldir).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if not include_drafts and parts and parts[0] == DRAFTS_DIRNAME:
            continue
        if p.name in NON_LAW_FILES:
            continue
        out.append(p)
    return out


def discover_laws(root: str | os.PathLike[str], *, include_drafts: bool = False) -> DiscoveryResult:
    """Every law in ``laws/`` (and ``laws/drafts/`` when asked), without running code.

    Duplicate law ids are reported in ``problems`` (both laws are still listed).
    """
    result = DiscoveryResult()
    for path in iter_python_files(root, include_drafts=include_drafts):
        part = laws_in_file(path, root)
        result.laws.extend(part.laws)
        result.problems.extend(part.problems)
    seen: dict[str, LawInfo] = {}
    for info in result.laws:
        if info.law_id in seen and seen[info.law_id].is_draft == info.is_draft:
            other = seen[info.law_id]
            result.problems.append(
                f"Two laws share the id {info.law_id!r}: {other.file} line {other.line} and "
                f'{info.file} line {info.line}. Give one of them @law(..., id="...").'
            )
        seen.setdefault(info.law_id, info)
    return result


# ---------------------------------------------------------------------------------------------
# What a law's inputs mean (static; for the example tables people read)


def _find_def(tree: ast.Module, qualname: str) -> ast.AST | None:
    body: list[ast.stmt] = tree.body
    node: ast.AST | None = None
    for part in qualname.split("."):
        node = next(
            (
                n
                for n in body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == part
            ),
            None,
        )
        if node is None:
            return None
        body = getattr(node, "body", [])
    return node


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


_NOUNS = {
    "money": "amount of money",
    "decimals": "decimal number",
    "integers": "whole number",
    "floats": "number",
    "text": "piece of text",
    "non_empty_text": "piece of text",
    "characters": "character",
    "booleans": "true-or-false value",
    "lists": "list of values",
    "tuples": "group of values",
    "dictionaries": "table of values",
    "sets": "set of values",
    "dates": "date",
    "datetimes": "date and time",
    "times": "time of day",
    "percentages": "percentage",
    "ids": "id",
    "uuids": "id",
    "emails": "email address",
}
_GENERIC = {"sampled_from", "one_of", "just", "builds", "from_type", "data", "composite"}


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def made_up_description(name: str, node: ast.expr | None = None, text: str = "") -> str:
    """Plain words for an input the law does not describe: ``charge=st.charges()`` ->
    ``a sample charge that lawkeep made up for testing``."""
    func = ""
    if isinstance(node, ast.Call):
        func = _call_name(node.func)
    if func in _NOUNS:
        noun = _NOUNS[func]
    elif func and func not in _GENERIC:
        noun = _singular(func).replace("_", " ")
    else:
        noun = _singular(name).replace("_", " ")
        if func == "sampled_from":
            noun = f"{noun} picked from a fixed list"
    return f"a sample {noun} that lawkeep made up for testing"


def describe_inputs(source: str, qualname: str) -> dict[str, str]:
    """Plain words for each input of a law, read from the source without running it.

    Uses ``@law(..., inputs={"name": "description"})`` when given; for inputs it does not
    describe, falls back to words made from the generator in ``@given(name=...)``, e.g.
    ``"a sample amount of money that lawkeep made up for testing"``. Returns ``{}`` when nothing
    is known.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    node = _find_def(tree, qualname)
    if node is None:
        return {}
    explicit: dict[str, str] = {}
    generated: dict[str, str] = {}
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        name = _call_name(dec.func)
        for kw in dec.keywords:
            if name != "given" and kw.arg == "inputs" and isinstance(kw.value, ast.Dict):
                for k, v in zip(kw.value.keys, kw.value.values, strict=False):
                    if (
                        isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and isinstance(v, ast.Constant)
                        and isinstance(v.value, str)
                    ):
                        explicit[k.value] = " ".join(v.value.split())
            elif name == "given" and kw.arg:
                try:
                    text = ast.unparse(kw.value)
                except Exception:  # noqa: BLE001 - odd syntax: just skip the description
                    continue
                generated[kw.arg] = made_up_description(kw.arg, kw.value, text)
    out = dict(generated)
    out.update(explicit)
    order = list(generated) + [k for k in explicit if k not in generated]
    return {k: out[k] for k in order}


def describe_inputs_in_file(path: str | os.PathLike[str], qualname: str) -> dict[str, str]:
    try:
        source = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return describe_inputs(source, qualname)
