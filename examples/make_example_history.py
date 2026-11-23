"""Rebuild an example project's law history and laws.lock from scratch.

Usage (from the lawkeep repository root)::

    uv run python examples/make_example_history.py examples/billing
    uv run python examples/make_example_history.py examples/bookings

What it does, in the same order a real project would go through it:

1. ``init``: writes ``lawkeep.toml`` (the default settings with ``source = ["src"]``), keeps
   ``laws/strategies.py`` and creates empty ``laws/history.jsonl`` and
   ``laws/failing_examples.jsonl``, then records an ``init`` entry.
2. One ``add_law`` entry per law, in file order: each law is written into its file and recorded
   with ``history.record_change`` (drafted by a coding agent, approved by a person), the way
   ``lawkeep approve`` records an approved draft.

Every lock update goes through ``lawkeep.history.record_change``, so the integrity check sees a
consistent history (a hand-written ``laws.lock`` would be flagged as rewritten by hand).

Why a script and not ``lawkeep init`` / ``draft`` / ``approve``? Those commands were being built
in parallel when the examples were written. Once they work, this script can be replaced by
running them; the files it produces follow the same formats (docs/ARCHITECTURE.md, section 4).
The person is a fixed made-up name ("person:example-maintainer") so no real name ends up in
the examples.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from lawkeep import config, history, paths
from lawkeep.discovery import iter_python_files

PERSON = "person:example-maintainer"
AGENT = "agent:claude-code"

# The plain-English request behind each law (what the shop owner asked for).
REQUESTS = {
    "refund_never_exceeds_payment": "Refunds can never be more than the customer paid.",
    "full_refund_gives_back_exact_payment": (
        "A full refund gives back exactly what was paid, and we get the whole card fee back."
    ),
    "partial_refund_is_exact_and_fee_share_is_fair": (
        "Partial refunds give back exactly what we type in, and our card fee share is fair."
    ),
    "discount_stays_between_zero_and_price": (
        "Discount codes can never make something cost more, or less than nothing."
    ),
    "rounding_is_to_the_nearest_cent": "Money is always rounded to the nearest cent.",
    "bookings_never_overlap": "Two customers can never be booked into the same chair at once.",
    "free_future_slot_can_be_booked": "If a slot is free and not in the past, people can book it.",
    "cancelling_frees_the_slot": "When someone cancels, the slot opens up for others.",
    "bookings_are_never_in_the_past": "Nobody can book a time that has already passed.",
    # companion laws added on 2026-11-22 with `lawkeep propose` (see the examples' AGENTS.md)
    "discount_takes_off_exactly_what_it_promises": (
        "Pin down the exact amount each discount code takes off; the existing discount law only "
        "checks the limits."
    ),
    "slots_overlap_exactly_when_they_share_time": (
        "Pin down the exact answer; the existing booking laws only check limits."
    ),
    "free_means_no_shared_time": (
        "Pin down the exact answer; the existing booking laws only check limits."
    ),
}


def split_laws(source: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split a law file into (header, [(function name, sentence, block text)]).

    The blocks are cut at each top-level decorated function, so header + all blocks == source.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    starts = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.decorator_list:
            first = min(d.lineno for d in node.decorator_list) - 1
            sentence = ""
            for d in node.decorator_list:
                if (
                    isinstance(d, ast.Call)
                    and getattr(d.func, "id", "") == "law"
                    and d.args
                    and isinstance(d.args[0], ast.Constant)
                ):
                    sentence = d.args[0].value
            starts.append((first, node.name, sentence))
    if not starts:
        return source, []
    # Blank lines between blocks belong to the block before (so each prefix is a valid file).
    header = "".join(lines[: starts[0][0]])
    blocks = []
    for i, (first, name, sentence) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(lines)
        blocks.append((name, sentence, "".join(lines[first:end])))
    return header, blocks


def rebuild(root: Path) -> None:
    root = root.resolve()
    law_files = iter_python_files(root)
    originals = {p: p.read_text(encoding="utf-8") for p in law_files}

    for f in (paths.lock_path(root), paths.history_path(root), paths.ledger_path(root)):
        f.unlink(missing_ok=True)
    paths.config_path(root).write_text(
        config.render_default_toml(agent_name="claude-code", source=["src"]), encoding="utf-8"
    )
    paths.history_path(root).write_text("", encoding="utf-8")
    paths.ledger_path(root).write_text("", encoding="utf-8")
    for p in law_files:
        p.unlink()

    history.record_change(
        root,
        action="init",
        actor=PERSON,
        direction="stricter",
        reason="Set up lawkeep for this project.",
        details={"note": "recorded by examples/make_example_history.py"},
    )

    for p, text in originals.items():
        header, blocks = split_laws(text)
        written = header
        module = paths.rel(paths.laws_dir(root), p)[: -len(".py")].replace("/", ".")
        for name, sentence, block in blocks:
            written += block
            p.write_text(written, encoding="utf-8")
            history.record_change(
                root,
                action="add_law",
                actor=AGENT,
                law_id=f"{module}.{name}",
                direction="stricter",
                reason=REQUESTS.get(name, sentence),
                permission_used="ask",
                approved_by=PERSON,
                sentence_after=sentence,
                details={"note": "recorded by examples/make_example_history.py"},
            )
        if written != text:  # trailing text after the last law
            p.write_text(text, encoding="utf-8")
            history.record_change(
                root,
                action="change_law",
                actor=PERSON,
                direction="same",
                reason="Tidy the end of the file.",
                approved_by=PERSON,
            )
    print(f"Rebuilt {paths.rel(root.parent, paths.lock_path(root))} and the law history.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(3)
    rebuild(Path(sys.argv[1]))
