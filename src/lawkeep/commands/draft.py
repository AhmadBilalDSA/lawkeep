"""`lawkeep draft`: ask a separate session of your coding agent to draft laws.

Owner: workstream D (law lifecycle).

    lawkeep draft billing.refunds --request "refunds never exceed the payment" [--docs SPEC.md]

1. Collect the request, the docs and the module's PUBLIC function signatures and docstrings
   (read with ``ast``; the function bodies are not sent).
2. Start the user's own coding agent once, non-interactively (``agent_launcher``), with a
   law-writer prompt: write ``@law`` tests into ``laws/drafts/`` and new input generators
   into ``laws/strategies.py``, from what the person wants, not from what the code does.
3. Keep the agent to drafts. A change to ``laws/strategies.py`` that only ADDS generators is
   recorded as an automatic stricter-side change when ``add_law = "auto"``; otherwise the new
   generators are parked as ``laws/drafts/strategies.py`` and installed when a person
   approves a draft. Any other change to a file ``laws.lock`` fingerprints (an approved law,
   an existing generator, ``lawkeep.toml``, a helper in ``laws/``) is put back to the approved
   version and becomes a WAITING PROPOSAL (``lawkeep.draft_edits``): never applied on its
   own, even when stricter; ``lawkeep approve`` applies or rejects it.
4. For each draft: does it import and run; does it hold on today's code (a failure is shown
   as "either the code has a bug or the law is wrong: you decide", with the example); the
   quick strength check when workstream C's ``run_quick_check`` is available.

Drafts are ignored by the gate until approved (``lawkeep approve``).
Result: 2 drafts (or the agent's changes to approved files) waiting for a person · 1 a draft
fails on today's code / the quick check · 3 agent not set up, failed or wrote nothing.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any, cast

from rich.console import RenderableType

from lawkeep import (
    agent_launcher,
    config,
    discovery,
    draft_edits,
    exitcodes,
    history,
    lock,
    output,
    paths,
    ui,
)
from lawkeep.errors import SetupError
from lawkeep.examples_table import DRAFT_STRATEGIES, LawProbe, probe_law, render_rows
from lawkeep.models import SimpleReport, utc_now_iso
from lawkeep.permissions import decide

HELP = "ask your coding agent (in a separate session) to write draft laws for you to approve"

SIDECAR = "drafts.json"
MAX_DOC_CHARS = 20_000
MAX_DOCS_TOTAL = 60_000
MAX_MODULE_FILES = 20


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "module",
        nargs="?",
        help="the code to write laws for: a module name (billing.refunds) or a file or folder path",
    )
    parser.add_argument(
        "--request",
        default="",
        help='the rules you want, in plain English (e.g. "refunds never exceed the payment")',
    )
    parser.add_argument(
        "--docs",
        nargs="*",
        default=[],
        metavar="PATH",
        help="documents that describe the rules (specs, policies, READMEs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only show the message lawkeep would send to your agent; start nothing",
    )
    parser.add_argument(
        "--timeout", type=int, default=None, help="how many seconds to wait for the agent"
    )
    parser.add_argument(
        "--actor", default=None, help='who writes the drafts, e.g. "agent:claude-code"'
    )


# ---------------------------------------------------------------------------------------------
# Drafts sidecar (laws/drafts/drafts.json): who drafted which file and from which request.


def sidecar_path(root: Path) -> Path:
    return paths.drafts_dir(root) / SIDECAR


def read_sidecar(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(sidecar_path(root).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            sidecar = cast("dict[str, Any]", data)
            if isinstance(sidecar.get("files"), dict):
                return sidecar
    except (OSError, ValueError):
        pass
    return {"files": {}}


def write_sidecar(root: Path, data: dict[str, Any]) -> None:
    p = sidecar_path(root)
    if not data.get("files"):
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def draft_files(root: Path) -> list[Path]:
    """Draft law files (``laws/drafts/**/*.py`` except generators, conftest, proposals)."""
    d = paths.drafts_dir(root)
    out: list[Path] = []
    for p in discovery.iter_python_files(root, include_drafts=True):
        try:
            parts = p.relative_to(d).parts
        except ValueError:
            continue
        if parts and parts[0] == "proposals":
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------------------------------------
# What the agent is told about the module (no function bodies)


def resolve_module(root: Path, cfg: config.Config, target: str) -> tuple[list[Path], str]:
    """Files of the module/package and its import name."""
    sources = cfg.project.source_dirs(root)
    p = Path(target)
    candidates = [p] if p.is_absolute() else [Path.cwd() / p, root / p]
    for c in candidates:
        if c.exists():
            c = c.resolve()
            files = (
                [c]
                if c.is_file()
                else sorted(
                    f
                    for f in c.rglob("*.py")
                    if "__pycache__" not in f.parts
                    and not f.name.startswith("_")
                    or f.name == "__init__.py"
                )[:MAX_MODULE_FILES]
            )
            return files, _import_name(c, sources)
    parts = target.split(".")
    for sd in sources:
        f = sd.joinpath(*parts).with_suffix(".py")
        if f.is_file():
            return [f], target
        pkg = sd.joinpath(*parts)
        if (pkg / "__init__.py").is_file():
            files = sorted(x for x in pkg.rglob("*.py") if "__pycache__" not in x.parts)
            return files[:MAX_MODULE_FILES], target
    raise SetupError(
        f"Cannot find the module {target!r} (looked in {', '.join(paths.rel(root, s) or '.' for s in sources)}).",
        hint="Give a module name like billing.refunds or a path like src/billing/refunds.py; "
        "set [project] source in lawkeep.toml if your code lives elsewhere.",
    )


def _import_name(path: Path, sources: list[Path]) -> str:
    for sd in sources:
        try:
            relp = path.relative_to(sd)
        except ValueError:
            continue
        parts = list(relp.with_suffix("").parts) if path.is_file() else list(relp.parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            return ".".join(parts)
    return path.stem


def _sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({ast.unparse(node.args)}){ret}"


def _doc(
    node: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, indent: str
) -> list[str]:
    doc = ast.get_docstring(node)
    if not doc:
        return []
    return [
        f'{indent}"""{line}' if i == 0 else f"{indent}{line}"
        for i, line in enumerate(doc.strip().splitlines()[:15])
    ] + [f'{indent}"""']


