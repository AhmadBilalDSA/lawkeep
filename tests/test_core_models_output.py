"""Tests for lawkeep.models (to_dict/from_dict, finalize) and lawkeep.output (emit)."""

from __future__ import annotations

import io
import json

import pytest

from lawkeep import exitcodes, output
from lawkeep.models import (
    CheckReport,
    ExampleRow,
    HistoryEntry,
    IntegrityIssue,
    LawChange,
    LawChangeReport,
    LawInfo,
    LawResult,
    ProposalResult,
    ProtectionReport,
    ProtectionResult,
    QuickCheckOutcome,
    SimpleReport,
    StrengthReport,
    StrengthResult,
    parse_iso,
    utc_now_iso,
)


def _fail():
    return LawResult(
        law_id="refunds.capped",
        sentence="A refund never exceeds the charge",
        status="fails",
        examples_tried=17,
        simplest_failing_example={"charge": "40", "amount": "50"},
        failure_message="assert 50 <= 40\nmore",
        duration_seconds=0.12,
        evidence={"seed": 0, "backend": "hypothesis"},
    )


def test_check_report_round_trip_and_json():
    report = CheckReport(
        laws=[_fail(), LawResult(law_id="b", sentence="b", status="holds", examples_tried=200)],
        integrity=[
            IntegrityIssue(
                kind="side_door_file",
                message="conftest.py added",
                path="conftest.py",
                severity="warning",
            )
        ],
        waiting=[LawChange(change="waiting", law_id="fees.two", direction="looser")],
    ).finalize()
    d = report.to_dict()
    assert d["kind"] == "check" and d["schema_version"] == 1 and d["exit_code"] == 1
    assert d["laws"][0]["simplest_failing_example"] == {"charge": "40", "amount": "50"}
    json.dumps(d)
    back = CheckReport.from_dict(json.loads(json.dumps(d)))
    assert back == report
    assert isinstance(back.laws[0], LawResult) and isinstance(back.waiting[0], LawChange)


def test_finalize_codes():
    holds = LawResult(law_id="a", sentence="a", status="holds")
    assert CheckReport(laws=[holds]).finalize().exit_code == exitcodes.ALL_HOLD
    assert "1 of 1" in CheckReport(laws=[holds]).finalize().summary
    r = CheckReport().finalize()
    assert r.exit_code == 0 and "no laws yet" in r.summary
    err = LawResult(law_id="a", sentence="a", status="error")
    assert CheckReport(laws=[err]).finalize().exit_code == exitcodes.SOME_FAIL
    issue = IntegrityIssue(kind="unrecorded_law_change", message="m")
    r = CheckReport(laws=[holds], integrity=[issue]).finalize()
    assert r.exit_code == exitcodes.WAITING and r.verdict == "WAITING FOR YOUR DECISION"
    warn = IntegrityIssue(kind="side_door_file", message="m", severity="warning")
    assert CheckReport(laws=[holds], integrity=[warn]).finalize().exit_code == 0
    assert (
        CheckReport(laws=[_fail()], integrity=[issue]).finalize().exit_code == exitcodes.SOME_FAIL
    )


def test_check_report_text():
    report = CheckReport(laws=[_fail()]).finalize()
    text = report.to_text()
    assert "FAIL refunds.capped" in text
    assert "simplest failing example: charge=40, amount=50" in text
    assert "why: assert 50 <= 40" in text
    assert text.strip().endswith("SOME LAWS FAIL: 1 of 1 laws fail.")


def test_validation():
    with pytest.raises(ValueError, match="status"):
        LawResult(law_id="a", sentence=None, status="passed")
    with pytest.raises(ValueError, match="level"):
        LawResult(law_id="a", sentence=None, status="holds", level="L9")
    with pytest.raises(ValueError, match="severity"):
        IntegrityIssue(kind="x", message="m", severity="fatal")
    with pytest.raises(ValueError, match="direction"):
        HistoryEntry(action="add_law", actor="x", direction="up")
    with pytest.raises(ValueError, match="permission_used"):
        HistoryEntry(action="add_law", actor="x", permission_used="maybe")
    with pytest.raises(ValueError):
        HistoryEntry(action="", actor="x")
    with pytest.raises(ValueError, match="decision"):
        ProposalResult(decision="later")
    with pytest.raises(ValueError, match="status"):
        ProtectionResult(function="m:f", file="m.py", status="safe")


