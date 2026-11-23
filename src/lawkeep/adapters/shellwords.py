"""A small POSIX-shell lexer for the pre-tool-use guard (``guard.py`` / ``writes.py``).

It turns a command line into simple commands with their words and redirections, so the guard
can look at what a command *writes to* instead of searching its text. ``shlex`` alone cannot do
this: it does not know ``$(...)`` nesting, backticks, here-documents, ``2>``/``&>``
redirections or that a newline ends a command. What it understands:

- quoting: ``'...'``, ``"..."`` (with ``\\`` escapes), ``$'...'``, backslash escapes, line
  continuations;
- separators: ``;``, ``&``, ``&&``, ``||``, ``|``, ``|&``, newlines, ``(``, ``)`` (subshell
  parentheses only separate: a ``cd`` inside them is treated as if it stayed in effect);
- expansions inside words, kept as parts: ``$NAME`` / ``${NAME}`` (``var``), ``$(...)``,
  backticks and ``<(...)``/``>(...)`` (``sub``, lexed recursively), ``$((...))`` and other
  ``${...}`` forms (``dyn``: value unknown);
- redirections: ``<``, ``>``, ``>>``, ``>|``, ``<>``, ``&>``, ``&>>``, ``>&``, ``<&``, with an
  optional file-descriptor number (``2>``), here-strings ``<<<`` and here-documents ``<<`` /
  ``<<-`` (the body is attached to the redirection and is never lexed as commands);
- comments (``#`` at the start of a word).

It never runs anything, never expands globs or variables (``writes.py`` does that, on paper),
and raises :class:`Incomplete` for unbalanced quotes or parentheses: such a command does not
run in a real shell either, and the guard then falls back to a careful text check.
``case ... esac`` patterns (``a)``) are not understood.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


class Incomplete(ValueError):
    """The command has unbalanced quotes, parentheses or an unterminated substitution."""


@dataclass
class Part:
    """One piece of a word."""

    kind: str  # "lit" | "var" | "sub" | "dyn"
    text: str = ""  # lit: the characters; var: the variable name; dyn: the source text
    quoted: bool = False  # lit: inside quotes (no glob, brace or tilde expansion)
    script: list[Command] | None = None  # sub: the commands inside $(...), `...`, <(...)


@dataclass
class Word:
    parts: list[Part] = field(default_factory=list["Part"])

    def is_static(self) -> bool:
        return all(p.kind == "lit" for p in self.parts)

    def static_text(self) -> str:
        """The literal characters only (expansions left out)."""
        return "".join(p.text for p in self.parts if p.kind == "lit")

    def subs(self) -> list[list[Command]]:
        return [p.script for p in self.parts if p.kind == "sub" and p.script is not None]


@dataclass
class Redirect:
    op: str  # ">", ">>", ">|", "<>", "&>", "&>>", ">&", "<", "<&", "<<", "<<-", "<<<"
    fd: str = ""  # explicit file descriptor number ("2" in 2>), else ""
    target: Word | None = None  # file (or fd number for >& / <&); here-string word for <<<
    body: str | None = None  # here-document body

    def writes(self) -> bool:
        return self.op in (">", ">>", ">|", "<>", "&>", "&>>", ">&")


@dataclass
class Command:
    """A simple command: words plus redirections."""

    words: list[Word] = field(default_factory=list["Word"])
    redirects: list[Redirect] = field(default_factory=list["Redirect"])
    #: the command whose output is piped into this one (``a | b``: b.piped_from is a)
    piped_from: Command | None = None

    def empty(self) -> bool:
        return not self.words and not self.redirects


_LitFn = Callable[[str, bool], None]  # the `lit(text, quoted)` helper in `_Lexer.word`
_OPERATOR_CHARS = ";&|()"
_WORD_END = " \t\n;&|()<>"


class _Lexer:
    def __init__(self, src: str) -> None:
        self.s = src
        self.i = 0
        self.pending: list[tuple[Redirect, str, bool]] = []  # heredocs waiting for a newline

    # -- helpers ---------------------------------------------------------------------------
    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.s[j] if j < len(self.s) else ""

    def startswith(self, text: str) -> bool:
        return self.s.startswith(text, self.i)

    def read_heredocs(self) -> None:
        """Called just after a newline: read the bodies of pending here-documents."""
        for redirect, delim, strip_tabs in self.pending:
            lines: list[str] = []
            while self.i < len(self.s):
                end = self.s.find("\n", self.i)
                line = self.s[self.i :] if end < 0 else self.s[self.i : end]
                self.i = len(self.s) if end < 0 else end + 1
                check = line.lstrip("\t") if strip_tabs else line
                if check == delim:
                    break
                lines.append(line)
            redirect.body = "\n".join(lines) + ("\n" if lines else "")
        self.pending = []

    # -- scripts ---------------------------------------------------------------------------
    def script(self, stop: str | None = None) -> list[Command]:
        """Commands until the end (``stop=None``) or an unmatched ``)`` (``stop=")"``)."""
        commands: list[Command] = []
        cur = Command()
        depth = 0  # open subshell parentheses inside this script

        def finish(pipe_next: bool = False) -> None:
            nonlocal cur
            prev = cur if not cur.empty() else None
            if prev is not None:
                commands.append(prev)
            cur = Command()
            if pipe_next and prev is not None:
                cur.piped_from = prev

        while self.i < len(self.s):
            c = self.peek()
            if c in " \t":
                self.i += 1
            elif c == "\\" and self.peek(1) == "\n":
                self.i += 2
            elif c == "\n":
                self.i += 1
                finish()
                if self.pending:
                    self.read_heredocs()
            elif c == "#":
                end = self.s.find("\n", self.i)
                self.i = len(self.s) if end < 0 else end
            elif c == ")" and stop == ")" and depth == 0:
                self.i += 1
                finish()
                return commands
            elif c in "<>" and self.peek(1) == "(":
                cur.words.append(self.word())  # process substitution: a word
            elif c in "<>" or (c == "&" and self.peek(1) == ">"):
                cur.redirects.append(self.redirect(""))
            elif c in _OPERATOR_CHARS:
                two = self.s[self.i : self.i + 2]
                if two in ("&&", "||", ";;", ";&"):
                    self.i += 2
                    finish()
                elif two == "|&":
                    self.i += 2
                    finish(pipe_next=True)
                elif c == "|":
                    self.i += 1
                    finish(pipe_next=True)
                else:
                    depth += 1 if c == "(" else -1 if c == ")" and depth else 0
                    self.i += 1
                    finish()
            else:
                start = self.i
                word = self.word()
                # "2>file": a word of digits right before < or > is a file descriptor
                text = self.s[start : self.i]
                if text.isdigit() and self.peek() in "<>" and self.peek(1) != "(":
                    cur.redirects.append(self.redirect(text))
                else:
                    cur.words.append(word)
        if stop == ")":
            raise Incomplete("unterminated $(")
        finish()
        return commands

    def redirect(self, fd: str) -> Redirect:
        for op in ("&>>", "&>", "<<<", "<<-", "<<", "<>", "<&", ">>", ">|", ">&", "<", ">"):
            if self.startswith(op):
                self.i += len(op)
                break
        else:  # pragma: no cover - only called on < > &
            raise Incomplete("bad redirection")
        while self.peek() in (" ", "\t"):
            self.i += 1
        if not self.peek() or self.peek() in "\n;&|()<>":
            raise Incomplete("redirection without a target")
        target = self.word()
        red = Redirect(op=op, fd=fd, target=target)
        if op in ("<<", "<<-"):  # the body follows the next newline (see read_heredocs)
            self.pending.append((red, target.static_text(), op == "<<-"))
            red.target = None
        return red

    # -- words -----------------------------------------------------------------------------
    def word(self) -> Word:
        parts: list[Part] = []

        def lit(text: str, quoted: bool) -> None:
            if parts and parts[-1].kind == "lit" and parts[-1].quoted == quoted:
                parts[-1].text += text
            else:
                parts.append(Part("lit", text, quoted))

        while self.i < len(self.s):
            c = self.peek()
            if c in "<>" and self.peek(1) == "(":
                self.i += 2
                parts.append(Part("sub", c + "(", script=self.script(stop=")")))
                continue
            if c in _WORD_END:
                break
            if c == "'":
                end = self.s.find("'", self.i + 1)
                if end < 0:
                    raise Incomplete("unterminated '")
                lit(self.s[self.i + 1 : end], True)
                self.i = end + 1
            elif c == '"':
                self.i += 1
                self.dquote(parts, lit)
            elif c == "\\":
                nxt = self.peek(1)
                if nxt == "\n":
                    self.i += 2
                else:
                    lit(nxt, True)
                    self.i += 2
            elif c == "$":
                self.dollar(parts, lit, in_dquote=False)
            elif c == "`":
                self.backtick(parts)
            else:
                lit(c, False)
                self.i += 1
        return Word(parts)

    def dquote(self, parts: list[Part], lit: _LitFn) -> None:
        while True:
            if self.i >= len(self.s):
                raise Incomplete('unterminated "')
            c = self.peek()
            if c == '"':
                self.i += 1
                lit("", True)  # "" is an (empty) word, not nothing
                return
            if c == "\\" and self.peek(1) in '$`"\\\n':
                if self.peek(1) != "\n":
                    lit(self.peek(1), True)
                self.i += 2
            elif c == "$":
                self.dollar(parts, lit, in_dquote=True)
            elif c == "`":
                self.backtick(parts)
            else:
                lit(c, True)
                self.i += 1

    def dollar(self, parts: list[Part], lit: _LitFn, *, in_dquote: bool) -> None:
        nxt = self.peek(1)
        if self.startswith("$(("):
            start = self.i
            self.i += 3
            depth = 2
            while self.i < len(self.s) and depth:
                ch = self.peek()
                depth += 1 if ch == "(" else -1 if ch == ")" else 0
                self.i += 1
            if depth:
                raise Incomplete("unterminated $((")
            parts.append(Part("dyn", self.s[start : self.i]))
        elif nxt == "(":
            self.i += 2
            parts.append(Part("sub", "$(", script=self.script(stop=")")))
        elif nxt == "{":
            end = self._matching_brace(self.i + 1)
            inner = self.s[self.i + 2 : end]
            self.i = end + 1
            if inner.replace("_", "a").isalnum() and not inner[:1].isdigit():
                parts.append(Part("var", inner))
            else:
                parts.append(Part("dyn", "${" + inner + "}"))
        elif nxt == "'" and not in_dquote:
            self.i += 2
            out: list[str] = []
            while True:
                if self.i >= len(self.s):
                    raise Incomplete("unterminated $'")
                ch = self.peek()
                if ch == "'":
                    self.i += 1
                    break
                if ch == "\\" and self.i + 1 < len(self.s):
                    esc = self.peek(1)
                    out.append({"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}.get(esc, esc))
                    self.i += 2
                    continue
                out.append(ch)
                self.i += 1
            lit("".join(out), True)
        elif nxt and (nxt.isalpha() or nxt == "_"):
            j = self.i + 1
            while j < len(self.s) and (self.s[j].isalnum() or self.s[j] == "_"):
                j += 1
            parts.append(Part("var", self.s[self.i + 1 : j]))
            self.i = j
        elif nxt and (nxt.isdigit() or nxt in "@*#?$!-"):
            parts.append(Part("dyn", "$" + nxt))
            self.i += 2
        else:
            lit("$", in_dquote)
            self.i += 1

    def _matching_brace(self, start: int) -> int:
        depth = 0
        j = start
        while j < len(self.s):
            ch = self.s[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return j
            elif ch == "\\":
                j += 1
            j += 1
        raise Incomplete("unterminated ${")

    def backtick(self, parts: list[Part]) -> None:
        j = self.i + 1
        body: list[str] = []
        while j < len(self.s) and self.s[j] != "`":
            if self.s[j] == "\\" and j + 1 < len(self.s) and self.s[j + 1] in "`\\$":
                body.append(self.s[j + 1])
                j += 2
                continue
            body.append(self.s[j])
            j += 1
        if j >= len(self.s):
            raise Incomplete("unterminated `")
        self.i = j + 1
        parts.append(Part("sub", "`", script=parse("".join(body))))


def parse(command: str) -> list[Command]:
    """The simple commands of ``command`` in order (nested ones stay inside their words).

    Raises :class:`Incomplete` for unbalanced quotes, parentheses or substitutions.
    """
    lexer = _Lexer(command)
    commands = lexer.script(None)
    if lexer.pending:  # here-document at the very end without a newline: empty body
        lexer.read_heredocs()
    return commands


def walk(commands: list[Command]) -> list[Command]:
    """Every command, including those inside substitutions (depth first, in order)."""
    out: list[Command] = []
    for cmd in commands:
        out.append(cmd)
        words = list(cmd.words) + [r.target for r in cmd.redirects if r.target is not None]
        for w in words:
            for sub in w.subs():
                out.extend(walk(sub))
    return out