def public_api(path: Path, import_name: str) -> str:
    """Public signatures and docstrings of one file (bodies replaced by ``...``)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return f"# {import_name}: could not be read ({exc})"
    out = [f"# module {import_name}"]
    out += _doc(tree, "")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            out.append(_sig(node) + ":")
            out += _doc(node, "    ")
            out.append("    ...")
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            decos = [f"@{ast.unparse(d)}" for d in node.decorator_list]
            out += decos
            out.append(f"class {node.name}({bases}):" if bases else f"class {node.name}:")
            out += _doc(node, "    ")
            for sub in node.body:
                if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    default = f" = {ast.unparse(sub.value)}" if sub.value is not None else ""
                    out.append(f"    {sub.target.id}: {ast.unparse(sub.annotation)}{default}")
                elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    not sub.name.startswith("_") or sub.name == "__init__"
                ):
                    out.append("    " + _sig(sub) + ":")
                    out += _doc(sub, "        ")
                    out.append("        ...")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if names and all(n.isupper() for n in names) and node.value is not None:
                text = ast.unparse(node)
                if len(text) <= 120:
                    out.append(text)
    return "\n".join(out)


COOKBOOK = """\
Law recipes (pick the ones that fit the request; one law per rule):
- Invariant: something is always true of the result.
    @law("A refund is never negative",
         inputs={"charge": "a payment the customer made",
                 "amount": "how much the customer asks to get back"})
    @given(charge=st.charges(), amount=st.money())
    def refund_never_negative(charge, amount):
        assert refund(charge, amount).total >= 0
- Round trip: decoding what you encoded gives back the original.
    assert parse(render(order)) == order
- Idempotence: doing it twice is the same as doing it once.
    assert normalize(normalize(s)) == normalize(s)
- Conservation: nothing is created or lost (totals before == totals after).
    assert sum(a.balance for a in after) == sum(a.balance for a in before)
- Exact amount (companion law): bounds alone let wrong answers through
  ("refund <= charge" also holds for a refund of 0), so pair each bound with a law that
  pins the exact expected value on the cases where it is known.
    @law("A refund within the charge is paid in full",
         inputs={"charge": "a payment the customer made",
                 "amount": "how much the customer asks to get back"})
    @given(charge=st.charges(), amount=st.money())
    def refund_pays_requested_amount(charge, amount):
        assume(amount <= charge.total)
        assert refund(charge, amount).total == amount
- Rejection: invalid input is refused with an error, never silently accepted.
    with pytest.raises(ValueError): refund(charge, -1)
