"""Terminal presentation (lawkeep.ui, lawkeep.explain): fits narrow terminals, plain mode has
no ANSI codes, --json is untouched. Wording rules: docs/OUTPUT-STYLE.md."""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

import pytest

from lawkeep import cli, discovery, explain, history, ui
from lawkeep.commands import check as check_cmd
from lawkeep.examples_table import LawProbe
from lawkeep.models import (
    CheckReport,
    ExampleRow,
    IntegrityIssue,
    LawInfo,
    LawResult,
    QuickCheckOutcome,
    StrengthReport,
    StrengthResult,
)
from lawkeep.output import setup_problem_text
from lawkeep.strength.render import strength_text

ANSI = re.compile(r"\x1b\[")
FIXTURES = Path(__file__).parent / "fixtures" / "gate"
BOX = "|+-"

STRONG = "The SAVE10 code takes exactly 10% off the price, rounded to the nearest cent"
WEAK = "A discounted price is never negative"


# ------------------------------------------------------------------------------------- helpers


class _Tty(io.StringIO):
    def isatty(self) -> bool:  # pragma: no cover - trivial
        return True


def _clean_env(monkeypatch) -> None:
    for var in ("NO_COLOR", "CI", "LAWKEEP_COLOR", "TERM", "COLUMNS"):
        monkeypatch.delenv(var, raising=False)


def _flat(text: str) -> str:
    """Text with box borders removed and whitespace collapsed: a sentence wrapped only at
    spaces is still found as one piece; a word broken in two is not."""
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s and set(s) <= set(BOX + " "):
            continue
        s = s.strip("|").strip()
        lines.append(s)
    return " ".join(" ".join(lines).split())


def _assert_fits(text: str, width: int) -> None:
    too_long = [ln for ln in text.splitlines() if len(ln) > width]
    assert not too_long, f"lines wider than {width}: {too_long}"
    assert not ANSI.search(text)


def _probe(status: str = "holds") -> LawProbe:
    rows = [
        ExampleRow(
            inputs={"price": "10.00"},
            result="apply_discount() → 9.00",
            holds=True,
            note="example written in the law",
        ),
        ExampleRow(inputs={"price": "0.00"}, result="apply_discount() → 0.00", holds=True),
    ]
    return LawProbe(law_id="d.x", sentence="s", status=status, examples_tried=46, rows=rows)


def _strength(caught: int, total: int, flags: list[str] | None = None) -> StrengthResult:
    outcomes = [
        QuickCheckOutcome(function="shop:f", variant=f"v{i}", caught=i < caught)
        for i in range(total)
    ]
    return StrengthResult(law_id="d.x", quick_check=outcomes, flags=flags or [])


def _approve_screen(width: int) -> str:
    strong = explain.draft_law_panel(
        LawInfo(law_id="discounts_save10.save10", sentence=STRONG, file="f", qualname="q"),
        probe=_probe(),
        strength=explain.strength_status(_strength(3, 3)),
        descriptions={"price": "the price before the discount, in dollars"},
        title="Draft law 1 of 2",
        file="laws/drafts/discounts_save10.py",
    )
    weak = explain.draft_law_panel(
        LawInfo(law_id="discounts_low.never_negative", sentence=WEAK, file="f", qualname="q"),
        probe=_probe(),
        strength=explain.strength_status(
            _strength(
                2,
                5,
                [
                    "One-sided: this law still holds when apply_discount() always returns 0 "
                    "(also when apply_discount() always returns Decimal('0')). It checks a "
                    "limit, not the answer."
                ],
            )
        ),
        descriptions=discovery.describe_inputs(
            "@law('x')\n@given(price=st.money(0, 1000))\ndef q(price):\n    pass\n", "q"
        ),
        title="Draft law 2 of 2",
    )
    return ui.render(strong, weak, ui.next_step("run `lawkeep approve`."), width=width)


# --------------------------------------------------------------------------------- the console


