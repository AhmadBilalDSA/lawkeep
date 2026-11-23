"""Decide by evidence whether a law change is stricter, the same, looser or unclear.

Owner: workstream D (law lifecycle).

Evidence (PLAN.md "lawkeep propose"):

1. Run the old and the new version on the SAME inputs: inputs drawn from both versions'
   generators, the examples written in either law, and saved failing examples for the law.
2. Run both again on the same inputs against deliberately broken versions of the functions
   the law calls ("always returns 0", "returns 1 more than it should", "always raises" ...),
   and, when ``strength.catch_matrix`` (workstream C) is available, on the same planted bugs.
3. Compare what each version rejects:
   - the new one rejects everything the old one rejects, and more -> ``stricter``
   - both reject exactly the same cases (and at least one case is rejected) -> ``same``
   - the new one accepts cases the old one rejected (and rejects nothing new) -> ``looser``
   - both, or no case either version rejects, or nothing comparable -> ``unclear``

Obvious weakening edits are ``looser`` whatever the evidence says: deleting the law, adding
skip/xfail, lowering ``max_examples``, removing ``assert`` statements, adding ``assume()``.

The evidence is "same results on these cases", never a proof; reports say so.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from lawkeep import discovery
from lawkeep.config import Config
from lawkeep.errors import ConfigError
from lawkeep.examples_table import run_probe
from lawkeep.models import ExampleRow
from lawkeep.paths import rel

MAX_DIFF_ROWS = 6
PLANTED_BUGS_ENV = "LAWKEEP_DIRECTION_PLANTED_BUGS"
#: Inputs drawn per version for the evidence; fixed (not taken from the check profile) so the
#: same change always gets the same evidence.
DIRECTION_EXAMPLES = 100


def direction_seed(law_id: str, old_source: str, new_source: str) -> int:
    """The Hypothesis seed for comparing two versions of a law: a hash of the law id and both
    versions' source hashes. Same change -> same inputs -> same label, on every run."""
    h = hashlib.sha256(law_id.encode("utf-8"))
    for source in (old_source, new_source):
        h.update(b"\0" + hashlib.sha256(source.encode("utf-8")).digest())
    return int.from_bytes(h.digest()[:8], "big")


@dataclass
class DirectionResult:
    direction: str  # "stricter" | "same" | "looser" | "unclear"
    evidence: dict[str, Any] = field(default_factory=dict[str, Any])
    examples: list[ExampleRow] = field(
        default_factory=list["ExampleRow"]
    )  # cases where old and new differ
    explanation: str = ""  # plain English

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "evidence": self.evidence,
            "examples": [e.to_dict() for e in self.examples],
            "explanation": self.explanation,
        }

    @property
    def new_version_broken(self) -> bool:
        """True when the new version does not even import or cannot be found."""
        return self.evidence.get("new_status") == "import_error"


# ---------------------------------------------------------------------------------------------
# Static checks (no code runs)


def _node(source: str, qualname: str) -> ast.AST | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    body: list[ast.stmt] = tree.body
    found: ast.AST | None = None
    for part in qualname.split("."):
        found = None
        for n in body:
            if (
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == part
            ):
                found = n
                break
        if found is None:
            return None
        body = getattr(found, "body", [])
    return found


def _name(expr: ast.AST) -> str:
    if isinstance(expr, ast.Call):
        return _name(expr.func)
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Name):
        return expr.id
    return ""


def _skips(node: ast.AST) -> int:
    count = 0
    for dec in getattr(node, "decorator_list", []):
        if _name(dec) in ("skip", "skipif", "xfail"):
            count += 1
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _name(sub.func) in ("skip", "xfail", "skipTest"):
            count += 1
    return count


def _max_examples(node: ast.AST) -> int | None:
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Call) and _name(dec.func) == "settings":
            for kw in dec.keywords:
                if kw.arg == "max_examples" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, int):
                        return kw.value.value
    return None


def _count(node: ast.AST, kind: type) -> int:
    return sum(1 for sub in ast.walk(node) if isinstance(sub, kind))


def _assumes(node: ast.AST) -> int:
    return sum(
        1 for sub in ast.walk(node) if isinstance(sub, ast.Call) and _name(sub.func) == "assume"
    )


