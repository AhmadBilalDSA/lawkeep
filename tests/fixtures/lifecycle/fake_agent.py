"""A FAKE coding agent for lawkeep's tests. It never calls any AI.

Usage (as an [agent] command): python fake_agent.py {prompt_file}
or with the prompt on stdin. FAKE_AGENT_MODE picks what it "drafts":

- good     one draft that holds + an added generator in laws/strategies.py
- failing  one draft that fails on today's code
- naughty  one draft + edits an approved law + changes an existing generator
- edit_looser     one draft + loosens the approved law (and explains it on a "Changed" line)
- edit_stricter   one draft + makes the approved law stricter
- edit_settings   one draft + edits lawkeep.toml
- edit_generator  one draft + changes the existing generator percents()
- edit_only       no draft, only loosens the approved law
- delete_law      one draft + deletes the approved law file
- nothing  writes nothing (exit 0)
- crash    writes nothing (exit 1)
The prompt it received is saved to fake_agent_prompt.txt in the project folder.
"""

import os
import sys
from pathlib import Path

GOOD = """from lawkeep import given, law, st
from teashop.pricing import total


@law("The total price is never negative")
@given(price=st.money(max_value=50), quantity=st.cups())
def total_never_negative(price, quantity):
    assert total(price, quantity) >= 0
"""

STRICTER = """from teashop.pricing import discount

from lawkeep import given, law, st


@law("A discount never makes the price larger")
@given(amount=st.money(max_value=1000), percent=st.integers(min_value=0, max_value=100))
def discount_never_raises_price(amount, percent):
    result = discount(amount, percent)
    assert result <= amount
    assert result >= 0
"""

FAILING = """from lawkeep import example, given, law, st
from teashop.pricing import total


@law("The total price is never more than the price of one cup")
@given(price=st.money(max_value=50), quantity=st.integers(min_value=0, max_value=20))
@example(price=5, quantity=3)
def total_at_most_one_cup(price, quantity):
    assert total(price, quantity) <= price
"""


def main() -> int:
    mode = os.environ.get("FAKE_AGENT_MODE", "good")
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
    else:
        prompt = sys.stdin.read()
    root = Path.cwd()
    (root / "fake_agent_prompt.txt").write_text(prompt, encoding="utf-8")
    drafts = root / "laws" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    strategies = root / "laws" / "strategies.py"
    if mode == "crash":
        print("fake agent: something went wrong", file=sys.stderr)
        return 1
    if mode == "nothing":
        print("fake agent: I could not write a law for that.")
        return 0
    law = root / "laws" / "pricing.py"
    if mode in ("edit_looser", "edit_only", "naughty"):
        law.write_text(law.read_text(encoding="utf-8").replace("<= amount", "<= amount + 5"))
    if mode == "edit_looser":
        print("Changed laws/pricing.py: discounts may round up by a few cents")
    if mode == "edit_stricter":
        law.write_text(STRICTER, encoding="utf-8")
    if mode == "delete_law":
        law.unlink()
    if mode == "edit_settings":
        toml = root / "lawkeep.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8") + '\n[permissions]\nadd_law = "ask"\n',
            encoding="utf-8",
        )
    if mode == "edit_only":
        print("fake agent: I only changed the law")
        return 0
    if mode in ("good", "naughty") or mode.startswith(("edit_", "delete_")):
        (drafts / "pricing_total.py").write_text(GOOD, encoding="utf-8")
        text = strategies.read_text(encoding="utf-8")
        if mode in ("naughty", "edit_generator"):
            text = text.replace("max_value=100", "max_value=1")
        text += "\n\ndef cups():\n    return st.integers(min_value=0, max_value=20)\n"
        strategies.write_text(text, encoding="utf-8")
    if mode == "failing":
        (drafts / "pricing_one_cup.py").write_text(FAILING, encoding="utf-8")
    print("fake agent: wrote drafts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