"""


def build_prompt(
    *,
    request: str,
    import_name: str,
    api: str,
    docs: list[tuple[str, str]],
    existing: list[tuple[str, str | None]],
    has_strategies: bool,
) -> str:
    slug = import_name.split(".")[-1]
    doc_text = "\n\n".join(f"--- {name} ---\n{text}" for name, text in docs) or "(none)"
    existing_text = "\n".join(f"- {i}: {s or '(no sentence)'}" for i, s in existing) or "(none yet)"
    return f"""\
You are the law writer for lawkeep (https://github.com/lawkeep/lawkeep). A "law" is a rule
the code must obey for EVERY input: one plain-English sentence plus a property-based test.
People approve laws by reading the sentence and a table of concrete examples, not the code.

Write laws for the Python module `{import_name}` from what the person WANTS (their request
and documents below), not from what the code currently does. Do not read the function
bodies to decide what is correct; if the code disagrees with the request, the law should
follow the request (lawkeep will show the disagreement to the person).

THE PERSON'S REQUEST
{
        request
        or "(no request given: draft the most important rules the documents and docstrings describe)"
    }

DOCUMENTS
{doc_text}

PUBLIC FUNCTIONS OF `{import_name}` (signatures and docstrings only)
{api}

LAWS THAT ALREADY EXIST (do not duplicate them)
{existing_text}

HOW TO WRITE A LAW
- Put each law in its own new file under laws/drafts/, named laws/drafts/{slug}_<short_name>.py.
- Start the file with:
      from lawkeep import law, given, st, example, assume
      from {import_name} import <the functions you need>
- Decorate each law with
      @law("<one plain-English sentence a non-programmer understands>",
           inputs={{"<input name>": "<what this input is, in plain words>", ...}})
  and @given(...) with input generators from `st` (all of Hypothesis' strategies, plus
  st.money(), st.percentages(), st.non_empty_text(), st.ids()).
- ALWAYS fill in `inputs=` with one short plain-English description for EVERY input of the
  law (every name in @given), e.g. inputs={{"amount": "the amount the customer asks to get
  back, in dollars"}}. People see it next to the example table when they approve the law.
- Add 1-3 @example(...) edge cases (zero, empty, boundaries) that the person will recognise.
- If you need a new input generator (e.g. valid orders), ADD a function to laws/strategies.py
  that returns a strategy (create the file if missing{
        "" if not has_strategies else "; never change or remove the generators already there"
    }),
  and use it as st.<name>() in the law.
- Keep each law small and fast; no network, no files, no sleeping.

{COOKBOOK}
RULES
- Only create files in laws/drafts/ and add generators to laws/strategies.py. Do NOT edit any
  other file: not the code under test, not other laws, not lawkeep.toml, not laws.lock.
  (lawkeep never applies an edit to an approved law, an existing generator or lawkeep.toml
  made here: it puts the approved version back and shows your edit to the person as a
  proposal. If you think one must change, say why in your final message on its own line:
  `Changed <path>: <why>`.)
- Do not run `lawkeep approve`; a person approves the drafts.
- Nobody is there to answer questions during this run, so do not wait for a reply: if
  something blocks you, skip it, write every other draft, and say what was blocked in your
  final message.
- If a rule in the request cannot be written as a law, say so in your final message instead
  of writing a weak law.
- Finish with a short list of the drafts you wrote (file, sentence) and anything you could
  not do.
"""


# ---------------------------------------------------------------------------------------------
# Snapshots: keep the agent to drafts


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _law_files_snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in lock.iter_fingerprinted_files(root):
        try:
            out[paths.rel(root, p)] = p.read_bytes()
        except OSError:
            continue
    return out


def _drafts_snapshot(root: Path) -> dict[str, str]:
    return {paths.rel(root, p): _sha(p.read_bytes()) for p in draft_files(root)}


def _source_snapshot(root: Path, cfg: config.Config) -> dict[str, str]:
    out: dict[str, str] = {}
    skip = {"__pycache__", ".venv", "venv", ".git", "node_modules", ".lawkeep", "laws"}
    for sd in cfg.project.source_dirs(root):
        if not sd.is_dir():
            continue
        for p in sd.rglob("*.py"):
            try:
                relparts = p.relative_to(root).parts
            except ValueError:
                relparts = p.parts
            if any(part in skip or part.endswith(".egg-info") for part in relparts[:-1]):
                continue
            try:
                out[paths.rel(root, p)] = _sha(p.read_bytes())
            except OSError:
                continue
            if len(out) > 5000:
                return out
    return out


def _top_level_defs(source: str) -> dict[str, str] | None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    out: dict[str, str] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        for n in names:
            out[n] = ast.dump(node)
    return out


def strategies_change(old: str, new: str) -> tuple[bool, list[str], list[str]]:
    """(only_adds, added names, changed-or-removed names) between two strategies.py texts."""
    a = _top_level_defs(old) if old else {}
    b = _top_level_defs(new)
    if a is None or b is None:
        return False, [], ["(file does not parse)"]
    added = sorted(n for n in b if n not in a)
    changed = sorted(n for n in a if n not in b or a[n] != b[n])
    return not changed, added, changed


def _handle_law_file_changes(
    root: Path,
    cfg: config.Config,
    before: dict[str, bytes],
    *,
    actor: str,
    request: str,
    lock_was_clean: bool,
    agent_output: str = "",
) -> tuple[list[str], list[draft_edits.WaitingEdit]]:
    """Keep the agent's law-file edits from taking effect without a person.

    New generators in laws/strategies.py are recorded (``add_law = "auto"``) or parked in
    laws/drafts/strategies.py, as before. Every other change to a fingerprinted file (an
    approved law, an existing generator, lawkeep.toml, ...) is put back and turned into a
    waiting proposal (``draft_edits``). Returns (notes, waiting proposals).
    """
    notes: list[str] = []
    after = _law_files_snapshot(root)
    strat_rel = paths.rel(root, paths.strategies_path(root))
    edits = draft_edits.capture_and_restore(root, before, after, skip={strat_rel})
    strat_proposal: tuple[bytes | None, bytes | None] | None = None
    if before.get(strat_rel) != after.get(strat_rel):
        old_text = before.get(strat_rel, b"").decode("utf-8", "replace")
        new_text = after.get(strat_rel, b"").decode("utf-8", "replace")
        only_adds, added, _changed = strategies_change(old_text, new_text)
        adds_text: str | None = new_text if only_adds and added else None
        if adds_text is None:
            # changes to existing generators (or anything that is not a pure addition)
            # become a proposal; new generators are split off and follow the usual rule
            draft_edits.restore(root, strat_rel, before.get(strat_rel))
            strat_proposal = (before.get(strat_rel), after.get(strat_rel))
            adds_text = draft_edits.additions_only(old_text, new_text, added)
        perm = decide(cfg, "add_law", "stricter")
        if adds_text is not None and perm.may_apply and lock_was_clean:
            paths.strategies_path(root).write_text(adds_text, encoding="utf-8")
            history.record_change(
                root,
                action="add_strategy",
                actor=actor,
                direction="stricter",
                reason=request,
                permission_used="auto",
                sentence_after=f"new input generators: {', '.join(added)}",
                details={
                    "evidence": "only adds input generators; no existing generator changed",
                    "added": added,
                    "permission_key": perm.key,
                },
            )
            notes.append(
                f"The agent added input generators to laws/strategies.py ({', '.join(added)}); "
                'this only adds, so it was recorded automatically (add_law = "auto").'
            )
        elif adds_text is not None and strat_proposal is None:
            parked = paths.drafts_dir(root) / DRAFT_STRATEGIES
            parked.parent.mkdir(parents=True, exist_ok=True)
            parked.write_text(adds_text, encoding="utf-8")
            draft_edits.restore(root, strat_rel, before.get(strat_rel))
            notes.append(
                "The agent added input generators to laws/strategies.py; lawkeep kept them "
                "aside in laws/drafts/strategies.py because your settings or an earlier "
                "unrecorded change mean they need your yes. They are installed when you "
                "approve a draft."
            )
        elif added and strat_proposal is not None:
            notes.append(
                f"The new input generators ({', '.join(added)}) are part of the waiting change "
                "to laws/strategies.py, so drafts that use them may not run until you approve it."
            )
        if strat_proposal is not None:
            on_disk = paths.strategies_path(root)
            edits.append(
                draft_edits.DraftEdit(
                    strat_rel,
                    on_disk.read_bytes() if on_disk.is_file() else None,
                    strat_proposal[1],
                )
            )
    waiting = draft_edits.make_proposals(root, cfg, edits, actor=actor, agent_output=agent_output)
    return notes, waiting


# ---------------------------------------------------------------------------------------------


def _quick_check(
    root: Path, cfg: config.Config, law_ids: list[str]
) -> tuple[dict[str, Any], str | None]:
    if not law_ids or not cfg.strength.quick_check:
        return {}, None
    try:
        from lawkeep import strength

        results = strength.run_quick_check(root, cfg, law_ids=law_ids, include_drafts=True)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, say why
        return {}, f"The quick strength check could not run ({type(exc).__name__}: {exc})."
    return {r.law_id: r for r in results}, None


def quick_check_text(result: Any) -> str | None:
    if result is None:
        return None
    outcomes = list(getattr(result, "quick_check", []) or [])
    flags = list(getattr(result, "flags", []) or [])
    if outcomes:
        caught = sum(1 for o in outcomes if o.caught)
        text = f"quick check: caught {caught} of {len(outcomes)} dumb versions"
    else:
        text = "quick check: no dumb versions were tried"
    if flags:
        text += " · " + "; ".join(flags)
    return text


def probe_text(p: LawProbe) -> list[str]:
    if p.status == "holds":
        return [f"holds on today's code ({p.examples_tried} examples tried)"]
    if p.status == "fails":
        lines = [
            "FAILS on today's code: either the code has a bug or the law is wrong — you decide."
        ]
        if p.failing:
            ex = p.failing[0]
            inputs = ", ".join(f"{k}={v}" for k, v in ex.inputs.items()) or "(no inputs)"
            res = f" → {ex.result}" if ex.result else ""
            lines.append(f"example: {inputs}{res}")
        if p.message:
            lines.append(f"why: {p.message}")
        return lines
    if p.status == "error":
        return [f"DOES NOT RUN: {p.message or 'unknown problem'}"]
    return [f"not run: {p.message or 'lawkeep could not run this law outside pytest'}"]


def run(args: argparse.Namespace) -> int:
    root = paths.require_root(args.root)
    cfg = config.load_config(root)
    if not args.module:
        raise SetupError(
            "Say which module to draft laws for, e.g. `lawkeep draft billing.refunds "
            '--request "refunds never exceed the payment"`.'
        )
    files, import_name = resolve_module(root, cfg, args.module)
    api = "\n\n".join(
        public_api(
            f, _import_name(f, cfg.project.source_dirs(root)) if len(files) > 1 else import_name
        )
        for f in files
    )
    docs: list[tuple[str, str]] = []
    total = 0
    doc_args: list[str] = args.docs or []
    for d in doc_args:
        p = Path(d) if Path(d).is_absolute() else (Path.cwd() / d)
        if not p.is_file():
            p = root / d
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SetupError(f"Cannot read the document {d}: {exc}") from None
        text = text[:MAX_DOC_CHARS]
        if total + len(text) > MAX_DOCS_TOTAL:
            text = text[: max(0, MAX_DOCS_TOTAL - total)]
        total += len(text)
        docs.append((d, text))
    existing = [(i.law_id, i.sentence) for i in discovery.discover_laws(root).laws]
    prompt = build_prompt(
        request=args.request,
        import_name=import_name,
        api=api,
        docs=docs,
        existing=existing,
        has_strategies=paths.strategies_path(root).is_file(),
    )
    actor = args.actor or (f"agent:{cfg.agent.name}" if cfg.agent.name else "agent:other")
    notes = [f"note: {w}" for w in cfg.warnings]

    if args.dry_run:
        report = SimpleReport(
            kind="draft",
            exit_code=exitcodes.ALL_HOLD,
            summary="Dry run: this is the prompt lawkeep would send to your coding agent.",
            lines=[*notes, prompt],
            data={
                "prompt": prompt,
                "module": import_name,
                "files": [paths.rel(root, f) for f in files],
            },
        )
        text = None
        if not args.json:
            # the prompt is printed as it is (not wrapped), so it can be copied
            head = ui.render(
                ui.banner(
                    "DRY RUN",
                    "This is the message lawkeep would send to your coding agent. Nothing was "
                    "started and no file was written.",
                    tone="info",
                ),
                *[ui.status("info", n.removeprefix("note: ")) for n in notes],
            )
            text = (
                head
                + "\n"
                + prompt.rstrip()
                + "\n\n"
                + ui.render(
                    ui.next_step("run the same command without --dry-run to start your agent.")
                )
            )
        output.emit(report, args.json, text=text)
        return report.exit_code

    # raises ConfigError (3) when no agent is set up
    agent_launcher.build_argv(cfg.agent, "probe", "prompt.md")
    lock_was_clean = lock.read_lock(root) is not None and lock.diff_against_lock(root).is_empty
    laws_before = _law_files_snapshot(root)
    drafts_before = _drafts_snapshot(root)
    source_before = _source_snapshot(root, cfg)
    paths.drafts_dir(root).mkdir(parents=True, exist_ok=True)

    launched = agent_launcher.launch(cfg.agent, prompt, cwd=root, timeout=args.timeout)

    lines = list(notes)
    change_notes, waiting = _handle_law_file_changes(
        root,
        cfg,
        laws_before,
        actor=actor,
        request=args.request,
        lock_was_clean=lock_was_clean,
        agent_output="\n".join(x for x in (launched.stdout, launched.stderr) if x),
    )
    lines += [f"note: {n}" for n in change_notes]
    lines += [ln for w in waiting for ln in waiting_lines(w)]
    waiting_data = [w.to_dict() for w in waiting]
    source_after = _source_snapshot(root, cfg)
    touched_code = sorted(
        p
        for p in set(source_before) | set(source_after)
        if source_before.get(p) != source_after.get(p)
    )
    if touched_code:
        lines.append(
            "WARNING: the drafting agent also changed code, which it was told not to do: "
            + ", ".join(touched_code[:10])
            + ". lawkeep did not undo this; look at it with `git diff`."
        )
    drafts_after = _drafts_snapshot(root)
    new_drafts = sorted(p for p, h in drafts_after.items() if drafts_before.get(p) != h)
    agent_tail = (launched.stderr or launched.stdout or "").strip()[-2000:]
    if not new_drafts and waiting:
        report = SimpleReport(
            kind="draft",
            exit_code=exitcodes.WAITING,
            summary=(
                "Your coding agent wrote no new drafts, but it changed approved laws or "
                f"settings: {len(waiting)} change(s) wait for your review (`lawkeep approve`)."
            ),
            lines=lines,
            data={
                "module": import_name,
                "drafts": [],
                "files": [],
                "waiting_changes": waiting_data,
                "agent": launched.to_dict(),
                "agent_output": agent_tail,
            },
            hint="Review them with `lawkeep approve`, then approve or reject each one.",
        )
        text = None
        if not args.json:
            text = ui.render(
                ui.banner(
                    "WAITING FOR YOUR REVIEW",
                    "Your coding agent wrote no new draft law, but it changed approved laws or "
                    "settings. Nothing was applied: the approved versions are back in place.",
                    tone="wait",
                ),
                *[
                    ui.warning(w.removeprefix("WARNING: "))
                    for w in lines
                    if w.startswith("WARNING:")
                ],
                *[
                    ui.status("info", ln.removeprefix("note: "))
                    for ln in lines
                    if ln.startswith("note: ")
                ],
                *waiting_section(waiting),
                ui.blank(),
                ui.next_step(
                    "run `lawkeep approve` in a terminal to read each change and say yes or no."
                ),
            )
        output.emit(report, args.json, text=text)
        return report.exit_code
    if not new_drafts:
        if not launched.ok:
            raise SetupError(
                f"Your coding agent stopped with an error (exit code {launched.returncode}) "
                "and wrote no drafts.",
                hint=agent_tail[-500:] or "Run the agent by hand to see what went wrong.",
            )
        report = SimpleReport(
            kind="draft",
            exit_code=exitcodes.SETUP_PROBLEM,
            summary="Your coding agent finished but wrote no drafts in laws/drafts/.",
            lines=lines,
            data={"agent": launched.to_dict(), "agent_output": agent_tail},
            hint="Read the agent's message (in --json: data.agent_output); it may explain why.",
        )
        text = None
        if not args.json:
            text = ui.render(
                ui.banner(
                    "NO DRAFTS",
                    "Your coding agent finished but did not write any draft law in laws/drafts/.",
                    tone="problem",
                ),
                *[ui.status("info", ln.removeprefix("note: ")) for ln in lines],
                ui.heading("The agent's last words") if agent_tail else None,
                ui.dim(agent_tail[-1200:]) if agent_tail else None,
                ui.next_step(
                    "read the agent's message above; it may say why. Then try again with a "
                    "clearer --request."
                ),
            )
        output.emit(report, args.json, text=text)
        return report.exit_code

    side = read_sidecar(root)
    drafted: list[dict[str, Any]] = []
    probes: list[tuple[str, Any, LawProbe]] = []
    for relp in new_drafts:
        side["files"][relp] = {
            "actor": actor,
            "request": args.request,
            "module": import_name,
            "created_at": utc_now_iso(),
        }
        found = discovery.laws_in_file(root / relp, root)
        for problem in found.problems:
            lines.append(f"problem: {problem}")
        if not found.laws:
            lines.append(f"problem: {relp} has no @law in it, so it is not a draft law.")
        for info in found.laws:
            probes.append((relp, info, probe_law(root, cfg, root / relp, info.law_id)))
    write_sidecar(root, side)
    qc, qc_note = _quick_check(root, cfg, [info.law_id for _, info, _ in probes])

    from lawkeep import explain
    from lawkeep.strength import result_for

    human_notes = [ln for ln in lines if ln.startswith("note: ")]
    human_warnings = [ln for ln in lines if ln.startswith(("WARNING:", "problem:"))]
    panels: list[RenderableType] = []
    try:
        approved = discovery.discover_laws(root).laws
    except Exception:  # noqa: BLE001 - only used to spot duplicates
        approved = []
    codes = [exitcodes.WAITING]
    for relp, info, p in probes:
        qresult = result_for(qc, info.law_id, relp)
        qtext = quick_check_text(qresult)
        flagged = bool(getattr(qresult, "flags", None))
        lines.append(f'DRAFT {info.law_id} "{info.sentence or ""}" ({relp})')
        lines += [f"  {t}" for t in probe_text(p)]
        if qtext:
            lines.append(f"  {qtext}")
        if p.rows:
            lines.append(render_rows(p.rows))
        if p.status in ("fails", "error") or flagged:
            codes.append(exitcodes.SOME_FAIL)
        panels.append(
            explain.draft_law_panel(
                info,
                probe=p,
                strength=explain.strength_status(
                    qresult,
                    missing=qc_note.rstrip(".") if qc_note else "the strength check did not run",
                ),
                descriptions=discovery.describe_inputs_in_file(root / relp, info.qualname),
                similar=explain.similar_law(info.sentence, approved),
                title=f"Draft law {len(panels) + 1} of {len(probes)}"
                if len(probes) > 1
                else "Draft law",
                file=relp,
            )
        )
        drafted.append(
            {
                "law_id": info.law_id,
                "sentence": info.sentence,
                "file": relp,
                "probe": p.to_dict(),
                "quick_check": qc[info.law_id].to_dict() if info.law_id in qc else None,
                "quick_check_text": qtext,
            }
        )
    if qc_note:
        lines.append(f"note: {qc_note}")
    if not probes:
        codes.append(exitcodes.SOME_FAIL)
    code = exitcodes.combine(*codes)
    n = len(probes)
    summary = (
        f"{n} draft law(s) written to laws/drafts/ and waiting for your decision "
        "(`lawkeep approve`)."
    )
    if waiting:
        summary += (
            f" The drafting AI also changed approved laws or settings: {len(waiting)} "
            "change(s) wait for your review."
        )
    if code == exitcodes.SOME_FAIL:
        summary += " Some drafts fail on today's code, do not run, or look weak: see above."
    report = SimpleReport(
        kind="draft",
        exit_code=code,
        summary=summary,
        lines=lines,
        data={
            "module": import_name,
            "drafts": drafted,
            "files": new_drafts,
            "waiting_changes": waiting_data,
            "agent": launched.to_dict(),
            "agent_output": agent_tail,
        },
        hint="Review them with `lawkeep approve` (sentence + examples), then approve or reject.",
    )
    text = None
    if not args.json:
        n_weak = sum(1 for d in drafted if d["quick_check"] and d["quick_check"].get("flags"))
        n_bad = sum(1 for _, _, p in probes if p.status in ("fails", "error"))
        detail = (
            f"Your coding agent wrote {explain_count(n)}. They are not checked until you "
            "approve them."
        )
        worries: list[str] = []
        if n_bad:
            worries.append(
                f"{ui.plural(n_bad, 'draft breaks', 'drafts break')} or does not run with "
                "your current code"
            )
        if n_weak:
            worries.append(f"{ui.plural(n_weak, 'draft looks', 'drafts look')} weak")
        if worries:
            detail += " Look closely: " + " and ".join(worries) + "."
        if waiting:
            detail += (
                f" The drafting AI also changed approved laws or settings "
                f"({ui.plural(len(waiting), 'change')}); nothing of that was applied."
            )
        text = ui.render(
            ui.banner(
                "DRAFTS WAITING FOR YOUR DECISION",
                detail,
                tone="wait",
            ),
            *[
                ui.warning(w.removeprefix("WARNING: ").removeprefix("problem: "))
                for w in human_warnings
            ],
            *[ui.status("info", ln.removeprefix("note: ")) for ln in human_notes],
            ui.status("info", qc_note) if qc_note else None,
            *[x for pnl in panels for x in (ui.blank(), pnl)],
            *waiting_section(waiting),
            ui.blank(),
            ui.next_step(
                "run `lawkeep approve` in a terminal to read each draft"
                + (" and each change" if waiting else "")
                + " and say yes or no."
            ),
        )
    output.emit(report, args.json, text=text)
    return report.exit_code


def waiting_lines(w: draft_edits.WaitingEdit) -> list[str]:
    """Plain lines (JSON ``lines``) for one drafting edit that waits for review."""
    from lawkeep import explain

    out = [
        f"WAITING {w.history_id}: the drafting agent changed {w.file}; lawkeep put the "
        f"approved version back and made the change a proposal "
        f"({(w.direction or 'unclear').upper()}, needs your review)"
    ]
    if w.kind == "law":
        out.append(
            f'  before: "{w.sentence_before}"' if w.sentence_before else "  before: (no law)"
        )
        out.append(f"  after:  {_after_text(w)}")
    out += [f"  {c}" for c in w.changes]
    if w.explanation:
        out.append(f"  what would change: {explain.plain_explanation(w.explanation)}")
    return out


def _after_text(w: draft_edits.WaitingEdit) -> str:
    if w.deleted:
        return "(deleted)"
    if w.action == "skip_law":
        return "(skipped: lawkeep would stop checking it)"
    if w.sentence_after and w.sentence_after == w.sentence_before:
        return "the same sentence, but what the law checks changes"
    return f"“{w.sentence_after}”" if w.sentence_after else "-"


def waiting_section(waiting: list[draft_edits.WaitingEdit]) -> list[RenderableType]:
    """The "also changed approved laws/settings" part of the draft screen."""
    from lawkeep import explain

    if not waiting:
        return []
    out: list[RenderableType] = [
        ui.blank(),
        ui.heading(
            "The drafting AI also changed approved laws or settings — waiting for your review"
        ),
        ui.dim(
            "It was asked to write drafts only. lawkeep put the approved versions back (so "
            "`lawkeep check` still checks them) and kept each change as a proposal. Nothing "
            "goes in until you say yes, not even a stricter change."
        ),
    ]
    for k, w in enumerate(waiting, 1):
        rows: list[tuple[str, str | None]]
        if w.kind == "law":
            rows = [
                ("Law now:", f"“{w.sentence_before}”" if w.sentence_before else "(no law yet)"),
                ("Would become:", _after_text(w)),
            ]
            if len(w.laws) > 1:
                rows.append(("Laws touched:", ", ".join(w.laws)))
        else:
            what = {
                "settings": "Settings (lawkeep.toml)",
                "generators": "Input generators (laws/strategies.py)",
            }.get(w.kind, w.file)
            rows = [("What:", what), ("Changes:", "; ".join(w.changes) or "-")]
        rows += [
            ("Why:", w.reason),
            ("What changes:", explain.plain_explanation(w.explanation) if w.explanation else None),
        ]
        body: list[RenderableType] = [
            ui.status(
                "ok" if w.direction in ("stricter", "same") else "warn",
                explain.direction_text(w.direction),
            ),
        ]
        if not w.new_version_runs:
            body.append(
                ui.warning("The changed law does not run; approving it would fail the gate.")
            )
        body += [ui.blank(), ui.label_value(rows)]
        if w.examples:
            body += [
                ui.blank(),
                ui.bold("Examples: does each version of the law accept what the code did?"),
                explain.examples_table(w.examples, proposal=True),
            ]
        body.append(ui.ident(f"file {w.file} · decision id {w.history_id}"))
        title = "Change to an approved file" + (
            f" {k} of {len(waiting)}" if len(waiting) > 1 else ""
        )
        out += [ui.blank(), ui.panel(*body, title=title, tone="warn")]
    return out


def explain_count(n: int) -> str:
    return "1 draft law" if n == 1 else f"{n} draft laws"