def test_plain_mode_when_not_a_terminal(monkeypatch):
    _clean_env(monkeypatch)
    assert ui.plain_wanted(io.StringIO()) is True
    assert ui.plain_wanted(_Tty()) is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.plain_wanted(_Tty()) is True
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("CI", "true")
    assert ui.plain_wanted(_Tty()) is True
    monkeypatch.setenv("LAWKEEP_COLOR", "always")
    assert ui.plain_wanted(io.StringIO()) is False


def test_colour_only_on_a_terminal(monkeypatch):
    _clean_env(monkeypatch)
    banner = ui.banner("ALL LAWS HOLD", "All 3 of your laws held.", tone="ok")
    assert not ANSI.search(ui.render(banner, stream=io.StringIO()))
    monkeypatch.setenv("TERM", "xterm-256color")
    coloured = ui.render(banner, stream=_Tty(), width=80)
    assert ANSI.search(coloured) and "ALL LAWS HOLD" in ui.strip_ansi(coloured)
    monkeypatch.setenv("NO_COLOR", "1")
    assert not ANSI.search(ui.render(banner, stream=_Tty(), width=80))


def test_plain_mode_uses_ascii_boxes_and_marks():
    text = ui.render(
        ui.banner("SOME LAWS FAIL", "1 of 3 laws broke.", tone="fail"),
        ui.status("ok", "works"),
        ui.warning("weak"),
        ui.next_step("fix the code."),
        plain=True,
        width=60,
    )
    assert "+---" in text and "| SOME LAWS FAIL" in text
    assert "+ works" in text and "! weak" in text and "Next step: Fix the code." in text
    assert all(ord(ch) < 128 for ch in text.replace("—", "").replace("“", "").replace("”", ""))


# ------------------------------------------------------------------------ key screens fit / words


@pytest.mark.parametrize("width", [80, 60])
def test_approve_screen_fits_and_never_breaks_words(width):
    text = _approve_screen(width)
    _assert_fits(text, width)
    flat = _flat(text)
    assert f"“{STRONG}”" in flat and f"“{WEAK}”" in flat
    assert (
        "Works with your current code — lawkeep tried 46 different inputs and the law was "
        "never broken." in flat
    )
    assert (
        "Strong law — lawkeep made 3 deliberately broken versions of the code and this law "
        "caught all 3." in flat
    )
    assert (
        "Weak law — lawkeep made 5 deliberately broken versions of the code and this law "
        "caught only 2." in flat
    )
    assert "Recommended: approve." in flat
    assert "Think twice: this law is weak" in flat
    assert "price: the price before the discount, in dollars" in flat
    assert "price: a sample amount of money that lawkeep made up for testing" in flat
    assert "Tip: the law can describe its inputs in its own words" in flat
    # the technical label and the noisy Decimal text are gone
    assert "Decimal(" not in text and "Vacuous" not in text and "dumb" not in text
    # table cells keep their words whole
    for word in ("apply_discount()", "gave", "Follows", "Input"):
        assert word in text.split() or word in flat.split()


@pytest.mark.parametrize("width", [80, 60])
def test_check_failure_screen_fits(width, monkeypatch):
    report = CheckReport(
        profile="pr",
        root="/tmp/x",
        laws=[
            LawResult(
                law_id="refunds.refund_is_capped",
                sentence="A refund never exceeds the original charge",
                status="fails",
                examples_tried=12,
                simplest_failing_example={"charge": "40", "amount": "Decimal('50')"},
                failure_message="AssertionError: assert 50 <= 40\n +  where 50 = refund(40, 50)",
            ),
            LawResult(
                law_id="refunds.never_negative",
                sentence="A refund is never negative",
                status="holds",
                examples_tried=200,
            ),
        ],
        integrity=[
            IntegrityIssue(kind="source_side_door", message="note one", severity="warning"),
            IntegrityIssue(kind="source_side_door", message="note two", severity="warning"),
        ],
    ).finalize()
    report.summary = check_cmd._summary(report)
    _clean_env(monkeypatch)
    monkeypatch.setenv("COLUMNS", str(width))  # what a terminal of that width reports
    text = check_cmd.render(report, "lawkeep tried up to 200 inputs per law")
    _assert_fits(text, width)
    flat = _flat(text)
    assert text.lstrip().startswith("+") and "SOME LAWS FAIL" in text.splitlines()[1]
    assert "“A refund never exceeds the original charge”" in flat
    assert "The input that breaks it: charge: $40 amount: $50" in flat
    assert "The law expected at most 40, but the code gave 50." in flat
    assert "where 50" not in text  # pytest's long explanation only with --details
    assert "2 notes about your project worth a look" in flat
    assert "note one" not in text  # summarised, not listed
    assert "Next step: Fix the code" in flat


