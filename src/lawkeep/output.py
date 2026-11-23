"""One way to print results: plain English for people, JSON for agents.

Every command ends with::

    from lawkeep.output import emit
    emit(report, args.json)          # prints report.to_text() or JSON of report.to_dict()
    return report.exit_code

``emit(report, as_json, text=...)`` lets a command supply its own plain-English rendering
without editing ``models.py``. JSON always goes to stdout as a single document, so agents
can parse stdout directly; nothing else is printed to stdout in JSON mode.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, TextIO, cast

from lawkeep import exitcodes
from lawkeep.models import SimpleReport, to_plain


def to_jsonable(obj: Any) -> Any:
    """Convert reports, dataclasses, paths and datetimes into JSON-ready values."""
    return to_plain(obj)


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False)


def render_text(report: Any) -> str:
    """Plain-English text of a report (``to_text()`` if it has one)."""
    if hasattr(report, "to_text"):
        return report.to_text()
    if isinstance(report, str):
        return report
    data = to_jsonable(report)
    if isinstance(data, dict):
        mapping = cast("dict[str, Any]", data)
        return "\n".join(f"{k}: {v}" for k, v in mapping.items())
    return str(data)


def emit(
    report: Any, as_json: bool, *, text: str | None = None, stream: TextIO | None = None
) -> None:
    """Print ``report`` as JSON (``as_json``) or plain text (``text`` overrides the rendering)."""
    out = stream or sys.stdout
    if as_json:
        out.write(dumps(report) + "\n")
    else:
        body = text if text is not None else render_text(report)
        out.write(body.rstrip("\n") + "\n")
    out.flush()


def setup_problem(
    command: str,
    message: str,
    as_json: bool,
    *,
    hint: str | None = None,
    data: dict[str, Any] | None = None,
    stream: TextIO | None = None,
) -> int:
    """Print a setup problem in the standard shape and return result code 3."""
    report = SimpleReport(
        kind=command,
        exit_code=exitcodes.SETUP_PROBLEM,
        summary=f"SETUP PROBLEM: {message}",
        hint=hint,
        data=dict(data or {}),
    )
    text = None if as_json else setup_problem_text(command, message, hint, stream=stream)
    emit(report, as_json, text=text, stream=stream)
    return exitcodes.SETUP_PROBLEM


def setup_problem_text(
    command: str, message: str, hint: str | None, *, stream: TextIO | None = None
) -> str:
    """The human screen for a setup problem: what is wrong, then what to do."""
    from lawkeep import ui

    return ui.render(
        ui.banner(
            "SETUP PROBLEM",
            f"lawkeep could not finish `lawkeep {command}`.",
            tone="problem",
        ),
        ui.status("fail", message),
        ui.next_step(hint or "Fix the problem above and run the command again."),
        stream=stream,
    )


def not_implemented(command: str, as_json: bool, *, stream: TextIO | None = None) -> int:
    """Standard message for a command that is not built yet (result code 3)."""
    return setup_problem(
        command,
        f"`lawkeep {command}` is not implemented yet.",
        as_json,
        hint="This part of lawkeep v0.1 is still being built.",
        data={"not_implemented": True},
        stream=stream,
    )


def _split_top_level(text: str) -> list[str]:
    """Split ``a, f(b, c), [d, e]`` at top-level commas."""
    parts: list[str] = []
    cur: list[str] = []
    depth, quote = 0, ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _match_paren(text: str, i: int) -> int:
    """Index of the bracket closing the one at ``text[i]`` (-1 if none)."""
    depth, quote = 0, ""
    for j in range(i, len(text)):
        ch = text[j]
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def unlambda(value: str) -> str:
    """Hypothesis shows values built with a lambda as code:
    ``(lambda start, steps: Slot(start, start + steps * STEP))(OPENING + 1 * STEP, 1)``.
    Show what was built and from what instead:
    ``Slot(start, start + steps * STEP) with start=OPENING + 1 * STEP, steps=1``.
    Works inside lists and tuples too; anything it cannot read is left as it is."""
    out: list[str] = []
    i = 0
    while True:
        k = value.find("(lambda ", i)
        if k < 0:
            out.append(value[i:])
            return "".join(out)
        close = _match_paren(value, k)
        if close < 0 or close + 1 >= len(value) or value[close + 1] != "(":
            out.append(value[i : k + 1])
            i = k + 1
            continue
        args_close = _match_paren(value, close + 1)
        m = re.fullmatch(r"\(lambda ([\w\s,]*): (.*)\)", value[k : close + 1], re.S)
        if args_close < 0 or not m:
            out.append(value[i : k + 1])
            i = k + 1
            continue
        params = [p.strip() for p in m.group(1).split(",") if p.strip()]
        args = _split_top_level(value[close + 2 : args_close])
        if len(params) != len(args):
            out.append(value[i : k + 1])
            i = k + 1
            continue
        out.append(value[i:k])
        out.append(_substitute(m.group(2).strip(), dict(zip(params, args, strict=True))))
        i = args_close + 1


def _substitute(body: str, values: dict[str, str]) -> str:
    """``Slot(start, start + steps * STEP)`` with start=OPENING, steps=1 ->
    ``Slot(OPENING, OPENING + 1 * STEP)`` (falls back to "... with start=..." text)."""
    import ast

    try:
        tree = ast.parse(body, mode="eval")
        repl = {k: ast.parse(unlambda(v), mode="eval").body for k, v in values.items()}

        class _Sub(ast.NodeTransformer):
            def visit_Name(self, node: ast.Name) -> ast.AST:
                return repl.get(node.id, node)

        return ast.unparse(_Sub().visit(tree))
    except (SyntaxError, ValueError):
        given = ", ".join(f"{k}={v}" for k, v in values.items())
        return f"({body} with {given})"


def plain_example(example: dict[str, Any] | str | None) -> str:
    """``{"charge": "40", "amount": "50"}`` -> ``charge=40, amount=50``."""
    if example is None:
        return ""
    if isinstance(example, str):
        return unlambda(example)
    if set(example) == {"call"}:
        return str(example["call"])
    return ", ".join(f"{k}={unlambda(str(v))}" for k, v in example.items())


def table(rows: list[list[Any]], headers: list[str] | None = None, *, indent: str = "  ") -> str:
    """A simple left-aligned text table (no colours, safe for any terminal and CI log)."""
    all_rows = [[str(c) for c in r] for r in ([headers] if headers else []) + rows]
    if not all_rows:
        return ""
    widths = [
        max(len(r[i]) if i < len(r) else 0 for r in all_rows)
        for i in range(max(map(len, all_rows)))
    ]
    lines: list[str] = []
    for n, r in enumerate(all_rows):
        lines.append(indent + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if headers and n == 0:
            lines.append(indent + "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def yes_no(
    question: str,
    *,
    default: bool | None = None,
    stream_in: TextIO | None = None,
    stream_out: TextIO | None = None,
) -> bool | None:
    """Ask a person yes/no in the terminal. Returns None when there is no terminal to ask.

    Commands must not call this in --json mode or when stdin is not a TTY: return WAITING (2)
    instead so the agent's own permission prompt (or a later `lawkeep approve`) can decide.
    """
    sin = stream_in or sys.stdin
    sout = stream_out or sys.stdout
    if stream_in is None and not sin.isatty():
        return None
    suffix = {True: " [Y/n] ", False: " [y/N] ", None: " [y/n] "}[default]
    while True:
        sout.write(question + suffix)
        sout.flush()
        answer = sin.readline()
        if not answer:
            return default
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer == "" and default is not None:
            return default
        sout.write("Please answer yes or no.\n")
