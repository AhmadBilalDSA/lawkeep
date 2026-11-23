"""The protection report on made-up projects in throwaway git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from lawkeep import config
from lawkeep.errors import SetupError
from lawkeep.protection import changed_functions, parse_diff, protection_report

FIXTURE = Path(__file__).parent / "fixtures" / "strength" / "shop"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Person",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Person",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(root: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV, "HOME": str(root.parent)}
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


POINTS_LAW = """
    from lawkeep import given, law, st
    from shop.refunds import loyalty_points


    @law("You get one point per 10 spent")
    @given(total=st.integers(min_value=0, max_value=10_000))
    def one_point_per_ten(total):
        assert loyalty_points(total) == total // 10
"""

REFUNDS_BASE = '''
    """Refunds and fees for a made-up shop."""

    from decimal import Decimal


    def refund(charge: Decimal, amount: Decimal) -> Decimal:
        """Give back `amount` of `charge`, but never more than the charge itself."""
        if amount > charge:
            return charge
        return amount


    def apply_fee(amount: Decimal) -> Decimal:
        """A 2% fee, nothing for amounts under 1."""
        if amount < Decimal("1"):
            return Decimal("0")
        return amount * Decimal("0.02")


    def loyalty_points(total: int) -> int:
        """One point per 10 spent."""
        return total // 10


    def old_helper(x):
        return x
'''

REFUNDS_FEATURE = '''
    """Refunds and fees for a made-up shop."""

    from decimal import Decimal

    MAX_REFUNDS = 3


    def refund(charge: Decimal, amount: Decimal) -> Decimal:
        """Give back `amount` of `charge`, but never more than the charge itself."""
        if amount > charge:
            return charge
        if amount < 0:
            return Decimal("0")
        return amount


    def apply_fee(amount: Decimal) -> Decimal:
        """A 2% fee, nothing for amounts under 1."""
        if amount < Decimal("1"):
            return Decimal("0")
        return amount * Decimal("0.02")


    def loyalty_points(total: int) -> int:
        """One point per 10 spent."""
        points = total // 10
        return points
'''

DISCOUNTS = """
    def discount(total: int) -> int:
        if total >= 100:
            return total // 20
        return 0
"""


def make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    write(root, "src/shop/refunds.py", REFUNDS_BASE)
    write(root, "laws/points.py", POINTS_LAW)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-qb", "feature")
    write(root, "src/shop/refunds.py", REFUNDS_FEATURE)
    write(root, "src/shop/discounts.py", DISCOUNTS)  # new, untracked file
    return root


def test_parse_diff():
    diff = textwrap.dedent(
        """\
        diff --git a/src/a.py b/src/a.py
        --- a/src/a.py
        +++ b/src/a.py
        @@ -3,0 +4,2 @@ def f():
        +    x = 1
        +    y = 2
        @@ -10,2 +12,0 @@ def g():
        @@ -20 +20 @@
        -old
        +new
        diff --git a/src/gone.py b/src/gone.py
        --- a/src/gone.py
        +++ /dev/null
        @@ -1,3 +0,0 @@
        """
    )
    changed, anchors, deleted = parse_diff(diff)
    assert changed["src/a.py"] == {4, 5, 20}
    assert anchors["src/a.py"] == {12}
    assert deleted == {"src/gone.py"}


def test_protected_partly_and_not_protected(tmp_path):
    root = make_repo(tmp_path)
    cfg = config.load_config(root)
    changed = {c.function: c for c in changed_functions(root, cfg)}
    assert set(changed) == {
        "shop.refunds:refund",
        "shop.refunds:loyalty_points",
        "shop.discounts:discount",
    }
    assert changed["shop.refunds:refund"].changed_lines == [12, 13]

    report = protection_report(root, cfg)
    by = {f.function: f for f in report.functions}
    points = by["shop.refunds:loyalty_points"]
    assert points.status == "protected", points
    assert points.planted_bugs_caught == points.planted_bugs_total > 0
    assert points.laws == ["points.one_point_per_ten"]

    refund = by["shop.refunds:refund"]
    assert refund.status == "partly", refund
    assert 0 < refund.planted_bugs_caught < refund.planted_bugs_total
    assert refund.note and refund.note.startswith("missed: line")

    discount = by["shop.discounts:discount"]  # a new file no law runs
    assert discount.status == "not_protected"
    assert discount.planted_bugs_caught == 0 and discount.planted_bugs_total > 0
    assert "no law runs this function" in (discount.note or "")

    assert report.base_ref == "main"
    assert any("old_helper" in n and "removed" in n for n in report.notes)
    assert any("outside any function" in n and "line 5" in n for n in report.notes)
    assert "1 protected, 1 partly, 1 NOT protected" in report.summary
    assert report.exit_code == 0  # informational: lawkeep shows, it does not block
    # the working tree is untouched
    assert "points = total // 10" in (root / "src/shop/refunds.py").read_text()
    assert git(root, "status", "--porcelain").count("\n") == 2


def test_explicit_base_uses_the_merge_base(tmp_path):
    root = make_repo(tmp_path)
    git(root, "add", "-A")
    git(root, "commit", "-qm", "feature work")
    git(root, "checkout", "-q", "main")
    write(root, "src/shop/other.py", "def later():\n    return 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "main moved on")
    git(root, "checkout", "-q", "feature")
    cfg = config.load_config(root)
    names = {c.function for c in changed_functions(root, cfg, base_ref="main")}
    assert "shop.other:later" not in names  # only what the feature branch changed
    assert "shop.discounts:discount" in names
    report = protection_report(root, cfg, base_ref="main")
    assert report.base_ref == "main"


def test_unknown_base_is_a_setup_problem(tmp_path):
    root = make_repo(tmp_path)
    with pytest.raises(SetupError) as err:
        changed_functions(root, config.load_config(root), base_ref="does-not-exist")
    assert "does-not-exist" in err.value.message


def test_no_git_checks_every_public_function(tmp_path):
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    report = protection_report(root, config.load_config(root))
    assert report.base_ref is None
    assert any("not a git repository" in n for n in report.notes)
    by = {f.function: f.status for f in report.functions}
    assert set(by) == {
        "shop.refunds:refund",
        "shop.refunds:apply_fee",
        "shop.refunds:loyalty_points",
    }
    assert by["shop.refunds:loyalty_points"] == "not_protected"
    assert by["shop.refunds:refund"] in ("partly", "protected")


def test_repository_without_commits_counts_everything_as_new(tmp_path):
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q")
    names = {c.function for c in changed_functions(root, config.load_config(root))}
    assert names == {"shop.refunds:refund", "shop.refunds:apply_fee", "shop.refunds:loyalty_points"}


def test_nothing_changed(tmp_path):
    root = tmp_path / "shop"
    shutil.copytree(FIXTURE, root)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    report = protection_report(root, config.load_config(root))
    assert report.functions == []
    assert report.summary.startswith("No changed functions")
    assert report.exit_code == 0