def static_weakening(
    old_source: str, old_qualname: str, new_source: str, new_qualname: str, default_examples: int
) -> list[str]:
    """Plain-English reasons why the edit obviously weakens the law (empty = none found)."""
    old = _node(old_source, old_qualname)
    new = _node(new_source, new_qualname)
    if old is None or new is None:
        return []
    reasons: list[str] = []
    if _skips(new) > _skips(old):
        reasons.append("it adds a skip or xfail marker, so the law would not really be checked")
    old_max = _max_examples(old) or default_examples
    new_max = _max_examples(new)
    if new_max is not None and new_max < old_max:
        reasons.append(f"it tries fewer inputs ({old_max} → {new_max} per run)")
    old_asserts, new_asserts = _count(old, ast.Assert), _count(new, ast.Assert)
    if new_asserts < old_asserts:
        reasons.append(f"it removes checks ({old_asserts} → {new_asserts} assert lines)")
    if _assumes(new) > _assumes(old):
        reasons.append("it adds assume(), which skips some inputs instead of checking them")
    return reasons


def is_skip_edit(old_source: str, old_qualname: str, new_source: str, new_qualname: str) -> bool:
    old = _node(old_source, old_qualname)
    new = _node(new_source, new_qualname)
    return old is not None and new is not None and _skips(new) > _skips(old)


# ---------------------------------------------------------------------------------------------


_BUILTIN_NAMES = frozenset(dir(builtins))


def _local_names(fn: ast.AST) -> set[str]:
    """Names bound inside the law: its arguments, assignment targets, loop/with/except
    variables, comprehension and lambda variables, and nested function/class names."""
    out: set[str] = set()
    for sub in ast.walk(fn):
        if isinstance(sub, ast.arg):
            out.add(sub.arg)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            out.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif (
            isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub is not fn
        ):
            out.add(sub.name)
    return out


class _Pairing:
    """Walk two ASTs in step and check they differ only by a consistent renaming of names.

    Every name in the old law must map to exactly one name in the new law and vice versa
    (alpha-equivalence): ``charge`` -> ``payment`` everywhere is a rename, but turning
    ``result.total <= charge.total`` into ``result.total <= result.total`` maps two old
    names onto one new name, so it is not. Attribute names, keyword argument names, numbers,
    strings, operators and the decorators (``@given(...)`` generators, ``@settings``,
    ``@example``) must stay exactly the same. Python built-ins (``abs``, ``min``, ``len`` ...)
    may not be renamed, and a renamed outside name (a function the law calls) must no longer
    exist under its old name (``still_defined``), otherwise the law would simply be pointed
    at a different function.
    """

    def __init__(
        self,
        old_fn: ast.AST,
        new_fn: ast.AST,
        still_defined: Callable[[str], bool] | None = None,
        old_imports: dict[str, str] | None = None,
        new_imports: dict[str, str] | None = None,
    ) -> None:
        self.fwd: dict[str, str] = {}
        self.back: dict[str, str] = {}
        self.old_locals = _local_names(old_fn)
        self.new_locals = _local_names(new_fn)
        self.still_defined = still_defined
        self.old_imports = old_imports or {}
        self.new_imports = new_imports or {}
        self.renamed_outside: list[tuple[str, str]] = []

    def _outside_rename_ok(self, a: str, b: str) -> bool:
        ta, tb = self.old_imports.get(a), self.new_imports.get(b)
        if ta is not None and ta == tb:
            return True  # only the local alias changed (import x as y): same function
        real = ta.rsplit(".", 1)[-1] if ta else a
        return not (self.still_defined is not None and self.still_defined(real))

    def name(self, a: str, b: str) -> bool:
        if self.fwd.get(a, b) != b or self.back.get(b, a) != a:
            return False
        if a != b:
            if a in _BUILTIN_NAMES or b in _BUILTIN_NAMES:
                return False
            if (a in self.old_locals) != (b in self.new_locals):
                return False
            if a not in self.old_locals and (a, b) not in self.renamed_outside:
                if not self._outside_rename_ok(a, b):
                    return False
                self.renamed_outside.append((a, b))
        self.fwd[a] = b
        self.back[b] = a
        return True

    def same(self, a: object, b: object, *, verbatim: bool = False) -> bool:
        if type(a) is not type(b):
            return False
        if isinstance(a, list):
            la, lb = cast("list[object]", a), cast("list[object]", b)
            return len(la) == len(lb) and all(
                self.same(x, y, verbatim=verbatim) for x, y in zip(la, lb, strict=True)
            )
        if not isinstance(a, ast.AST):
            return a == b
        assert isinstance(b, ast.AST)
        for fname in a._fields:
            va, vb = getattr(a, fname, None), getattr(b, fname, None)
            if fname in ("ctx", "type_comment"):
                continue
            if fname == "annotation" and isinstance(a, ast.arg):
                continue
            if fname == "decorator_list":
                if not self.same(va, vb, verbatim=True):
                    return False
                continue
            renamable = not verbatim and (
                (isinstance(a, ast.Name) and fname == "id")
                or (isinstance(a, ast.arg) and fname == "arg")
                or (
                    isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and fname == "name"
                )
                or (isinstance(a, ast.ExceptHandler) and fname == "name" and va and vb)
            )
            if renamable:
                if not self.name(str(va), str(vb)):
                    return False
                continue
            if not self.same(va, vb, verbatim=verbatim):
                return False
        return True