@pytest.mark.parametrize("width", [80, 60])
def test_strength_and_setup_screens_fit(width, monkeypatch):
    monkeypatch.setenv("COLUMNS", str(width))
    rep = StrengthReport(
        laws=[
            StrengthResult(
                law_id="r.capped",
                sentence="A refund never exceeds the original charge",
                quick_check=[QuickCheckOutcome("shop:refund", "zero", False)],
                flags=["One-sided: this law still holds when refund() always returns 0."],
            )
        ],
        uncovered_functions=["shop.refunds:fee"],
    )
    text = strength_text(rep)
    _assert_fits(text, width)
    assert "SOME LAWS ARE WEAK" in text and "WEAK" in text
    assert "Functions no law checks (1)" in _flat(text)
    problem = setup_problem_text("check", "lawkeep.toml has a bad value.", "Fix line 3.")
    _assert_fits(problem, width)
    assert "SETUP PROBLEM" in problem and "Next step: Fix line 3." in _flat(problem)


# ------------------------------------------------------------------------------- end to end


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    shutil.copytree(FIXTURES / "refunds_project", root)
    (root / "laws" / "history.jsonl").touch()
    (root / "laws" / "failing_examples.jsonl").touch()
    history.record_change(root, action="init", actor="person:tester", direction="stricter")
    return root


def test_check_text_is_plain_and_json_is_unchanged(tmp_path, capsys, monkeypatch):
    _clean_env(monkeypatch)
    root = _project(tmp_path)
    code = cli.main(["check", "--root", str(root)])
    text = capsys.readouterr().out
    assert code == 0 and not ANSI.search(text)
    assert "ALL LAWS HOLD" in text and "Next step:" in text
    _assert_fits(text, 80)

    # JSON: same keys and summary format as before; no colour even when colour is forced
    monkeypatch.setenv("LAWKEEP_COLOR", "always")
    code = cli.main(["check", "--json", "--root", str(root)])
    raw = capsys.readouterr().out
    assert code == 0 and not ANSI.search(raw)
    data = json.loads(raw)
    assert list(data) == [
        "kind",
        "schema_version",
        "exit_code",
        "summary",
        "lawkeep_version",
        "profile",
        "root",
        "laws",
        "integrity",
        "waiting",
        "notes",
        "started_at",
        "duration_seconds",
        "only_laws",
        "laws_known",
        # additive (2026-11-23, one waiting list): appended, nothing before them changed
        "waiting_for_person",
        "person_present",
        "person_present_reason",
    ]
    assert data["summary"] == "ALL LAWS HOLD (3 of 3)"
    assert data["waiting_for_person"]["count"] == 0
    assert set(data["laws"][0]) == {
        "law_id",
        "sentence",
        "status",
        "level",
        "examples_tried",
        "simplest_failing_example",
        "failure_message",
        "duration_seconds",
        "evidence",
        "file",
        "nodeid",
    }


def test_approve_json_keys_unchanged(tmp_path, capsys, monkeypatch):
    _clean_env(monkeypatch)
    root = _project(tmp_path)
    code = cli.main(["approve", "--json", "--root", str(root)])
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert list(data) == [
        "kind",
        "schema_version",
        "exit_code",
        "summary",
        "lawkeep_version",
        "lines",
        "data",
        "hint",
    ]
    assert data["summary"] == "Nothing is waiting for your decision."
    # "waiting_for_person" is additive (2026-11-23, one waiting list)
    assert set(data["data"]) == {"waiting", "decided", "problems", "waiting_for_person"}
    code = cli.main(["approve", "--root", str(root)])
    text = capsys.readouterr().out
    assert "NOTHING TO DECIDE" in text and "Next step:" in text and not ANSI.search(text)


