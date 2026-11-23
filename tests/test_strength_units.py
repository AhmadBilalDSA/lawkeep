"""Unit tests for the strength building blocks that run no project code: the planted-bug
generator (ast mutator), the source map and the renderers. Made-up code only."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from lawkeep import config
from lawkeep.models import ProtectionReport, ProtectionResult, StrengthReport, StrengthResult
from lawkeep.strength import mutants, render, sourcemap

SOURCE = textwrap.dedent(
    '''
    """A made-up module."""
    from decimal import Decimal


    def fee(amount: Decimal) -> Decimal:
        """Two percent, nothing under 1."""
        if amount < 1:
            return Decimal("0")
        return amount * 2 / 100


    def greet(name):
        return f"hello {name}" + "!"


    class Cart:
        def __init__(self, items):
            self.items = list(items)

        def total(self) -> int:
            total = 0
            for x in self.items:
                total += x
            return total

        @staticmethod
        def is_empty(items) -> bool:
            return not items and True


    def _private():
        return 1
    '''
)


def test_list_mutations_is_deterministic_and_plain():
    a = mutants.list_mutations(SOURCE, "fee")
    b = mutants.list_mutations(SOURCE, "fee")
    assert [m.description for m in a] == [m.description for m in b]
    descs = [m.description for m in a]
    assert any("changed `<` to `<=`" in d for d in descs)
    assert any("changed `*` to `/`" in d for d in descs)
    assert any("changed `/` to `*`" in d for d in descs)
    assert any("returned None instead of" in d for d in descs)
    assert any("changed `1` to `2`" in d for d in descs)
    # the docstring and the string inside Decimal("0") are never mutated
    assert not any("Two percent" in d for d in descs)
    assert all(m.line >= 6 for m in a)  # inside the function body


def test_strings_and_fstrings_are_left_alone():
    descs = [m.description for m in mutants.list_mutations(SOURCE, "greet")]
    assert not any("`+` to `-`" in d for d in descs)  # string concatenation
    assert any("returned None" in d for d in descs)


def test_methods_augassign_and_statement_deletion():
    descs = [m.description for m in mutants.list_mutations(SOURCE, "Cart.total")]
    assert any("changed `+` to `-` in `total += x`" in d for d in descs)
    assert any(d.startswith("deleted `total += x`") for d in descs)
    descs = [m.description for m in mutants.list_mutations(SOURCE, "Cart.is_empty")]
    assert any("changed `and` to `or`" in d for d in descs)
    assert any("removed the `not`" in d for d in descs)
    assert any("changed `True` to `False`" in d for d in descs)


def test_mutated_module_compiles_and_changes_behaviour():
    muts = mutants.list_mutations(SOURCE, "fee")
    idx = next(m.index for m in muts if "changed `<` to `<=`" in m.description)
    module = mutants.mutated_module(SOURCE, "fee", idx)
    ns: dict = {}
    from decimal import Decimal

    exec(compile(module, "<made-up>", "exec"), {"Decimal": Decimal}, ns)
    assert ns["fee"](Decimal(1)) == Decimal(0)  # the original gives 0.02
    # line numbers point at the real file
    assert module.body[0].lineno == 6


def test_unknown_function_or_index():
    assert mutants.list_mutations(SOURCE, "nope") == []
    assert mutants.list_mutations("def broken(:\n", "broken") == []
    try:
        mutants.mutated_module(SOURCE, "fee", 999)
    except LookupError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected LookupError")


def test_spread_picks_evenly():
    assert mutants.spread(list(range(10)), 3) == [0, 4, 9]
    assert mutants.spread([1, 2], 5) == [1, 2]
    assert mutants.spread([1, 2, 3], None) == [1, 2, 3]
    assert mutants.spread([1, 2, 3], 1) == [1]


def test_functions_in_source_and_lookup():
    funcs = sourcemap.functions_in_source(SOURCE, "src/shop/fees.py")
    names = [f.qualname for f in funcs]
    assert names == ["fee", "greet", "Cart.__init__", "Cart.total", "Cart.is_empty", "_private"]
    fee = funcs[0]
    assert fee.body_start == 8  # after the docstring
    at6 = sourcemap.function_at(funcs, 6)
    assert at6 is not None and at6.qualname == "fee"
    assert sourcemap.function_running(funcs, 6) is None  # the def line runs at import
    running9 = sourcemap.function_running(funcs, 9)
    assert running9 is not None and running9.qualname == "fee"
    is_empty = next(f for f in funcs if f.qualname == "Cart.is_empty")
    assert is_empty.first_line < is_empty.lineno  # decorator line counts
    assert not next(f for f in funcs if f.qualname == "_private").is_public
    assert sourcemap.short("shop.cart:Cart.total") == "Cart.total()"


def test_source_files_and_module_names(tmp_path: Path):
    root = tmp_path
    for rel in (
        "src/shop/__init__.py",
        "src/shop/fees.py",
        "src/shop/test_fees.py",
        "src/shop/conftest.py",
        "laws/fees.py",
        "tests/test_x.py",
    ):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def f():\n    return 1\n")
    cfg = config.default_config()  # "src" exists -> source = src
    files = [p.relative_to(root).as_posix() for p in sourcemap.source_files(root, cfg)]
    assert files == ["src/shop/__init__.py", "src/shop/fees.py"]
    assert sourcemap.module_name(root, cfg, "src/shop/fees.py") == "shop.fees"
    assert sourcemap.module_name(root, cfg, "src/shop/__init__.py") == "shop"
    assert sourcemap.is_source_file(root, cfg, root / "src/shop/fees.py")
    assert not sourcemap.is_source_file(root, cfg, root / "laws/fees.py")
    idx = sourcemap.build_index(root, cfg)
    assert "shop.fees:f" in idx.by_name


def test_strength_text_and_protection_markdown():
    rep = StrengthReport(
        mode="quick",
        laws=[
            StrengthResult(
                law_id="refunds.capped",
                sentence="A refund never exceeds the charge",
                functions_touched=["shop.refunds:refund"],
                planted_bugs_total=3,
                planted_bugs_caught=1,
                flags=["One-sided: this law still holds when refund() always returns 0."],
            )
        ],
        uncovered_functions=["shop.refunds:apply_fee"],
        summary="SOME LAWS ARE WEAK: 1 of 1 laws flagged.",
        exit_code=1,
    )
    text = render.strength_text(rep, details=True)
    assert "SOME LAWS ARE WEAK" in text and "WEAK" in text and "refunds.capped" in text
    assert "caught 1 of 3 planted bugs" in text
    assert "shop.refunds:apply_fee" in text and "Functions no law checks (1)" in text

    prot = ProtectionReport(
        base_ref="main",
        functions=[
            ProtectionResult(
                "shop.refunds:refund", "src/shop/refunds.py", "protected", [8], 9, 9, ["r"]
            ),
            ProtectionResult(
                "shop.refunds:apply_fee",
                "src/shop/refunds.py",
                "not_protected",
                [15],
                6,
                0,
                [],
                note="no law runs this function - read this one",
            ),
        ],
        summary="2 changed function(s)",
    )
    md = render.protection_markdown(prot)
    assert (
        "| `shop.refunds:apply_fee` | **Not protected — read this one** | your laws caught none "
        "of 6 planted bugs. no law runs this function | none |" in md
    )
    assert md.index("apply_fee") < md.index("shop.refunds:refund`")  # unprotected first
    text = render.protection_text(prot)
    assert "Protected" in text and "refund()" in text and "caught all 9" in text
    assert "Not protected" in text and "none of 6" in text


def test_mutations_never_touch_decorators_or_defaults():
    src = "@deco(1)\ndef f(x=3):\n    return x\n"
    descs = [m.description for m in mutants.list_mutations(src, "f")]
    assert descs == ["returned None instead of `x`"]
    ast.parse(src)
