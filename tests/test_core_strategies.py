"""Tests for lawkeep.strategies (st)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings

from lawkeep import st, strategies


@settings(max_examples=50)
@given(st.money())
def test_money_default_range(x):
    assert isinstance(x, Decimal)
    assert Decimal("0") <= x <= Decimal("1000000")
    assert x == x.quantize(Decimal("0.01"))


@settings(max_examples=50)
@given(st.money(1, 5, places=0))
def test_money_custom(x):
    assert 1 <= x <= 5 and x == x.to_integral_value()


def test_money_bad_range():
    with pytest.raises(ValueError, match="larger"):
        st.money(10, 1)


@settings(max_examples=30)
@given(st.percentages(), st.non_empty_text(), st.ids(prefix="user"))
def test_helpers(p, t, i):
    assert 0 <= p <= 100
    assert t.strip()
    assert i.startswith("user-")


def test_hypothesis_strategies_are_reexported():
    import hypothesis.strategies as hst

    assert st.integers is hst.integers
    assert st.composite is hst.composite
    assert "money" in strategies.__all__


def test_project_strategies_lookup(tmp_path, monkeypatch):
    (tmp_path / "lawkeep.toml").write_text("")
    (tmp_path / "laws").mkdir()
    (tmp_path / "laws" / "strategies.py").write_text(
        "from lawkeep import st\ndef charges():\n    return st.integers(min_value=1, max_value=9)\n"
    )
    monkeypatch.setenv("LAWKEEP_ROOT", str(tmp_path))
    strategies.reset_project_cache()
    try:
        from hypothesis import find

        value = find(st.charges(), lambda v: True)
        assert 1 <= value <= 9
        with pytest.raises(AttributeError, match="laws/strategies.py"):
            st.no_such_thing  # noqa: B018
    finally:
        strategies.reset_project_cache()


def test_unknown_name_without_project(tmp_path, monkeypatch):
    monkeypatch.delenv("LAWKEEP_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    strategies.reset_project_cache()
    with pytest.raises(AttributeError):
        st.charges  # noqa: B018