def test_setup_problem_text_and_json(tmp_path, capsys):
    (tmp_path / "lawkeep.toml").write_text("[check]\nprofile = 5\n", encoding="utf-8")
    code = cli.main(["check", "--root", str(tmp_path)])
    text = capsys.readouterr().out
    assert code == 3 and "SETUP PROBLEM" in text and "Next step:" in text
    code = cli.main(["check", "--json", "--root", str(tmp_path)])
    data = json.loads(capsys.readouterr().out)
    assert code == 3 and data["summary"].startswith("SETUP PROBLEM: ")
    assert list(data)[:5] == ["kind", "schema_version", "exit_code", "summary", "lawkeep_version"]


# ------------------------------------------------------------------------------ explanations


def test_strength_wording():
    assert explain.strength_status(_strength(3, 3)).text.startswith(
        "Strong law — lawkeep made 3 deliberately broken versions of the code and this law "
        "caught all 3."
    )
    weak = explain.strength_status(_strength(0, 4))
    assert weak.weak and "caught none of them" in weak.text
    assert not explain.strength_status(None, missing="it is switched off").measured


def test_probe_wording():
    assert explain.probe_status(_probe())[1] == (
        "Works with your current code — lawkeep tried 46 different inputs and the law was "
        "never broken."
    )
    assert explain.probe_status(_probe("fails"))[0] == "fail"


def test_similar_law_is_spotted():
    approved = [
        LawInfo(
            law_id="r.capped",
            sentence="A refund never exceeds the original payment",
            file="f",
            qualname="q",
        )
    ]
    assert explain.similar_law("A refund never exceeds the original payment.", approved)
    assert explain.similar_law("Discounts never go below zero", approved) is None


def test_draft_prompt_asks_for_input_descriptions():
    from lawkeep.commands.draft import build_prompt

    prompt = build_prompt(
        request="refunds never exceed the payment",
        import_name="shop.refunds",
        api="def refund(charge, amount): ...",
        docs=[],
        existing=[],
        has_strategies=True,
    )
    assert 'inputs={"<input name>": "<what this input is, in plain words>", ...}' in prompt
    assert "ALWAYS fill in `inputs=`" in prompt


def test_failure_reasons_are_said_in_words():
    words = explain.comparison_in_words
    assert words("0.26 == 0.25") == "The law expected 0.25, but the code gave 0.26."
    assert words("3 >= 5") == "The law expected at least 5, but the code gave 3."
    assert words("7 < 2") == "The law expected less than 2, but the code gave 7."
    assert words("1 > 2") == "The law expected more than 2, but the code gave 1."
    assert words("0 != 0") == "The law expected something other than 0, but the code gave 0."
    assert words("0 <= x <= 3") == "This check was false: 0 <= x <= 3"  # cannot translate
    assert explain.failure_lines("AssertionError: assert Decimal('0.26') == Decimal('0.25')") == [
        "The law expected 0.25, but the code gave 0.26."
    ]


def test_inputs_without_description_get_plain_words():
    made_up = discovery.made_up_description
    import ast

    call = ast.parse("st.charges()", mode="eval").body
    assert made_up("charge", call) == "a sample charge that lawkeep made up for testing"
    call = ast.parse("st.refund_requests()", mode="eval").body
    assert made_up("request", call) == "a sample refund request that lawkeep made up for testing"
    call = ast.parse("st.sampled_from([0, 1])", mode="eval").body
    assert "sample code picked from a fixed list" in made_up("codes", call)


# --------------------------------------------------------------------------- readable values


