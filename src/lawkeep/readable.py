"""Input values in plain words, for people (docs/OUTPUT-STYLE.md).

lawkeep shows inputs as Python text (``charge(price=Decimal('0.25'), discount_code=None)``).
This module turns that text into words where it can do so safely:

    describe("charge", "charge(price=Decimal('0.25'), discount_code=None)")
    -> "a charge of $0.25 with no discount code"

Generic rules, nothing project-specific: the wording comes from the names of the input and
its fields. Amounts are shown as money (``$0.25``) only when the name suggests money (price,
amount, total, fee, ...). ``None`` becomes "none" / "no <field>"; objects built with keyword
arguments become "a <name> with <field> <value>, ..."; tuples and lists of objects are broken
into numbered parts. Anything else returns ``None`` so the caller shows the raw text. Nothing
is ever executed: the text is only parsed with :mod:`ast`.
"""

from __future__ import annotations

import ast
import re

MONEY_WORDS = {
    "price",
    "amount",
    "total",
    "fee",
    "fees",
    "cost",
    "costs",
    "payment",
    "paid",
    "refund",
    "balance",
    "money",
    "tax",
    "subtotal",
    "salary",
    "wage",
    "charge",
}
_MAX = 160


def words(name: str) -> str:
    """``discount_code`` -> ``discount code``; ``CardFee`` -> ``card fee``."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name.strip("_"))
    return " ".join(name.replace("_", " ").split()).lower()


def is_money(name: str) -> bool:
    return any(w in MONEY_WORDS for w in words(name).split())


def _article(phrase: str) -> str:
    return "an" if phrase[:1] in "aeiou" else "a"


def _number(text: str, money: bool) -> str:
    if not money:
        return text
    neg = text.startswith("-")
    digits = text[1:] if neg else text
    return f"{'-' if neg else ''}${digits}"


class _Unsure(Exception):
    pass


def _value(name: str, node: ast.AST, src: str) -> str:
    money = is_money(name)
    if isinstance(node, ast.Constant):
        v = node.value
        if v is None:
            return "none"
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, (int, float)):
            seg = ast.get_source_segment(src, node) or repr(v)
            return _number(seg, money)
        if isinstance(v, str):
            return v if v.strip() else "empty text"
        raise _Unsure
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _value(name, node.operand, src)
        return f"-{inner}" if not inner.startswith("$") else f"-{inner}"
    if isinstance(node, ast.Call):
        func = node.func
        fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if not fname:
            raise _Unsure
        if fname in ("Decimal", "Fraction") and len(node.args) == 1 and not node.keywords:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, (str, int)):
                return _number(str(arg.value), money)
            raise _Unsure
        if fname in ("datetime", "date") and node.args and not node.keywords:
            parts: list[int] = [
                a.value
                for a in node.args
                if isinstance(a, ast.Constant) and isinstance(a.value, int)
            ]
            if len(parts) != len(node.args):
                raise _Unsure
            y, mo, d, *rest = [*parts, 0, 0][: max(len(parts), 3)]
            text = f"{y:04d}-{mo:02d}-{d:02d}"
            if fname == "datetime" and len(parts) > 3:
                h, mi = (parts[3], parts[4] if len(parts) > 4 else 0)
                text += f" {h:02d}:{mi:02d}"
            return text
        if node.args:
            raise _Unsure  # positional fields: we do not know their names
        label = words(fname)
        if not node.keywords:
            return f"{_article(label)} {label}"
        fields: list[tuple[str, str]] = []
        for kw in node.keywords:
            if kw.arg is None:
                raise _Unsure
            v = _value(kw.arg, kw.value, src)
            fields.append((kw.arg, v))
        first_name, first_value = fields[0]
        head = f"{_article(label)} {label}"
        rest = fields
        if is_money(first_name) and first_value.startswith(("$", "-$")):
            head += f" of {first_value}"
            rest = fields[1:]
        phrases = [f"no {words(k)}" if v == "none" else f"{words(k)} {v}" for k, v in rest]
        return head + (" with " + ", ".join(phrases) if phrases else "")
    if isinstance(node, (ast.Tuple, ast.List)):
        items = [_value(name, e, src) for e in node.elts]
        if not items:
            return "nothing (empty)" if isinstance(node, ast.List) else "nothing"
        if all(" " not in i for i in items):
            return "[" + ", ".join(items) + "]" if isinstance(node, ast.List) else ", ".join(items)
        word = "item" if isinstance(node, ast.List) else "part"
        return "; ".join(f"{word} {n}: {i}" for n, i in enumerate(items, 1))
    if isinstance(node, ast.Name) and node.id in ("True", "False", "None"):
        return {"True": "yes", "False": "no", "None": "none"}[node.id]
    raise _Unsure


def describe(name: str, raw: str) -> str | None:
    """Plain words for the value ``raw`` (Python text) of input ``name``, or None if unsure."""
    text = str(raw).strip()
    if not text or len(text) > 2000:
        return None
    try:
        node = ast.parse(text, mode="eval").body
        out = _value(name, node, text)
    except (SyntaxError, ValueError, _Unsure, RecursionError):
        return None
    return out if len(out) <= _MAX else None


def _simple(value: str) -> bool:
    return " " not in value or value.startswith("[")


def inline(inputs: dict[str, str]) -> str:
    """All inputs on one line: ``price $16.47, code SAVE25`` / ``price $8.00, no code``."""
    from lawkeep.explain import tidy

    out: list[str] = []
    for k, raw in inputs.items():
        if k == "call":
            out.append(tidy(str(raw)))
            continue
        v = describe(k, str(raw))
        if v is None:
            out.append(f"{k}={tidy(str(raw))}")
        elif v == "none":
            out.append(f"no {words(k)}")
        elif _simple(v):
            out.append(f"{words(k)} {v}")
        else:
            out.append(f"{words(k)}: {v}")
    return ", ".join(out) if out else "(no inputs)"


def lines(inputs: dict[str, str]) -> list[tuple[str, str]]:
    """One ``(name, words)`` pair per input, for "The input that breaks it" blocks."""
    from lawkeep.explain import tidy

    out: list[tuple[str, str]] = []
    for k, raw in inputs.items():
        if k == "call":
            out.append(("call", tidy(str(raw))))
            continue
        v = describe(k, str(raw))
        out.append((k, v if v is not None else tidy(str(raw))))
    return out
