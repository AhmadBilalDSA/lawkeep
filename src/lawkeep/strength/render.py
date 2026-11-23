"""Human text and Markdown for the strength and protection reports (docs/OUTPUT-STYLE.md).

``strength_text`` and ``protection_text`` return terminal text (Rich: colour on a terminal,
plain otherwise); ``protection_markdown`` is for the pull-request comment (no Rich).
"""

from __future__ import annotations

from typing import Any

from rich.console import RenderableType
from rich.text import Text

from lawkeep import ui
from lawkeep.models import ProtectionReport, ProtectionResult, StrengthReport, StrengthResult
from lawkeep.strength.sourcemap import short

#: How each protection status is shown (the same words in text and Markdown).
STATUS_WORDS = {
    "protected": "Protected",
    "partly": "Partly protected",
    "not_protected": "Not protected — read this one",
}
_STATUS_TONE = {"protected": "green", "partly": "yellow", "not_protected": "bold red"}


# ---------------------------------------------------------------------------------------------
# strength


def _strength_word(r: StrengthResult) -> tuple[str, str]:
    from lawkeep.explain import strength_status

    s = strength_status(r)
    if s.weak:
        return "WEAK", "bold yellow"
    if not s.measured:
        return "Not measured", ""
    if s.text.startswith("Strong"):
        return "Strong", "green"
    return "Fairly strong", "green"


def _found(r: StrengthResult) -> str:
    bits: list[str] = []
    if r.quick_check:
        caught = sum(1 for q in r.quick_check if q.caught)
        bits.append(f"caught {caught} of {len(r.quick_check)} deliberately broken versions")
    if r.planted_bugs_total:
        bits.append(f"caught {r.planted_bugs_caught} of {r.planted_bugs_total} planted bugs")
    if not bits:
        return "nothing could be measured"
    if any(w.startswith("On its own this law checks a limit") for w in r.weak_data):
        bits.append("other laws catch what it lets through")
    return "; ".join(bits)


def strength_text(report: StrengthReport, *, details: bool = False) -> str:
    """The `lawkeep strength` screen."""
    from lawkeep.explain import plain_flag, strength_status

    flagged = [r for r in report.laws if r.flags]
    measured = len(report.laws)
    how = (
        "lawkeep made deliberately broken versions of the code each law calls (for example "
        "one that always returns 0) and planted small bugs in a copy of it, then checked "
        "whether the law noticed."
    )
    if not report.laws:
        banner = ui.banner(
            "NO LAWS MEASURED",
            report.summary.split(": ", 1)[-1]
            if report.summary
            else "There was nothing to measure.",
            tone="info",
        )
    elif flagged:
        banner = ui.banner(
            "SOME LAWS ARE WEAK",
            f"{len(flagged)} of {ui.plural(measured, 'law')} would let real mistakes through. "
            + how,
            tone="warn",
        )
    else:
        banner = ui.banner(
            "NO WEAK LAWS FOUND",
            f"All {ui.plural(measured, 'law')} caught the mistakes lawkeep tried. " + how,
            tone="ok",
        )
    out: list[RenderableType] = [banner]
    if report.laws:
        rows: list[list[Any]] = []
        for r in sorted(report.laws, key=lambda r: (not r.flags, r.law_id)):
            word, style = _strength_word(r)
            name = Text(f"“{r.sentence}”" if r.sentence else r.law_id)
            if details:
                name = ui.group(name, ui.ident(r.law_id))
            rows.append([name, Text(word, style=style), _found(r)])
        out += [
            ui.blank(),
            ui.table(["Law", "Strength", "What lawkeep found"], rows, ratios=[5, 2, 4]),
        ]
    for r in report.laws:
        if not r.flags:
            continue
        s = strength_status(r)
        parts: list[RenderableType] = [ui.quote(r.sentence or r.law_id), ui.warning(s.text)]
        for f in r.flags[1:]:
            parts.append(ui.warning(plain_flag(f)[:1].upper() + plain_flag(f)[1:]))
        if details:
            for w in r.weak_data:
                parts.append(ui.status("info", w))
        elif r.weak_data:
            parts.append(
                ui.status(
                    "info",
                    f"{ui.plural(len(r.weak_data), 'note')} about inputs this law never tried "
                    "(run `lawkeep strength --details` to see them).",
                )
            )
        shown = r.missed_bugs[: (10 if details else 3)]
        if shown:
            parts.append(ui.bold("Planted bugs this law missed (the code was wrong, the law held)"))
            for m in shown:
                parts.append(
                    ui.status(
                        "info",
                        f"{short(m.get('function', ''))} line {m.get('line')}: "
                        f"{m.get('description')}",
                    )
                )
            if len(r.missed_bugs) > len(shown):
                parts.append(
                    ui.dim(
                        f"... and {len(r.missed_bugs) - len(shown)} more "
                        "(run `lawkeep strength --details` to see more)"
                    )
                )
        if r.functions_touched:
            fns = ", ".join(short(f) for f in r.functions_touched[:4])
            more = (
                f" and {len(r.functions_touched) - 4} more" if len(r.functions_touched) > 4 else ""
            )
            parts.append(ui.dim(f"This law runs {fns}{more}."))
            parts.append(ui.ident(f"law id {r.law_id}"))
        out += [ui.blank(), ui.panel(*parts, title="Weak law", tone="warn")]
    if details:
        for r in report.laws:
            if r.flags or not (r.weak_data or r.missed_bugs):
                continue
            out.append(ui.status("info", f"“{r.sentence or r.law_id}”: " + "; ".join(r.weak_data)))
    if report.uncovered_functions:
        n = len(report.uncovered_functions)
        out += [
            ui.blank(),
            ui.heading(f"Functions no law checks ({n})"),
            ui.dim("A bug in these would not be caught by any law."),
        ]
        out += [ui.status("info", name) for name in report.uncovered_functions[:20]]
        if n > 20:
            out.append(ui.dim(f"... and {n - 20} more"))
    if report.notes:
        out.append(ui.blank())
        out += [ui.status("info", n) for n in report.notes]
    out.append(ui.blank())
    if flagged:
        out.append(
            ui.next_step(
                "make the weak laws stronger: ask your coding agent to propose a stricter "
                "version (`lawkeep propose`) or to draft a companion law that checks the exact "
                "answer (`lawkeep draft`)."
            )
        )
    elif not report.laws:
        out.append(ui.next_step("add laws first: ask your coding agent to run `lawkeep draft`."))
    elif report.uncovered_functions:
        out.append(ui.next_step("consider laws for the functions no law checks (`lawkeep draft`)."))
    else:
        out.append(ui.next_step("nothing to do. Your laws caught every mistake lawkeep tried."))
    return ui.render(*out)