def test_readable_values_in_words():
    from lawkeep import readable

    d = readable.describe
    assert d("charge", "charge(price=Decimal('0.25'), discount_code=None)") == (
        "a charge of $0.25 with no discount code"
    )
    assert d("charge", "Charge(id='ch-1', total=40.00, card_fee=0.80)") == (
        "a charge with id ch-1, total $40.00, card fee $0.80"
    )
    assert d("request", "(charge(price=Decimal('0.25'), discount_code=None), Decimal('0.13'))") == (
        "part 1: a charge of $0.25 with no discount code; part 2: 0.13"
    )
    assert d("code", "None") == "none" and d("code", "'SAVE25'") == "SAVE25"
    assert d("count", "3") == "3" and d("price", "3") == "$3"  # money only for money names
    assert d("slot", "Slot(start=datetime(2030, 1, 7, 9, 0), end=datetime(2030, 1, 7, 9, 15))") == (
        "a slot with start 2030-01-07 09:00, end 2030-01-07 09:15"
    )
    # unsure -> None, so the caller shows the raw text
    assert d("slot", "Slot(OPENING, OPENING + STEP)") is None
    assert d("x", "(lambda a: a)(3)") is None
    assert readable.inline({"price": "16.47", "code": "'SAVE25'"}) == "price $16.47, code SAVE25"
    assert readable.inline({"price": "8.00", "code": "None"}) == "price $8.00, no code"
    assert readable.inline({"slot": "Slot(OPENING, OPENING)"}) == "slot=Slot(OPENING, OPENING)"


@pytest.mark.parametrize("width", [80, 60])
def test_example_table_is_one_line_per_example(width):
    rows = [
        ExampleRow(
            inputs={"price": "16.47", "code": "'SAVE25'"},
            result="apply_discount() → 12.35",
            holds=True,
        ),
        ExampleRow(
            inputs={"price": "0.00", "code": "None"},
            result="apply_discount() → 0.00",
            holds=True,
            note="edge case: zero",
        ),
        ExampleRow(
            inputs={"price": "8.00", "code": "None"},
            result="apply_discount() → 8.00",
            holds=True,
            note="edge case: nothing (None)",
        ),
    ]
    text = ui.render(explain.examples_table(rows), width=width, plain=True)
    _assert_fits(text, width)
    if width >= 80:
        assert "price $16.47, code SAVE25" in text  # both inputs on one line
    assert "price $16.47," in text and "SAVE25" in text
    assert "Note" in text and "zero" in text
    assert "no value" not in text and "nothing (None)" not in text  # meaningless note dropped
    assert "Decimal" not in text


def test_long_ids_wrap_at_dots_not_mid_word():
    law_id = "discounts_not_negative.discounted_price_is_never_negative"
    text = ui.render(ui.ident(f"law id {law_id} · file laws/drafts/x.py"), width=40, plain=True)
    _assert_fits(text, 40)
    pieces = text.split()
    assert "discounts_not_negative." in pieces or any(p.endswith(".") for p in pieces)
    assert "".join(text.split()).replace("lawid", "").startswith(law_id.replace(" ", ""))
    for piece in pieces:  # every break is after a separator or between words
        if piece not in ("law", "id", "·", "file"):
            assert piece in f"law id {law_id} · file laws/drafts/x.py"


def test_one_sided_law_is_fine_when_a_companion_catches_the_rest():
    from lawkeep.strength.core import _credit_companions

    bound = StrengthResult(
        law_id="refunds.capped",
        quick_check=[
            QuickCheckOutcome("shop:refund", "zero", False),
            QuickCheckOutcome("shop:refund", "raises", True),
        ],
        flags=["One-sided: this law still holds when refund() always returns 0."],
    )
    exact = StrengthResult(
        law_id="refunds.exact", quick_check=[QuickCheckOutcome("shop:refund", "zero", True)]
    )
    _credit_companions([bound, exact], {"refunds.capped": list(bound.flags)}, set())
    assert bound.flags == [] and "refunds.exact" in bound.weak_data[0]
    # a draft never counts as the companion
    bound.flags = ["One-sided: this law still holds when refund() always returns 0."]
    bound.weak_data = []
    _credit_companions([bound, exact], {"refunds.capped": list(bound.flags)}, {"refunds.exact"})
    assert bound.flags and not bound.weak_data
