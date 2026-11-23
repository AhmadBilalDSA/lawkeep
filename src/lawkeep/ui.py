"""Shared terminal presentation for every lawkeep command (built on Rich).

The wording rules are in ``docs/OUTPUT-STYLE.md``; this module gives every command the same
building blocks so the screens look and read alike:

- :func:`console` / :func:`render`: a Rich console that fits the terminal width. Colour and
  Unicode boxes only on a real terminal; **plain mode** (no ANSI codes, ASCII boxes) when the
  output is not a terminal, in CI (``CI`` set), with ``NO_COLOR`` or ``TERM=dumb``.
  ``LAWKEEP_COLOR=always`` forces colour (for screenshots).
- :func:`banner` (the verdict), :func:`panel`, :func:`table`, :func:`heading`,
  :func:`status` (a line with a mark: ok / warn / fail / info), :func:`warning`,
  :func:`next_step`, :func:`dim`, :func:`bullets`, :func:`ask_yes_no`.

``--json`` output never goes through this module: agents parse ``output.emit`` JSON.
The Markdown report (``report --format markdown``) does not use Rich either.

Every helper returns a Rich renderable that decides at render time whether it is drawn in
plain mode (``console.color_system is None``), so the same screen code serves both modes.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TextIO

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

#: Header rule only, ASCII (plain mode tables).
ASCII_SIMPLE = box.Box("    \n    \n -- \n    \n    \n -- \n    \n    \n", ascii=True)

#: tone -> (colour, mark in colour mode, mark in plain mode)
TONES: dict[str, tuple[str, str, str]] = {
    "ok": ("green", "✓", "+"),
    "fail": ("red", "✗", "x"),
    "wait": ("yellow", "!", "!"),
    "warn": ("yellow", "!", "!"),
    "problem": ("red", "✗", "x"),
    "info": ("cyan", "•", "-"),
    "plain": ("default", "•", "-"),
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ---------------------------------------------------------------------------------------------
# Console


def plain_wanted(stream: TextIO | None = None) -> bool:
    """True when output should be plain text (no colour, ASCII boxes)."""
    env = os.environ
    if env.get("LAWKEEP_COLOR", "").strip().lower() in ("always", "1", "yes", "true"):
        return False
    if env.get("NO_COLOR", "") != "":
        return True
    if env.get("CI", "").strip().lower() not in ("", "0", "false"):
        return True
    if env.get("TERM", "") == "dumb":
        return True
    stream = stream if stream is not None else sys.stdout
    if stream is None:  # no stdout at all (e.g. pythonw)
        return True
    try:
        return not stream.isatty()
    except (AttributeError, ValueError, OSError):
        return True


def console(
    stream: TextIO | None = None, *, width: int | None = None, plain: bool | None = None
) -> Console:
    """A console for ``stream`` (default: the current ``sys.stdout``) that fits the terminal.

    Width: the terminal's width; when not a terminal, ``COLUMNS`` or 80.
    """
    stream = stream if stream is not None else sys.stdout
    plain = plain_wanted(stream) if plain is None else plain
    return Console(
        file=stream,
        width=width,
        color_system=None if plain else "auto",
        force_terminal=not plain,
        no_color=plain,
        highlight=False,
        markup=False,
        emoji=False,
        soft_wrap=False,
        legacy_windows=False,
    )


def is_plain(con: Console) -> bool:
    return con.color_system is None


def render(
    *items: RenderableType | None,
    stream: TextIO | None = None,
    width: int | None = None,
    plain: bool | None = None,
) -> str:
    """Render ``items`` (``None`` skipped) to a string, as they would appear on ``stream``."""
    con = console(stream, width=width, plain=plain)
    with con.capture() as cap:
        for item in items:
            if item is not None:
                con.print(item)
    text = cap.get()
    if is_plain(con):
        text = "\n".join(line.rstrip() for line in text.splitlines())
    return text.rstrip("\n") + "\n"


def show(*items: RenderableType | None, stream: TextIO | None = None) -> None:
    """Print ``items`` straight to ``stream`` (default stdout)."""
    out = stream if stream is not None else sys.stdout
    out.write(render(*items, stream=out))
    out.flush()


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


class _Adaptive:
    """A renderable built at render time, once we know whether the console is plain."""

    def __init__(self, build: Callable[[bool], RenderableType]) -> None:
        self.build = build

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.build(is_plain(console))

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement.get(console, options, self.build(is_plain(console)))


def _text(value: Any, style: str = "") -> Text:
    if isinstance(value, Text):
        return value
    return Text("" if value is None else str(value), style=style)


# ---------------------------------------------------------------------------------------------
# Building blocks


def banner(title: str, detail: str | Text | None = None, *, tone: str = "info") -> RenderableType:
    """The verdict, in a box: ``ALL LAWS HOLD`` + one plain sentence saying what it means."""
    colour, mark, _ = TONES.get(tone, TONES["info"])

    def build(plain: bool) -> RenderableType:
        head = Text(title if plain else f"{mark} {title}", style="" if plain else f"bold {colour}")
        body: list[RenderableType] = [head]
        if detail:
            body.append(_text(detail))
        return Panel(
            Group(*body),
            box=box.ASCII if plain else box.ROUNDED,
            border_style="" if plain else colour,
            padding=(0, 1),
            expand=True,
        )

    return _Adaptive(build)


def panel(
    *body: RenderableType | str | None,
    title: str | None = None,
    tone: str = "plain",
    subtitle: str | None = None,
) -> RenderableType:
    """A box around one item (a draft law, a proposal...). ``None`` body parts are skipped."""
    colour = TONES.get(tone, TONES["plain"])[0]
    parts = [_text(b) if isinstance(b, str) else b for b in body if b is not None]

    def build(plain: bool) -> RenderableType:
        return Panel(
            Group(*parts),
            title=Text(f" {title} ", style="" if plain else "bold") if title else None,
            title_align="left",
            subtitle=Text(f" {subtitle} ", style="" if plain else "dim") if subtitle else None,
            subtitle_align="right",
            box=box.ASCII if plain else box.ROUNDED,
            border_style="" if plain or colour == "default" else colour,
            padding=(0, 1),
            expand=True,
        )

    return _Adaptive(build)


def heading(text: str) -> RenderableType:
    """A section title. Plain mode underlines it with dashes."""

    def build(plain: bool) -> RenderableType:
        if plain:
            return Text(f"{text}\n{'-' * min(len(text), 70)}")
        return Text(text, style="bold underline")

    return _Adaptive(build)


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    ratios: Sequence[int | None] | None = None,
    min_widths: Sequence[int | None] | None = None,
    max_widths: Sequence[int | None] | None = None,
    no_wrap: Sequence[bool] | None = None,
) -> RenderableType:
    """A table that fits the window: long cells wrap between words, never off the edge."""
    rows = [list(r) for r in rows]

    def build(plain: bool) -> RenderableType:
        t = Table(
            box=ASCII_SIMPLE if plain else box.SIMPLE_HEAD,
            show_header=True,
            header_style="" if plain else "bold",
            pad_edge=False,
            show_edge=False,
            expand=False,
            padding=(0, 1),
        )
        for i, h in enumerate(headers):
            t.add_column(
                h,
                ratio=(ratios[i] if ratios and i < len(ratios) else None),
                min_width=(min_widths[i] if min_widths and i < len(min_widths) else None),
                max_width=(max_widths[i] if max_widths and i < len(max_widths) else None),
                no_wrap=bool(no_wrap and i < len(no_wrap) and no_wrap[i]),
                overflow="fold",
            )
        for r in rows:
            t.add_row(*[c if not isinstance(c, str) and c is not None else _text(c) for c in r])
        return t

    return _Adaptive(build)


def status(tone: str, text: str | Text, *, style: str | None = None) -> RenderableType:
    """``✓ Works with your current code — ...`` with a hanging indent (plain: ``+ ...``)."""
    colour, mark, plain_mark = TONES.get(tone, TONES["plain"])

    def build(plain: bool) -> RenderableType:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True)
        grid.add_column()
        body = _text(text)
        if not plain and style:
            body = body.copy()
            body.stylize(style)
        grid.add_row(
            Text(plain_mark if plain else mark, style="" if plain else f"bold {colour}"), body
        )
        return grid

    return _Adaptive(build)


def warning(text: str | Text) -> RenderableType:
    """A yellow warning line (plain: ``! ...``)."""
    return status("warn", text, style="yellow")


def next_step(text: str | Text) -> RenderableType:
    """The last line of a screen: what to do now."""

    def build(plain: bool) -> RenderableType:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True)
        grid.add_column()
        body = _text(text)
        if body.plain[:1].islower():
            body = Text(body.plain[:1].upper()) + body[1:]
        grid.add_row(Text("Next step:", style="" if plain else "bold cyan"), body)
        return grid

    return _Adaptive(build)


def dim(text: str | Text) -> RenderableType:
    """Secondary detail (ids, paths, timings): dim in colour, plain text otherwise."""
    return _text(text, style="dim")


def bold(text: str) -> Text:
    return Text(text, style="bold")


def quote(sentence: str | None) -> Text:
    """A law sentence, quoted and bold: the most important line of an item."""
    return Text(f"“{sentence}”" if sentence else "(this law has no readable sentence)", "bold")


def bullets(items: Iterable[str | Text], *, tone: str = "plain") -> RenderableType:
    return Group(*[status(tone, i) for i in items])


def indent(item: RenderableType, n: int = 2) -> RenderableType:
    return Padding(item, (0, 0, 0, n))


def blank() -> Text:
    return Text("")


def group(*items: RenderableType | None) -> RenderableType:
    return Group(*[i for i in items if i is not None])


def label_value(rows: Iterable[tuple[str, str | Text | None]]) -> RenderableType:
    """Two columns, ``Before:  "..."`` style, with the value wrapping under itself."""
    rows = [(k, v) for k, v in rows if v is not None]

    def build(plain: bool) -> RenderableType:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(no_wrap=True, style="" if plain else "bold")
        grid.add_column()
        for k, v in rows:
            grid.add_row(Text(k), _text(v))
        return grid

    return _Adaptive(build)


def ask_yes_no(question: str, *, default: bool = False) -> bool | None:
    """Ask in the terminal. ``None`` when there is no terminal (then return WAITING)."""
    from lawkeep.output import yes_no

    return yes_no(question, default=default, stream_in=sys.stdin)


def plural(n: int, word: str, many: str | None = None) -> str:
    """``plural(1, "law")`` -> ``1 law``; ``plural(3, "law")`` -> ``3 laws``."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