# ---------------------------------------------------------------------------------------------
# protection


def _caught(f: ProtectionResult) -> str:
    if not f.planted_bugs_total:
        return "lawkeep could not plant bugs here"
    if f.planted_bugs_caught == f.planted_bugs_total:
        return f"your laws caught all {f.planted_bugs_total} planted bugs"
    if not f.planted_bugs_caught:
        return f"your laws caught none of {f.planted_bugs_total} planted bugs"
    return f"your laws caught {f.planted_bugs_caught} of {f.planted_bugs_total} planted bugs"


def _note(note: str) -> str:
    """The status column already says "read this one"; do not say it twice."""
    for tail in (" - read this one", " — read this one", "; read this one"):
        note = note.replace(tail, "")
    return note


def _order(report: ProtectionReport) -> list[ProtectionResult]:
    order = {"not_protected": 0, "partly": 1, "protected": 2}
    return sorted(report.functions, key=lambda f: (order[f.status], f.function))


def protection_view(report: ProtectionReport, *, standalone: bool = False) -> RenderableType:
    """Renderables for the protection report (used by `lawkeep report`)."""
    base = f" since {report.base_ref}" if report.base_ref else ""
    out: list[RenderableType] = [
        ui.heading(f"Is the code you changed{base} protected by laws?"),
        ui.dim(
            "For each changed function, lawkeep planted small bugs in a copy of it and "
            "checked whether your laws noticed. Read the code your laws do not protect."
        ),
    ]
    if report.functions:
        rows: list[list[Any]] = []
        for f in _order(report):
            what = _caught(f)
            if f.laws:
                what += " · laws: " + ", ".join(f.laws[:3]) + (" ..." if len(f.laws) > 3 else "")
            if f.note:
                what += f" · {_note(f.note)}"
            rows.append(
                [
                    short(f.function),
                    Text(STATUS_WORDS[f.status], style=_STATUS_TONE[f.status]),
                    what,
                ]
            )
        out.append(
            ui.table(["Function", "Protection", "What lawkeep found"], rows, ratios=[3, 2, 5])
        )
    else:
        out.append(ui.status("info", "No changed functions were found."))
    for n in report.notes:
        out.append(ui.status("info", n))
    return ui.group(*out)


def protection_text(report: ProtectionReport) -> str:
    return ui.render(protection_view(report))


def _md(text: str) -> str:
    return text.replace("|", "\\|")


def protection_markdown(report: ProtectionReport) -> str:
    base = f" since `{report.base_ref}`" if report.base_ref else ""
    out = [
        f"### Is the code you changed{base} protected by laws?",
        "",
        "For each changed function, lawkeep planted small bugs in a copy of it and checked "
        "whether your laws noticed. **Read the code your laws do not protect.**",
        "",
    ]
    if report.functions:
        out += [
            "| Function | Protection | What lawkeep found | Laws |",
            "| --- | --- | --- | --- |",
        ]
        for f in _order(report):
            status = STATUS_WORDS[f.status]
            status = f"**{status}**" if f.status != "protected" else status
            laws = ", ".join(f"`{law}`" for law in f.laws[:3]) or "none"
            found = _caught(f)
            if f.note:
                found += f". {_note(f.note)}"
            out.append(f"| `{_md(f.function)}` | {status} | {_md(found)} | {laws} |")
        out.append("")
    else:
        out += ["No changed functions were found.", ""]
    for n in report.notes:
        out.append(f"- {_md(n)}")
    return "\n".join(out).rstrip() + "\n"