def test_all_reports_round_trip():
    reports = [
        StrengthReport(
            laws=[
                StrengthResult(
                    law_id="a",
                    quick_check=[QuickCheckOutcome("m:f", "always_zero", True)],
                    planted_bugs_total=4,
                    planted_bugs_caught=3,
                )
            ],
            uncovered_functions=["m:g"],
        ),
        ProtectionReport(
            base_ref="main",
            functions=[ProtectionResult(function="m:f", file="m.py", status="partly")],
        ),
        LawChangeReport(
            since=utc_now_iso(),
            changes=[
                LawChange(
                    change="added",
                    law_id="a",
                    examples=[ExampleRow(inputs={"x": "1"}, result="2", holds=True)],
                )
            ],
            protection=ProtectionReport(),
        ),
        ProposalResult(
            law_id="a",
            direction="looser",
            decision="waiting",
            exit_code=2,
            examples=[
                ExampleRow(
                    inputs={"refund": "$0.50"}, result="fee $0.00", holds=True, old_holds=False
                )
            ],
        ),
        SimpleReport(kind="init", lines=["Created laws/"], data={"files": ["laws/"]}),
    ]
    for r in reports:
        d = json.loads(json.dumps(r.to_dict()))
        assert {"kind", "schema_version", "exit_code", "summary", "lawkeep_version"} <= set(d)
        assert type(r).from_dict(d) == r
    assert reports[0].laws[0].mutation_score == 0.75
    assert reports[0].to_dict()["laws"][0]["mutation_score"] == 0.75
    assert StrengthResult(law_id="x").mutation_score is None
    assert reports[-1].kind == "init" and reports[0].kind == "strength"


def test_law_change_from_history():
    e = HistoryEntry(
        action="change_law",
        actor="agent:codex",
        law_id="fees.two",
        direction="looser",
        status="waiting",
        sentence_before="Fee is 2%",
        sentence_after="Fee is 2%, or $0 under $1",
        reason="no fees on tiny refunds",
        id="h-1",
    )
    row = LawChange.from_history(e)
    assert (row.change, row.sentence, row.sentence_before, row.history_id) == (
        "waiting",
        "Fee is 2%, or $0 under $1",
        "Fee is 2%",
        "h-1",
    )
    assert LawChange.from_history(HistoryEntry(action="add_law", actor="a")).change == "added"
    assert LawChange.from_history(HistoryEntry(action="remove_law", actor="a")).change == "removed"
    assert (
        LawChange.from_history(HistoryEntry(action="rename_target", actor="a")).change == "updated"
    )
    assert (
        LawChange.from_history(HistoryEntry(action="x", actor="a", status="rejected")).change
        == "rejected"
    )


def test_law_info_and_history_entry_dicts():
    info = LawInfo(law_id="a", sentence="s", file="laws/a.py", qualname="a")
    assert LawInfo.from_dict(info.to_dict()) == info
    e = HistoryEntry(
        action="add_law", actor="a", files={"laws/a.py": "sha256:1", "laws/b.py": None}
    )
    assert HistoryEntry.from_dict({**e.to_dict(), "unknown_future_key": 1}) == e


def test_timestamps():
    ts = utc_now_iso()
    assert ts.endswith("Z") and parse_iso(ts).tzinfo is not None
    assert parse_iso("2026-11-21T10:00:00").year == 2026


def test_emit_text_and_json():
    report = SimpleReport(
        kind="approve", exit_code=2, summary="Waiting for your yes.", lines=["R2 looser"]
    )
    buf = io.StringIO()
    output.emit(report, False, stream=buf)
    assert buf.getvalue() == "R2 looser\nWaiting for your yes.\n"
    buf = io.StringIO()
    output.emit(report, True, stream=buf)
    data = json.loads(buf.getvalue())
    assert data["exit_code"] == 2 and data["kind"] == "approve"
    buf = io.StringIO()
    output.emit(report, False, text="custom text", stream=buf)
    assert buf.getvalue() == "custom text\n"
    buf = io.StringIO()
    output.emit({"a": 1}, False, stream=buf)
    assert buf.getvalue() == "a: 1\n"


def test_setup_problem_and_not_implemented():
    buf = io.StringIO()
    assert output.setup_problem("check", "no laws/ folder", True, hint="run init", stream=buf) == 3
    data = json.loads(buf.getvalue())
    assert (
        data["exit_code"] == 3
        and data["summary"].startswith("SETUP PROBLEM")
        and data["hint"] == "run init"
    )
    buf = io.StringIO()
    assert output.not_implemented("draft", False, stream=buf) == 3
    assert "not implemented yet" in buf.getvalue()


def test_helpers():
    assert output.plain_example({"a": "1", "b": "x"}) == "a=1, b=x"
    assert output.plain_example({"call": "f(a=1)"}) == "f(a=1)"
    assert output.plain_example(None) == "" and output.plain_example("s") == "s"
    t = output.table([["payment $40, refund $50", "rejected", "✓"]], ["input", "result", "holds"])
    assert "input" in t.splitlines()[0] and "-----" in t.splitlines()[1]
    assert output.table([]) == ""


def test_yes_no():
    out = io.StringIO()
    assert output.yes_no("Approve?", stream_in=io.StringIO("maybe\ny\n"), stream_out=out) is True
    assert "Please answer" in out.getvalue()
    assert (
        output.yes_no("Approve?", stream_in=io.StringIO("no\n"), stream_out=io.StringIO()) is False
    )
    assert (
        output.yes_no(
            "Approve?", default=False, stream_in=io.StringIO("\n"), stream_out=io.StringIO()
        )
        is False
    )
    assert output.yes_no("Approve?", stream_in=io.StringIO(""), stream_out=io.StringIO()) is None