def rename_only(
    old_source: str,
    old_qualname: str,
    new_source: str,
    new_qualname: str,
    still_defined: Callable[[str], bool] | None = None,
) -> bool:
    """True when the new law differs from the old one only by a consistent renaming of names
    (a rename such as ``refund`` -> ``issue_refund``): same structure, numbers, strings,
    operators, attribute and keyword names, and identical decorators (see ``_Pairing``).

    ``still_defined(name)`` (optional) says whether an outside name the law used to call still
    exists in the code under test; if it does, renaming it is not a rename.

    The plan's ``rename_target`` permission ("update a law after a rename, same meaning
    confirmed") needs both this and same results on the same cases. Anything else that the
    evidence calls "same" is treated as "unclear", so a person decides (sampled cases can miss
    a weakening that only matters on inputs the generators never produce).
    """
    old, new = _node(old_source, old_qualname), _node(new_source, new_qualname)
    if old is None or new is None:
        return False
    pairing = _Pairing(
        old, new, still_defined, _module_imports(old_source), _module_imports(new_source)
    )
    return pairing.same(old, new)


def _module_imports(source: str) -> dict[str, str]:
    """Top-level imports: local name -> "module.attr" (``from m import a as b`` gives
    ``{"b": "m.a"}``; ``import m as n`` gives ``{"n": "m"}``)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for n in tree.body:
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                out[a.asname or a.name] = f"{n.module}.{a.name}"
        elif isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
    return out


def _source_defines(root: Path, cfg: Config):
    """``still_defined(name)``: is ``name`` defined at the top level of a source module?"""
    cache: set[str] | None = None

    def still_defined(name: str) -> bool:
        nonlocal cache
        if cache is None:
            cache = set()
            for d in cfg.project.source_dirs(root):
                if not d.is_dir():
                    continue
                for f in d.rglob("*.py"):
                    if any(
                        part in (".venv", "venv", "laws", "tests", "node_modules")
                        for part in f.parts
                    ):
                        continue
                    try:
                        tree = ast.parse(f.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
                        continue
                    for n in tree.body:
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            cache.add(n.name)
                        elif isinstance(n, ast.Assign):
                            cache.update(t.id for t in n.targets if isinstance(t, ast.Name))
        return name in cache

    return still_defined


def _is_example_decorator(dec: ast.AST) -> bool:
    return isinstance(dec, ast.Call) and _name(dec.func) == "example"


def _generator_parts(node: ast.AST) -> tuple[list[str], list[str]]:
    """(decorators other than @example, strategy calls inside the body), as text."""
    decs = [
        ast.dump(d, annotate_fields=False)
        for d in getattr(node, "decorator_list", [])
        if not _is_example_decorator(d)
    ]
    body_calls: list[str] = []
    for sub in ast.walk(node):
        if sub in getattr(node, "decorator_list", []):
            continue
        if isinstance(sub, ast.Call):
            func = sub.func
            base = func.value if isinstance(func, ast.Attribute) else None
            if _name(func) == "draw" or (
                isinstance(base, ast.Name) and base.id in ("st", "strategies")
            ):
                body_calls.append(ast.dump(sub, annotate_fields=False))
    return decs, sorted(body_calls)


def generators_changed(
    old_source: str, old_qualname: str, new_source: str, new_qualname: str
) -> bool:
    """True when the new version draws its inputs differently (``@given`` generators,
    ``@settings`` or strategies drawn inside the law). Adding ``@example`` cases is fine.

    Evidence from sampled inputs cannot show that new generators still cover everything the
    old ones did (``st.integers()`` -> ``st.just(0)`` plus one extra assert "rejects more" on
    the same inputs, yet the law then only ever checks 0), so such a change is never
    "stricter" or "same" on its own.
    """
    old = _node(old_source, old_qualname)
    new = _node(new_source, new_qualname)
    if old is None or new is None:
        return False
    od, ob = _generator_parts(old)
    nd, nb = _generator_parts(new)
    if od != nd or ob != nb:
        return True
    old_ex = [
        ast.dump(d, annotate_fields=False)
        for d in getattr(old, "decorator_list", [])
        if _is_example_decorator(d)
    ]
    new_ex = [
        ast.dump(d, annotate_fields=False)
        for d in getattr(new, "decorator_list", [])
        if _is_example_decorator(d)
    ]
    return any(e not in new_ex for e in old_ex)  # removing an @example is not "same"


# -- one changed number in a comparison: which way does it move the bound? ----------------


def _number(node: ast.AST) -> Decimal | None:
    """The number in ``5``, ``-5``, ``0.5``, ``Decimal("0.005")``, ``float("1e-3")`` ..."""
    try:
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not (isinstance(node.value, bool))
        ):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = _number(node.operand)
            if inner is None:
                return None
            return -inner if isinstance(node.op, ast.USub) else inner
        if (
            isinstance(node, ast.Call)
            and _name(node.func) in ("Decimal", "float", "int", "Fraction")
            and len(node.args) == 1
            and not node.keywords
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, (str, int, float))
        ):
            return Decimal(str(node.args[0].value))
    except (InvalidOperation, ValueError):
        return None
    return None


#: Accepted values of ``value OP bound`` as an interval: (low end, high end), each end
#: ``None`` (unbounded) or ``True``/``False`` for "includes the bound" / "excludes it".
_INTERVALS: dict[type, tuple[bool | None, bool | None]] = {
    ast.Lt: (None, False),
    ast.LtE: (None, True),
    ast.Gt: (False, None),
    ast.GtE: (True, None),
    ast.Eq: (True, True),
}
_FLIPPED: dict[type, type] = {
    ast.Lt: ast.Gt,
    ast.LtE: ast.GtE,
    ast.Gt: ast.Lt,
    ast.GtE: ast.LtE,
    ast.Eq: ast.Eq,
}
_OP_TEXT: dict[type, str] = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "=="}


def _finite(node: ast.AST) -> Decimal | None:
    n = _number(node)
    return n if n is not None and n.is_finite() else None  # NaN/infinity: not a limit to move


def _split_offset(node: ast.AST) -> tuple[str | None, Decimal]:
    """``bound`` as (rest, constant offset): ``amount + Decimal("0.05")`` -> ("amount", 0.05),
    ``limit - 2`` -> ("limit", -2), ``5 + x`` -> ("x", 5), ``-(-3)``/``Decimal("3")`` -> (None,
    3). ``rest`` is an ``ast.dump`` (compared, never run); anything else is (itself, 0)."""
    n = _finite(node)
    if n is not None:
        return None, n
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        right = _finite(node.right)
        if right is not None:
            rest, off = _split_offset(node.left)
            return rest, (off + right if isinstance(node.op, ast.Add) else off - right)
        left = _finite(node.left)
        if left is not None and isinstance(node.op, ast.Add):
            rest, off = _split_offset(node.right)
            return rest, left + off
    return ast.dump(node, annotate_fields=False), Decimal(0)


def _within(inner: tuple[Decimal, type], outer: tuple[Decimal, type]) -> bool:
    """Is every value accepted by ``value INNER_OP inner_bound`` also accepted by
    ``value OUTER_OP outer_bound`` (over all real numbers)? Bounds are offsets from a shared
    base, so only their difference matters."""
    (ib, iop), (ob, oop) = inner, outer
    ilo, ihi = _INTERVALS[iop]
    olo, ohi = _INTERVALS[oop]
    # low end: outer must start no later than inner
    if olo is not None:
        if ilo is None or ib < ob or (ib == ob and ilo and not olo):
            return False
    # high end: outer must end no earlier than inner
    if ohi is not None:
        if ihi is None or ib > ob or (ib == ob and ihi and not ohi):
            return False
    return True


_Chain = list[tuple[ast.AST, object]]


def _diff_points(a: object, b: object, chain: _Chain, out: list[_Chain]) -> None:
    """Collect the smallest differing places of two trees (each as its chain of ancestor
    pairs, ending with the differing pair itself when both are AST nodes)."""
    if isinstance(a, list) and isinstance(b, list):
        la, lb = cast("list[object]", a), cast("list[object]", b)
        if len(la) != len(lb):
            out.append(chain)
            return
        for x, y in zip(la, lb, strict=True):
            _diff_points(x, y, chain, out)
        return
    if isinstance(a, ast.AST) and isinstance(b, ast.AST) and type(a) is type(b):
        if ast.dump(a) == ast.dump(b):
            return
        here: _Chain = [*chain, (a, b)]
        for fname in a._fields:
            if fname in ("ctx", "type_comment"):
                continue
            _diff_points(getattr(a, fname, None), getattr(b, fname, None), here, out)
        return
    if isinstance(a, ast.AST):
        pair: tuple[ast.AST, object] = (a, cast("object", b))
        out.append([*chain, pair])
    elif a != b or type(cast("object", a)) is not type(cast("object", b)):
        out.append(chain)  # e.g. Constant(5) vs Constant(10): the Constant pair ends `chain`


def bound_direction(
    old_source: str, old_qualname: str, new_source: str, new_qualname: str
) -> tuple[str, str] | None:
    """When the only change is the limit of one comparison in an assert, say which way the
    law moved: ``("looser", why)`` or ``("stricter", why)``. None for anything else (the
    evidence path decides).

    Recognised (``b`` any expression, ``c``/``k`` number literals such as ``5``, ``-5``,
    ``0.5``, ``Decimal("0.05")``, ``float("1e-3")``):

    - a changed number: ``x <= Decimal("0.005")`` -> ``x <= Decimal("0.05")``;
    - a limit moved by a constant: ``x <= b`` -> ``x <= b + c`` / ``b - c`` / ``c + b`` /
      ``b - (-c)``, and ``b + k`` -> ``b + c`` (offsets on the same ``b`` are compared);
    - a changed comparison: ``<`` <-> ``<=``, ``>`` <-> ``>=``, ``==`` <-> ``<=``/``>=``;
    - any mix of these on one comparison, the limit on either side (``b >= x``).

    The two versions' accepted values are compared as intervals over the real numbers
    (``x <= b`` accepts (-inf, b]); a version accepting a superset is looser, a subset
    stricter. Conditions, all checked: exactly one comparison differs, it has one operator,
    only one side of it changed, it sits in an ``assert`` (not its message) and is reached
    only through ``and``/``or`` (no ``not``, ``if`` test, lambda, ``try`` or ``with``
    around it, which could turn its meaning around). Multiplication is not recognised
    (``b * 2`` is smaller than ``b`` when ``b`` is negative).

    Assumption, stated in the reason: the limit is an ordinary number (``int``, ``float``,
    ``Decimal``, ``Fraction``), for which ``b + c`` never wraps around (fixed-width integer
    types such as numpy's ``uint8`` would).
    """
    old = _node(old_source, old_qualname)
    new = _node(new_source, new_qualname)
    if old is None or new is None:
        return None
    points: list[_Chain] = []
    _diff_points(old, new, [], points)
    if not points:
        return None
    compares: list[tuple[int, tuple[ast.AST, object]]] = []
    for chain in points:
        idx = next(
            (i for i in range(len(chain) - 1, -1, -1) if isinstance(chain[i][0], ast.Compare)),
            None,
        )
        if idx is None:
            return None
        compares.append((idx, chain[idx]))
    first_idx, (ca, cb) = compares[0]
    if any(pair[0] is not ca for _, pair in compares):
        return None
    if not (isinstance(ca, ast.Compare) and isinstance(cb, ast.Compare)):
        return None  # the comparison was replaced by something else
    chain = points[0][:first_idx]
    # monotone position: Assert.test -> (BoolOp)* -> Compare, nothing that could invert it
    asserts = [i for i, (n, _) in enumerate(chain) if isinstance(n, ast.Assert)]
    if not asserts:
        return None
    ai = asserts[-1]
    if any(
        isinstance(n, (ast.Try, ast.With, ast.AsyncWith)) or type(n).__name__ == "TryStar"
        for n, _ in chain[:ai]
    ):
        return None
    path = [n for n, _ in chain[ai + 1 :]] + [ca]
    assert_node = chain[ai][0]
    assert isinstance(assert_node, ast.Assert)
    if path[0] is not assert_node.test or not all(isinstance(n, ast.BoolOp) for n in path[:-1]):
        return None
    if len(ca.ops) != 1 or len(cb.ops) != 1:
        return None
    op_a, op_b = type(ca.ops[0]), type(cb.ops[0])
    if op_a not in _INTERVALS or op_b not in _INTERVALS:
        return None
    left_same = ast.dump(ca.left) == ast.dump(cb.left)
    right_same = ast.dump(ca.comparators[0]) == ast.dump(cb.comparators[0])
    if left_same:  # value on the left: value OP bound
        bound_a, bound_b = ca.comparators[0], cb.comparators[0]
    elif right_same:  # value on the right: bound OP value == value FLIPPED(OP) bound
        bound_a, bound_b = ca.left, cb.left
        op_a, op_b = _FLIPPED[op_a], _FLIPPED[op_b]
    else:
        return None
    rest_a, off_a = _split_offset(bound_a)
    rest_b, off_b = _split_offset(bound_b)
    if rest_a != rest_b:
        return None
    if off_a == off_b and op_a is op_b:
        return None  # written differently, same limit: not this rule's call
    old_iv, new_iv = (off_a, op_a), (off_b, op_b)
    looser = _within(old_iv, new_iv)
    stricter = _within(new_iv, old_iv)
    if looser == stricter:  # neither (e.g. <= turned into >=) or both: not decidable here
        return None
    word = "looser" if looser else "stricter"
    old_text = ast.unparse(ca)
    new_text = ast.unparse(cb)
    why = (
        f"the check `{old_text}` becomes `{new_text}`, so the law "
        + ("allows more results than before" if looser else "allows fewer results than before")
        + " (assuming ordinary numbers)"
    )
    return word, why


def _ledger_inputs(root: Path, law_id: str) -> list[dict[str, str]]:
    """Saved failing examples for this law, as {arg: repr} dicts (format owned by B)."""
    try:
        from lawkeep import ledger

        entries = ledger.load(root)
    except Exception:  # noqa: BLE001 - B may not be done; the ledger is optional evidence
        return []
    out: list[dict[str, Any]] = []
    for e in entries:
        if e.get("law_id") != law_id:
            continue
        for key in ("example", "inputs", "simplest_failing_example"):
            ex = e.get(key)
            if isinstance(ex, dict) and ex and "call" not in ex:
                example = cast("dict[object, Any]", ex)
                out.append({str(k): v for k, v in example.items()})
                break
    return out


def _planted_bug_evidence(
    root: Path, cfg: Config, law_id: str, old_file: Path, new_file: Path, new_qualname: str
) -> dict[str, Any] | None:
    """Old vs new on the same planted bugs, if workstream C's catch_matrix is available."""
    try:
        from lawkeep import strength

        matrix = strength.catch_matrix(
            root, cfg, [rel(root, old_file), rel(root, new_file)], max_bugs=20
        )
    except Exception as exc:  # noqa: BLE001 - optional evidence; say why it is missing
        return {"available": False, "note": f"planted bugs not used ({type(exc).__name__})"}
    # When both files define the same law id, strength keys results "<id>@<relative file>".
    known = set(matrix.caught) | set(getattr(matrix, "tested", {}) or {})
    old_key = f"{law_id}@{rel(root, old_file)}"
    if old_key not in known:
        old_key = law_id
    new_key = f"{law_id}@{rel(root, new_file)}"
    if new_key not in known:
        candidates = [
            k for k in known if k != old_key and "@" not in k and k.endswith("." + new_qualname)
        ]
        if not candidates:
            return {
                "available": False,
                "note": "planted bugs ran but the new version was not found",
            }
        new_key = candidates[0]
    old_caught = set(matrix.caught.get(old_key, []))
    new_caught = set(matrix.caught.get(new_key, []))
    return {
        "available": True,
        "total": len(matrix.bugs),
        "old_caught": len(old_caught),
        "new_caught": len(new_caught),
        "only_old": sorted(old_caught - new_caught),
        "only_new": sorted(new_caught - old_caught),
    }


def _diff_row(
    case: dict[str, Any], old: str, new: str, note: str | None, *, real: bool = True
) -> ExampleRow:
    real_new = case["real"].get("new", {})
    result = real_new.get("result") or case["real"].get("old", {}).get("result")
    return ExampleRow(
        inputs=dict(case["inputs"]),
        result=result if real else None,
        holds=new in ("holds", "skipped"),
        old_holds=old in ("holds", "skipped"),
        note=note,
    )


def _use_planted_bugs(flag: bool | None) -> bool:
    if flag is not None:
        return flag
    return os.environ.get(PLANTED_BUGS_ENV, "1").strip().lower() not in ("0", "false", "no", "")


def decide_direction(
    root: Path,
    cfg: Config,
    law_id: str,
    old_file: Path | None,
    new_file: Path | None,
    *,
    planted_bugs: bool | None = None,
) -> DirectionResult:
    """``old_file=None`` means a new law; ``new_file=None`` means the law is deleted.

    ``new_file`` may live anywhere (e.g. a staged copy); the new version is read as if it were
    at ``old_file``'s place so the law keeps its id. ``planted_bugs`` (default: on, unless the
    environment variable ``LAWKEEP_DIRECTION_PLANTED_BUGS=0``) also compares both versions on
    workstream C's planted bugs when ``strength.catch_matrix`` is available.
    """
    root = Path(root).resolve()
    if old_file is None and new_file is None:
        raise ValueError("decide_direction needs an old or a new version")
    if new_file is None:
        return DirectionResult(
            "looser",
            evidence={"static": ["the law is deleted"], "summary": "the law is deleted"},
            explanation="Deleting a law removes all of its checks.",
        )
    new_file = Path(new_file).resolve()
    if old_file is None:
        return DirectionResult(
            "stricter",
            evidence={"summary": "a new law only adds checks"},
            explanation="A new law only adds checks.",
        )
    old_file = Path(old_file).resolve()
    old_source = old_file.read_text(encoding="utf-8")
    new_source = new_file.read_text(encoding="utf-8")
    old_found = discovery.laws_in_source(old_source, file=old_file, root=root).by_id()
    new_disc = discovery.laws_in_source(new_source, file=old_file, root=root)
    new_found = new_disc.by_id()
    old_info = old_found.get(law_id)
    if old_info is None:
        return DirectionResult(
            "stricter",
            evidence={"summary": "a new law only adds checks"},
            explanation=f"{law_id} is not a law yet, so this adds a new law.",
        )
    new_info = new_found.get(law_id)
    static: list[str] = []
    if new_info is None:
        if len(new_disc.laws) == 1 and len(old_found) == 1:
            new_info = new_disc.laws[0]
            static_note = f"the law is renamed to {new_info.law_id}"
        else:
            return DirectionResult(
                "looser",
                evidence={
                    "static": ["the law is missing from the new version"],
                    "summary": "the law is missing from the new version",
                },
                explanation=f"The new version no longer contains {law_id}, which deletes it.",
            )
    else:
        static_note = None
    try:
        default_examples = cfg.check.get_profile().examples
    except ConfigError:  # pragma: no cover - config validated earlier
        default_examples = 200
    static += static_weakening(
        old_source, old_info.qualname, new_source, new_info.qualname, default_examples
    )
    unclear_reasons: list[str] = []
    for other_id, other in old_found.items():
        if other_id == law_id:
            continue
        twin = new_found.get(other_id)
        if twin is None:
            static.append(f"it also deletes law {other_id}")
        elif twin.source_hash != other.source_hash:
            unclear_reasons.append(f"it also changes law {other_id}")

    other_changes = list(unclear_reasons)
    bound = bound_direction(old_source, old_info.qualname, new_source, new_info.qualname)
    if bound is not None and bound[0] == "looser":
        static.append(bound[1])

    data = run_probe(
        root,
        cfg,
        [
            {"name": "old", "file": str(old_file), "qualname": old_info.qualname},
            {"name": "new", "file": str(new_file), "qualname": new_info.qualname},
        ],
        examples=DIRECTION_EXAMPLES,
        mutants=True,
        extra_inputs=_ledger_inputs(root, law_id),
        seed=direction_seed(law_id, old_source, new_source),
    )
    evidence: dict[str, Any] = {"static": static}
    if static_note:
        evidence["note"] = static_note
    if "error" in data:
        evidence["summary"] = data["error"]
        direction = "looser" if static else "unclear"
        return DirectionResult(
            direction,
            evidence=evidence,
            explanation=_explain(direction, static, unclear_reasons, data["error"]),
        )
    vinfo = data.get("versions", {})
    evidence["old_status"] = vinfo.get("old", {}).get("status")
    evidence["new_status"] = vinfo.get("new", {}).get("status")
    if evidence["new_status"] == "import_error":
        msg = vinfo["new"].get("message") or "the new version does not run"
        evidence["summary"] = msg
        return DirectionResult("unclear", evidence=evidence, explanation=msg)

    mutants = {m["id"]: m["description"] for m in data.get("mutants", [])}
    old_only = new_only = both = comparable = 0
    old_caught: set[str] = set()
    new_caught: set[str] = set()
    rows_looser: list[ExampleRow] = []
    rows_stricter: list[ExampleRow] = []
    shown: set[tuple[str, str]] = set()  # one example row per broken version
    cases = data.get("cases", [])
    for case in cases:
        pairs = [
            (
                None,
                case["real"].get("old", {}).get("outcome"),
                case["real"].get("new", {}).get("outcome"),
            )
        ]
        pairs += [(mid, o.get("old"), o.get("new")) for mid, o in case.get("mutants", {}).items()]
        for mid, old, new in pairs:
            if old not in ("holds", "skipped", "fails") or new not in ("holds", "skipped", "fails"):
                continue
            comparable += 1
            o_rej, n_rej = old == "fails", new == "fails"
            if mid is not None:
                if o_rej:
                    old_caught.add(mid)
                if n_rej:
                    new_caught.add(mid)
            if o_rej and n_rej:
                both += 1
            elif o_rej and not n_rej:
                old_only += 1
                if len(rows_looser) < 40 and _first_for(shown, "looser", mid):
                    rows_looser.append(
                        _diff_row(
                            case, old, new, _note(mid, mutants, "now allowed"), real=mid is None
                        )
                    )
            elif n_rej and not o_rej:
                new_only += 1
                if len(rows_stricter) < 40 and _first_for(shown, "stricter", mid):
                    rows_stricter.append(
                        _diff_row(
                            case, old, new, _note(mid, mutants, "now rejected"), real=mid is None
                        )
                    )

    planted = None
    if _use_planted_bugs(planted_bugs):
        planted = _planted_bug_evidence(root, cfg, law_id, old_file, new_file, new_info.qualname)
    if planted and planted.get("available"):
        old_only += len(planted["only_old"])
        new_only += len(planted["only_new"])
        both += min(planted["old_caught"], planted["new_caught"])
        comparable += planted["total"]

    n_cases = len(cases)
    n_mut = len(mutants)
    evidence.update(
        {
            "cases": n_cases,
            "broken_versions": n_mut,
            "compared": comparable,
            "old_rejects_only": old_only,
            "new_rejects_only": new_only,
            "both_reject": both,
            "old_caught_broken_versions": len(old_caught),
            "new_caught_broken_versions": len(new_caught),
            "planted_bugs": planted,
        }
    )
    if comparable == 0:
        by_evidence = "unclear"
        why = "lawkeep could not run the old and the new version on the same inputs"
    elif old_only == 0 and new_only == 0:
        if both == 0:
            by_evidence = "unclear"
            why = (
                f"lawkeep tried {n_cases} inputs and {n_mut} deliberately broken versions of "
                f"the code, and neither version of the law ever failed, so there is nothing "
                f"to compare"
            )
        else:
            by_evidence = "same"
            why = (
                f"both versions gave the same answer on {n_cases} inputs and on {n_mut} "
                f"deliberately broken versions of the code (each caught {len(old_caught)})"
            )
    elif old_only == 0:
        by_evidence = "stricter"
        why = (
            f"the new version refuses everything the old one refused, plus {new_only} more "
            f"input(s); it caught {len(new_caught)} deliberately broken versions of the code, "
            f"the old one {len(old_caught)}"
        )
    elif new_only == 0:
        by_evidence = "looser"
        why = (
            f"the new version lets through {old_only} input(s) the old one refused; it caught "
            f"{len(new_caught)} deliberately broken versions of the code, the old one "
            f"{len(old_caught)}"
        )
    else:
        by_evidence = "unclear"
        why = (
            f"the new version lets through {old_only} input(s) the old one refused, and "
            f"refuses {new_only} the old one let through"
        )
    if planted and planted.get("available"):
        why += (
            f"; of {planted['total']} small bugs planted in a copy of the code, the old version "
            f"caught {planted['old_caught']} and the new version {planted['new_caught']}"
        )
    evidence["summary"] = why
    direction = by_evidence
    if direction in ("stricter", "same") and generators_changed(
        old_source, old_info.qualname, new_source, new_info.qualname
    ):
        evidence["generators_changed"] = True
        unclear_reasons.append(
            "it changes which inputs the law tries, and lawkeep cannot tell from samples "
            "whether the new inputs still cover everything the old ones did"
        )
    if direction == "same" and not rename_only(
        old_source,
        old_info.qualname,
        new_source,
        new_info.qualname,
        still_defined=_source_defines(root, cfg),
    ):
        evidence["rename_only"] = False
        unclear_reasons.append(
            "the law's check itself changed (for example a number, a comparison or a "
            "condition), not just names, so lawkeep cannot call it the same law even though "
            "both versions gave the same answers on the inputs it tried"
        )
    static_stricter: list[str] = []
    if static:
        direction = "looser"
    elif (
        bound is not None
        and bound[0] == "stricter"
        and old_only == 0  # no input contradicts it
        and not other_changes  # nothing else in the file changed
    ):
        # The static rule settles it: evidence alone often cannot (the cases between the
        # old and the new limit may never be drawn), and it never contradicted the rule.
        direction = "stricter"
        evidence["static_stricter"] = bound[1]
        unclear_reasons = []
        static_stricter = [bound[1]]
    elif unclear_reasons and direction in ("stricter", "same"):
        direction = "unclear"
    rows_looser.sort(key=_real_first)
    rows_stricter.sort(key=_real_first)
    rows = rows_looser + rows_stricter if direction != "stricter" else rows_stricter + rows_looser
    return DirectionResult(
        direction,
        evidence=evidence,
        examples=rows[:MAX_DIFF_ROWS],
        explanation=_explain(direction, static or static_stricter, unclear_reasons, why),
    )


def _note(mid: str | None, mutants: dict[str, str], real_note: str) -> str:
    if mid is None:
        return real_note
    return f"with a deliberately broken version: {mutants.get(mid, mid)}"


def _first_for(shown: set[tuple[str, str]], side: str, mid: str | None) -> bool:
    if mid is None:
        return True
    if (side, mid) in shown:
        return False
    shown.add((side, mid))
    return True


def _real_first(row: ExampleRow) -> int:
    return 1 if (row.note or "").startswith("with a deliberately") else 0


def _explain(direction: str, static: list[str], unclear: list[str], why: str) -> str:
    label = {
        "stricter": "STRICTER",
        "same": "SAME MEANING",
        "looser": "LOOSER",
        "unclear": "UNCLEAR",
    }[direction]
    parts: list[str] = []
    if static:
        parts.append("; ".join(static))
    parts.extend(unclear)
    if why:
        parts.append(f"evidence: {why}")
    return f"{label}: " + "; ".join(parts) + "."