class _Breakable:
    """Text that wraps between words and, inside a word too long for the line (a law id, a
    file path), only after ``.``, ``/``, ``_`` or ``:``; nothing is added or cut."""

    SEPS = "./:"  # preferred break points; "_" only when a part is still too long

    def __init__(self, text: str, style: str = "dim") -> None:
        self.text = text
        self.style = style

    @staticmethod
    def _split(word: str, seps: str) -> list[str]:
        out: list[str] = []
        cur = ""
        for ch in word:
            cur += ch
            if ch in seps:
                out.append(cur)
                cur = ""
        if cur:
            out.append(cur)
        return out

    def _pieces(self, word: str, width: int | None = None) -> list[str]:
        out: list[str] = []
        for part in self._split(word, self.SEPS):
            if width is not None and len(part) > width:
                out.extend(self._split(part, "_"))
            else:
                out.append(part)
        return out

    def lines(self, width: int) -> list[str]:
        width = max(width, 8)
        out: list[str] = []
        cur = ""
        for word in self.text.split():
            cand = f"{cur} {word}" if cur else word
            if len(cand) <= width:
                cur = cand
                continue
            if cur:
                out.append(cur)
                cur = ""
            if len(word) <= width:
                cur = word
                continue
            for piece in self._pieces(word, width):
                while len(piece) > width:  # no separator close enough: last resort
                    if cur:
                        out.append(cur)
                        cur = ""
                    out.append(piece[:width])
                    piece = piece[width:]
                if len(cur) + len(piece) <= width:
                    cur += piece
                else:
                    out.append(cur)
                    cur = piece
        if cur:
            out.append(cur)
        return out or [""]

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        style = "" if is_plain(console) else self.style
        for line in self.lines(options.max_width):
            yield Text(line, style=style, no_wrap=True, overflow="ignore")

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        longest = max((len(p) for w in self.text.split() for p in self._pieces(w)), default=1)
        return Measurement(min(longest, options.max_width), min(len(self.text), options.max_width))


def ident(text: str, *, style: str = "dim") -> RenderableType:
    """Background detail such as ``law id refunds.refund_is_capped · file laws/refunds.py``:
    dim, and long ids wrap at dots and slashes instead of in the middle of a word."""
    return _Breakable(text, style)
