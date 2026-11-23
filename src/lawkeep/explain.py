"""Plain-English explanations of laws, shared by ``draft``, ``approve``, ``propose`` and ``check``.

Every sentence here follows ``docs/OUTPUT-STYLE.md``: lead with the answer, give every number
its meaning, keep ids and paths in the background. The functions return plain strings (for
reports and Markdown) or Rich renderables built with :mod:`lawkeep.ui`.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rich.console import RenderableType
from rich.text import Text

from lawkeep import ui
from lawkeep.models import ExampleRow, LawInfo

_NOT_HOLDING = "This law does not hold on today's code"


# ---------------------------------------------------------------------------------------------
# Small wording helpers

_DECIMAL = re.compile(r"Decimal\('(-?[0-9.]+(?:[eE][+-]?[0-9]+)?)'\)")


def tidy(text: str) -> str:
    """Values as people write them: ``Decimal('0.25')`` -> ``0.25``."""
    return _DECIMAL.sub(r"\1", text)


_COMPARE = re.compile(r"^(?P<got>.+?) (?P<op>==|!=|<=|>=|<|>) (?P<want>.+)$")
_EXPECT = {
    "==": "{want}",
    "!=": "something other than {want}",
    "<=": "at most {want}",
    ">=": "at least {want}",
    "<": "less than {want}",
    ">": "more than {want}",
}


def comparison_in_words(expr: str) -> str:
    """``0.26 == 0.25`` -> ``The law expected 0.25, but the code gave 0.26.`` (the left side
    is what the code gave, the right side what the law expects). Anything else, such as a
    chained comparison, is shown as it is."""
    m = _COMPARE.match(expr.strip())
    if m:
        got, op, want = m.group("got"), m.group("op"), m.group("want")
        if not any(o in f" {got} {want} " for o in (" == ", " != ", " <= ", " >= ", " < ", " > ")):
            expected = _EXPECT[op].format(want=want)
            return f"The law expected {expected}, but the code gave {got}."
    return f"This check was false: {expr}"


def failure_lines(message: str | None, *, details: bool = False) -> list[str]:
    """Why a law failed, in plain words: the first line, plus pytest's explanation with
    ``details``. ``AssertionError: assert a <= b`` -> ``This check was false: a <= b``."""
    lines = [ln.rstrip() for ln in (message or "").strip().splitlines() if ln.strip()]
    if not lines:
        return []
    first = lines[0]
    for prefix in ("AssertionError: ", "E   AssertionError: "):
        if first.startswith(prefix):
            first = first[len(prefix) :]
    if first.startswith("assert "):
        first = comparison_in_words(tidy(first[len("assert ") :]))
    out = [tidy(first)]
    if details:
        out += [tidy(ln) for ln in lines[1:8]]
    return out


def inputs_text(inputs: dict[str, Any] | None, *, sep: str = ", ") -> str:
    """``{"charge": "40", "amount": "50"}`` -> ``charge=40, amount=50``."""
    from lawkeep.output import plain_example

    if not inputs:
        return "(no inputs)"
    if sep == ", ":
        return tidy(plain_example(inputs))
    return sep.join(tidy(plain_example({k: v})) for k, v in inputs.items())


def breaking_input_text(inputs: dict[str, Any] | None, *, raw: bool = False) -> str:
    """One line per input, in words: ``charge: a charge of $0.25 with no discount code``.
    ``raw=True`` adds the exact value lawkeep tried (for ``--details``)."""
    from lawkeep import readable

    if not inputs:
        return "(no inputs)"
    out: list[str] = []
    for name, value in readable.lines(inputs):
        out.append(value if name == "call" else f"{name}: {value}")
    if raw:
        out.append("exact value: " + inputs_text(inputs))
    return "\n".join(out)


def result_text(result: str | None) -> str:
    """``combine() → 1`` -> ``combine() gave 1`` (what the real code did)."""
    if result is None or result == "":
        return "-"
    return tidy(str(result).replace(" → ", " gave ").replace("; ", ";\n"))


def answer(holds: bool | None) -> str:
    if holds is True:
        return "Yes"
    if holds is False:
        return "NO"
    return "-"


def _answer_cell(holds: bool | None) -> Text:
    styles: dict[bool | None, str] = {True: "green", False: "bold red"}
    style = styles.get(holds, "")
    return Text(answer(holds), style=style)


def direction_text(direction: str | None) -> str:
    """What a direction means for the person."""
    return {
        "stricter": "Stricter — the law would demand more of your code.",
        "same": "Same meaning — only names changed; the law checks the same thing.",
        "looser": "Looser — the law would allow more, so it protects less.",
        "unclear": (
            "Unclear — lawkeep cannot tell whether this makes the law stricter or looser, "
            "so a person decides."
        ),
    }.get(direction or "unclear", "Unclear — a person decides.")


def plain_explanation(text: str | None) -> str:
    """Direction explanations start with a label ("LOOSER: ..."); the person already sees the
    direction in words, so keep the plain part, starting with a capital letter."""
    t = (text or "").strip()
    for label in ("STRICTER: ", "SAME MEANING: ", "LOOSER: ", "UNCLEAR: "):
        if t.startswith(label):
            t = t[len(label) :]
    t = t.replace("; evidence: ", ". What lawkeep saw: ").replace(
        "evidence: ", "What lawkeep saw: "
    )
    t = t.replace("case(s)", "input(s)")
    return tidy(t[:1].upper() + t[1:]) if t else "-"


def direction_word(direction: str | None) -> str:
    return {
        "stricter": "stricter",
        "same": "same meaning",
        "looser": "looser",
        "unclear": "unclear",
    }.get(direction or "", direction or "")


# ---------------------------------------------------------------------------------------------
# "Does it work with my code?" (a probe of the law on many inputs)


def probe_status(probe: Any) -> tuple[str, str]:
    """(tone, sentence) for a :class:`lawkeep.examples_table.LawProbe`."""
    status = getattr(probe, "status", "not_run")
    n = getattr(probe, "examples_tried", 0) or 0
    message = getattr(probe, "message", None)
    if status == "holds":
        tried = ui.plural(n, "different input")
        return "ok", (
            f"Works with your current code — lawkeep tried {tried} and the law was never broken."
        )
    if status == "fails":
        return "fail", (
            "Breaks with your current code — lawkeep found an input where the code does not "
            "follow this law. Either the code has a bug or the law is wrong: you decide."
        )
    if status == "error":
        return "fail", f"Does not run — {message or 'lawkeep could not start this law'}."
    return "warn", (
        f"Not tried — lawkeep could not run this law on its own"
        f"{f' ({message})' if message else ''}."
    )


def probe_one_line(probe: Any) -> str:
    """Short evidence text for the law history (``details.evidence``)."""
    return probe_status(probe)[1]


# ---------------------------------------------------------------------------------------------
# "How strong is it?" (the quick strength check)


@dataclass
class Strength:
    tone: str  # ok | warn | info
    text: str
    weak: bool = False
    measured: bool = True
    reason: str | None = None  # why it is weak, in plain words


def strength_status(result: Any, *, missing: str | None = None) -> Strength:
    """Plain words for a :class:`lawkeep.models.StrengthResult` from the quick check."""
    if result is None:
        why = missing or "the strength check did not run"
        return Strength("info", f"Strength not measured — {why}.", measured=False)
    outcomes = list(getattr(result, "quick_check", []) or [])
    flags = [f for f in (getattr(result, "flags", []) or []) if f]
    if any(f.startswith(_NOT_HOLDING) for f in flags):
        return Strength(
            "info",
            "Strength not measured — the law breaks with your current code, so lawkeep "
            "could not test how well it catches mistakes.",
            measured=False,
        )
    total = len(outcomes)
    caught = sum(1 for o in outcomes if getattr(o, "caught", False))
    pt = getattr(result, "planted_bugs_total", None) or 0
    pc = getattr(result, "planted_bugs_caught", None) or 0
    planted = (
        f" It also caught {pc} of {pt} small bugs lawkeep planted in a copy of your code."
        if pt
        else ""
    )
    if flags or (total and caught == 0):
        reason = (
            flags[0]
            if flags
            else (
                f"lawkeep made {ui.plural(total, 'deliberately broken version')} of the code and "
                f"this law caught none of them."
            )
        )
        reason = plain_flag(reason)
        if total and flags and caught < total:
            only = f"only {caught}" if caught else "none of them"
            text = (
                f"Weak law — lawkeep made {total} deliberately broken versions of the code and "
                f"this law caught {only}. {reason[:1].upper()}{reason[1:]}"
            )
        else:
            text = f"Weak law — {reason}"
        return Strength("warn", text, weak=True, reason=reason)
    if not total:
        if pt:
            tone = "ok" if pc == pt else "info"
            return Strength(
                tone,
                f"{'Strong' if pc == pt else 'Fairly strong'} law — it caught {pc} of {pt} small "
                f"bugs lawkeep planted in a copy of your code.",
            )
        return Strength(
            "info",
            "Strength not measured — lawkeep found no function of your code that this law "
            "calls by name, so it could not make broken versions.",
            measured=False,
        )
    if caught == total:
        every = "all" if total > 2 else ("both" if total == 2 else "it")
        made = ui.plural(total, "deliberately broken version")
        return Strength(
            "ok",
            f"Strong law — lawkeep made {made} of the code and this law caught {every}"
            f"{f' {total}' if total > 2 else ''}.{planted}",
        )
    return Strength(
        "ok",
        f"Fairly strong law — lawkeep made {total} deliberately broken versions of the code "
        f"and this law caught {caught} of them.{planted}",
    )


def plain_flag(flag: str) -> str:
    """Strength flags start with a technical label ("Vacuous: ..."); keep the plain part,
    without the list of every other broken version that also got through."""
    flag = shorten_flag(tidy(re.sub(r" \(also when .*?\)(?=\. It checks)", "", flag)))
    for label, plain in (
        ("Vacuous: ", ""),
        ("One-sided: ", "it is one-sided: "),
        ("Weak test data: ", "weak test data: "),
    ):
        if flag.startswith(label):
            rest = flag[len(label) :]
            return (plain + rest[:1].lower() + rest[1:]) if plain else rest
    return flag[:1].lower() + flag[1:] if flag[:2] != flag[:2].upper() else flag


def strength_one_line(result: Any) -> str | None:
    if result is None:
        return None
    return strength_status(result).text


# ---------------------------------------------------------------------------------------------
# Example tables and inputs


def examples_table(rows: Sequence[ExampleRow], *, proposal: bool = False) -> RenderableType:
    """Input / What the code did / Follows the law? (proposals: old law / new law)."""
    if not rows:
        return ui.dim("(lawkeep could not show examples for this law)")
    from lawkeep import readable

    notes = [_short_note(r.note) for r in rows]
    with_notes = any(notes)
    body: list[list[Any]] = []
    for r, note in zip(rows, notes, strict=True):
        cells: list[Any] = [readable.inline(r.inputs), result_text(r.result)]
        if proposal:
            cells += [_answer_cell(r.old_holds), _answer_cell(r.holds)]
        else:
            cells.append(_answer_cell(r.holds))
        if with_notes:
            cells.append(Text(note or "", style="dim"))
        body.append(cells)
    note_col: tuple[list[str], list[int], list[int]] = (
        (["Note"], [6], [12]) if with_notes else ([], [], [])
    )
    if proposal:
        return ui.table(
            ["Input", "What the code did", "Old law OK?", "New law OK?", *note_col[0]],
            body,
            ratios=[1, 1, None, None, None],
            min_widths=[12, 12, 4, 4, *note_col[1]],
            max_widths=[None, None, 8, 8, *note_col[2]],
        )
    return ui.table(
        ["Input", "What the code did", "Follows the law?", *note_col[0]],
        body,
        ratios=[1, 1, None, None],
        min_widths=[12, 12, 7, *note_col[1]],
        max_widths=[None, None, 9, *note_col[2]],
    )


def _short_note(note: str | None) -> str | None:
    """Short words for the Note column; notes that tell a person nothing are dropped."""
    if not note:
        return None
    return {
        "example written in the law": "written in the law",
        "saved failing example": "failed before",
        "edge case: zero": "zero",
        "edge case: empty": "empty",
        "edge case: nothing (None)": None,  # "no code" in the Input column says it already
    }.get(note, note)


def input_names(rows: Iterable[ExampleRow]) -> list[str]:
    out: list[str] = []
    for r in rows:
        for k in r.inputs or {}:
            if k not in out and k != "call":
                out.append(k)
    return out


def inputs_block(
    descriptions: dict[str, str] | None, rows: Sequence[ExampleRow]
) -> RenderableType | None:
    """``What the inputs mean`` list, for every input shown in the table."""
    names = input_names(rows)
    for k in descriptions or {}:
        if k not in names:
            names.append(k)
    if not names:
        return None
    desc = descriptions or {}
    table = ui.label_value(
        [(f"{n}:", desc.get(n) or "a sample value lawkeep made up for testing") for n in names]
    )
    tip = None
    if any(not desc.get(n) or is_made_up(desc[n]) for n in names):
        tip = ui.dim(INPUTS_TIP)
    return ui.group(ui.bold("What the inputs mean"), ui.indent(table), tip)


#: Shown when a law does not describe its inputs itself.
INPUTS_TIP = (
    'Tip: the law can describe its inputs in its own words, e.g. @law("...", '
    'inputs={"charge": "a payment the customer made"}).'
)


def is_made_up(description: str) -> bool:
    """True for the fallback description lawkeep writes when the law gives none."""
    return description.endswith("that lawkeep made up for testing")


# ---------------------------------------------------------------------------------------------
# Similar laws


def similar_law(sentence: str | None, approved: Iterable[LawInfo]) -> LawInfo | None:
    """An approved law whose sentence is almost the same (a likely duplicate), if any."""
    if not sentence:
        return None
    best, score = None, 0.0
    a = sentence.lower().strip(" .")
    for info in approved:
        if not info.sentence or info.is_draft:
            continue
        b = info.sentence.lower().strip(" .")
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        if ratio > score:
            best, score = info, ratio
    return best if score >= 0.85 else None


# ---------------------------------------------------------------------------------------------
# The draft-law panel (draft and approve)


def recommendation(probe: Any, strength: Strength, similar: LawInfo | None) -> tuple[str, str]:
    """(tone, sentence): what lawkeep suggests the person does with a draft law."""
    status = getattr(probe, "status", "not_run")
    dup = (
        f" An approved law already says almost the same thing: “{similar.sentence}”."
        if similar is not None
        else ""
    )
    if status == "error":
        return "fail", (
            "Recommended: reject. This law does not run; ask your coding agent to fix it and "
            "draft it again."
        )
    if status == "fails":
        return "warn", (
            "Decide carefully: the law and your code disagree. Approve only if the law is "
            "right and you will fix the code; reject it if the law is wrong." + dup
        )
    if strength.weak:
        return "warn", (
            "Think twice: this law is weak, so it would let real mistakes through. Approving "
            "it adds little protection; consider rejecting it and asking your coding agent for "
            "a stricter law." + dup
        )
    if similar is not None:
        return "warn", "Think twice: this may be a duplicate." + dup
    if status == "not_run":
        return "warn", (
            "Look closely: lawkeep could not try this law, so check the sentence and examples "
            "yourself before approving."
        )
    if not strength.measured:
        return "ok", (
            "Probably fine: it works with your code, but lawkeep could not measure how strong "
            "it is. Read the sentence and examples, then decide."
        )
    return "ok", "Recommended: approve. It works with your code and catches mistakes."


def draft_law_panel(
    info: LawInfo,
    *,
    probe: Any,
    strength: Strength,
    descriptions: dict[str, str] | None = None,
    similar: LawInfo | None = None,
    title: str = "Draft law",
    file: str | None = None,
) -> RenderableType:
    """One box per draft law: sentence, checks, examples, inputs, recommendation."""
    p_tone, p_text = probe_status(probe)
    parts: list[RenderableType | None] = [ui.quote(info.sentence), ui.blank()]
    parts.append(ui.status(p_tone, p_text))
    failing = list(getattr(probe, "failing", []) or [])
    if getattr(probe, "status", "") == "fails" and failing:
        ex = failing[0]
        parts.append(
            ui.indent(
                ui.label_value(
                    [
                        ("The input that breaks it:", breaking_input_text(ex.inputs)),
                        ("The code did:", result_text(ex.result) if ex.result else None),
                    ]
                ),
                2,
            )
        )
    if getattr(probe, "status", "") in ("holds", "fails"):
        parts.append(ui.status(strength.tone, strength.text))
    rows = list(getattr(probe, "rows", []) or [])
    if rows:
        parts += [ui.blank(), ui.bold("Examples lawkeep tried"), examples_table(rows)]
    block = inputs_block(descriptions, rows)
    if block is not None:
        parts += [ui.blank(), block]
    r_tone, r_text = recommendation(probe, strength, similar)
    parts += [ui.blank(), ui.status(r_tone, Text(r_text, style="bold"))]
    parts.append(ui.ident(f"law id {info.law_id}" + (f" · file {file}" if file else "")))
    tone = "warn" if r_tone != "ok" else "ok"
    return ui.panel(*parts, title=title, tone=tone)


_SUGGESTION = re.compile(
    r"It checks a limit, not the answer\. Suggestion: add a law that pins down what (?P<fn>\S+) "
    r"returns, e\.g\. an exact result for a known input, or a relation between input and output\."
)


def shorten_flag(text: str) -> str:
    """The long strength suggestions, said shortly (the JSON keeps the full text)."""
    text = _SUGGESTION.sub(
        lambda m: (
            f"It checks a limit, not the exact answer; a companion law that checks what "
            f"{m.group('fn')} returns would fix that."
        ),
        text,
    )
    return text.replace(
        "it may catch the error itself (try/except, pytest.raises) or skip the check when the "
        "call fails.",
        "the law probably catches the error itself, or skips its check when the call fails.",
    )
