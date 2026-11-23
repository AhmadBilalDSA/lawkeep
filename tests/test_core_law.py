"""Tests for lawkeep._law (@law), the public API and static discovery agreeing on law ids."""

from __future__ import annotations

import importlib.util
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

import lawkeep
from lawkeep import discovery
from lawkeep._law import LAW_ATTR, MARKER_NAME, LawMeta, get_law, is_law, law, make_law_id


@pytest.fixture(autouse=True)
def _no_root_env(monkeypatch):
    monkeypatch.delenv("LAWKEEP_ROOT", raising=False)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "lawkeep.toml").write_text("")
    (tmp_path / "laws").mkdir()
    return tmp_path


def _meta(obj: object) -> LawMeta:
    meta = get_law(obj)
    assert meta is not None, obj
    return meta


def _import_file(path: Path):
    name = f"lawfile_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LAW_FILE = textwrap.dedent(
    """
    from hypothesis.stateful import RuleBasedStateMachine, rule, invariant
    from lawkeep import law, given, st, example
    import lawkeep

    def refund(charge, amount):
        return min(charge, amount)

    @law("A refund never exceeds   the original charge")
    @given(charge=st.integers(min_value=0), amount=st.integers(min_value=0))
    def refund_is_capped(charge, amount):
        assert refund(charge, amount) <= charge

    @given(charge=st.integers(min_value=0))
    @law("A refund of zero is zero")
    def zero_refund(charge):
        assert refund(charge, 0) == 0

    @lawkeep.law("Refunding the full charge gives the charge", id="R3")
    @example(charge=5)
    @given(charge=st.integers(min_value=0))
    def full_refund(charge):
        assert refund(charge, charge) == charge

    @law("Plain example law")
    def plain_example():
        assert refund(3, 1) == 1

    @law("The balance never goes negative")
    class Wallet(RuleBasedStateMachine):
        def __init__(self):
            super().__init__()
            self.balance = 0

        @rule(n=st.integers(min_value=0, max_value=10))
        def deposit(self, n):
            self.balance += n

        @invariant()
        def not_negative(self):
            assert self.balance >= 0

    TestWallet = Wallet.TestCase
    """
)


@pytest.fixture
def law_module(tmp_path):
    root = _project(tmp_path)
    sub = root / "laws" / "billing"
    sub.mkdir()
    path = sub / "refunds.py"
    path.write_text(LAW_FILE)
    return root, path, _import_file(path)


def test_law_ids_sentences_and_kinds(law_module):
    root, path, m = law_module
    meta = _meta(m.refund_is_capped)
    assert isinstance(meta, LawMeta)
    assert meta.law_id == "billing.refunds.refund_is_capped"
    assert meta.sentence == "A refund never exceeds the original charge"  # whitespace collapsed
    assert meta.kind == "function"
    assert meta.file is not None and Path(meta.file) == path.resolve()
    assert _meta(m.zero_refund).law_id == "billing.refunds.zero_refund"
    assert _meta(m.full_refund).law_id == "R3"
    assert _meta(m.plain_example).law_id == "billing.refunds.plain_example"
    wallet = _meta(m.Wallet)
    assert wallet.kind == "state_machine" and wallet.law_id == "billing.refunds.Wallet"
    assert _meta(m.TestWallet) == wallet
    assert is_law(m.refund) is False


def test_stacked_laws_still_run(law_module):
    _, _, m = law_module
    m.refund_is_capped()
    m.zero_refund()
    m.full_refund()
    m.plain_example()
    m.TestWallet("runTest").runTest()


def test_pytest_marker_is_attached(law_module):
    _, _, m = law_module
    for fn in (m.refund_is_capped, m.zero_refund, m.full_refund, m.plain_example, m.TestWallet):
        marks = [mk for mk in getattr(fn, "pytestmark", []) if mk.name == MARKER_NAME]
        assert marks, fn
        assert marks[0].kwargs["law_id"] == _meta(fn).law_id
        assert marks[0].args[0] == _meta(fn).sentence


def test_failing_law_still_fails(tmp_path):
    root = _project(tmp_path)
    path = root / "laws" / "bad.py"
    path.write_text(
        "from lawkeep import law, given, st\n"
        "@law('Numbers are small')\n"
        "@given(n=st.integers())\n"
        "def small(n):\n"
        "    assert n < 10\n"
    )
    m = _import_file(path)
    with pytest.raises(AssertionError):
        m.small()


def test_static_discovery_matches_runtime_ids(law_module):
    root, _, m = law_module
    found = discovery.discover_laws(root)
    assert found.problems == []
    by_id = found.by_id()
    for obj in (m.refund_is_capped, m.zero_refund, m.full_refund, m.plain_example, m.Wallet):
        meta = _meta(obj)
        assert meta.law_id in by_id
        assert by_id[meta.law_id].sentence == meta.sentence
    assert by_id["R3"].explicit_id is True
    assert by_id["billing.refunds.Wallet"].kind == "state_machine"
    assert all(i.file == "laws/billing/refunds.py" for i in found.laws)


def test_draft_keeps_its_id(tmp_path):
    root = _project(tmp_path)
    drafts = root / "laws" / "drafts"
    drafts.mkdir()
    path = drafts / "fees.py"
    path.write_text(
        "from lawkeep import law\n@law('Fee is 2%')\ndef fee_is_two_percent():\n    pass\n"
    )
    m = _import_file(path)
    assert _meta(m.fee_is_two_percent).law_id == "fees.fee_is_two_percent"
    assert discovery.discover_laws(root).laws == []
    with_drafts = discovery.discover_laws(root, include_drafts=True).laws
    assert [(i.law_id, i.is_draft) for i in with_drafts] == [("fees.fee_is_two_percent", True)]


def test_make_law_id_fallbacks(tmp_path):
    assert make_law_id("f", file=None, laws_dir=None, module="pkg.mod") == "pkg.mod.f"
    assert make_law_id("f", file=tmp_path / "x.py", laws_dir=tmp_path / "laws", module=None) == "f"
    ldir = tmp_path / "laws"
    assert make_law_id("f", file=ldir / "a" / "__init__.py", laws_dir=ldir) == "a.f"


def test_law_outside_laws_dir_uses_module_name():
    @law("Defined in a test module")
    def local_law():
        pass

    meta = _meta(local_law)
    assert meta.law_id.endswith("local_law")
    assert getattr(local_law, LAW_ATTR) is meta


@pytest.mark.parametrize("bad", ["", "   ", None, 3])
def test_law_rejects_bad_sentences(bad):
    with pytest.raises(TypeError, match="sentence"):
        law(bad)


def test_bare_law_decorator_gives_helpful_error():
    with pytest.raises(TypeError, match="plain-English sentence"):

        @law  # pyright: ignore[reportArgumentType]  # deliberately misused
        def oops():
            pass


def test_bad_id_rejected():
    with pytest.raises(TypeError):
        law("x", id="")


def test_public_api_exports():
    from hypothesis import example, given, settings

    assert lawkeep.given is given
    assert lawkeep.example is example
    assert lawkeep.settings is settings
    assert lawkeep.law is law
    assert lawkeep.st.integers is not None
    assert hasattr(lawkeep.st, "money")
    assert lawkeep.__version__ == "0.1.0"
    with pytest.raises(AttributeError):
        lawkeep.does_not_exist  # noqa: B018


def test_discovery_reports_problems(tmp_path):
    root = _project(tmp_path)
    (root / "laws" / "broken.py").write_text("def (:\n")
    (root / "laws" / "dynamic.py").write_text(
        "from lawkeep import law as keep\nS='x'\n@keep(S)\ndef a():\n    pass\n"
    )
    (root / "laws" / "dup1.py").write_text(
        "from lawkeep import law\n@law('a', id='same')\ndef a():\n    pass\n"
    )
    (root / "laws" / "dup2.py").write_text(
        "from lawkeep import law\n@law('b', id='same')\ndef b():\n    pass\n"
    )
    (root / "laws" / "strategies.py").write_text(
        "from lawkeep import law\n@law('not a law file')\ndef x():\n    pass\n"
    )
    res = discovery.discover_laws(root)
    text = "\n".join(res.problems)
    assert "syntax error" in text
    assert "not a plain string" in text
    assert "Two laws share the id 'same'" in text
    ids = sorted(i.law_id for i in res.laws)
    assert ids == ["dynamic.a", "same", "same"]
    assert next(i for i in res.laws if i.law_id == "dynamic.a").sentence is None


def test_discovery_hash_changes_with_law_source(tmp_path):
    root = _project(tmp_path)
    f = root / "laws" / "x.py"
    f.write_text(
        "from lawkeep import law\n@law('a')\ndef a():\n    assert 1\n\ndef helper():\n    pass\n"
    )
    h1 = discovery.discover_laws(root).laws[0].source_hash
    f.write_text(
        "from lawkeep import law\n@law('a')\ndef a():\n    assert 1\n\ndef helper():\n    return 2\n"
    )
    h2 = discovery.discover_laws(root).laws[0].source_hash
    f.write_text(
        "from lawkeep import law\n@law('a')\ndef a():\n    assert 2\n\ndef helper():\n    return 2\n"
    )
    h3 = discovery.discover_laws(root).laws[0].source_hash
    assert h1 == h2 != h3


def test_law_inputs_are_described_and_optional():
    @law("Totals add up", inputs={"codes": "the result   numbers being combined"})
    def adds_up(codes):
        pass

    meta = _meta(adds_up)
    assert meta.inputs == {"codes": "the result numbers being combined"}
    assert meta.to_dict()["inputs"] == {"codes": "the result numbers being combined"}

    @law("No description")
    def plain(x):
        pass

    assert _meta(plain).inputs is None and "inputs" not in _meta(plain).to_dict()
    with pytest.raises(TypeError, match="inputs"):
        law("Bad", inputs={"x": 1})  # pyright: ignore[reportArgumentType]  # deliberately wrong
    with pytest.raises(TypeError, match="inputs"):
        law("Bad", inputs=["x"])  # pyright: ignore[reportArgumentType]  # deliberately wrong


def test_describe_inputs_reads_the_source_without_running_it():
    source = textwrap.dedent(
        """
        from lawkeep import law, given, st

        @law("A refund is capped", inputs={"amount": "what the customer asks back"})
        @given(charge=st.charges(), amount=st.money(0, 1000))
        def capped(charge, amount):
            raise SystemExit("never run")

        class Group:
            @law("nested")
            @given(x=st.integers())
            def inner(self, x):
                pass
        """
    )
    assert discovery.describe_inputs(source, "capped") == {
        "charge": "a sample charge that lawkeep made up for testing",
        "amount": "what the customer asks back",
    }
    assert discovery.describe_inputs(source, "Group.inner") == {
        "x": "a sample whole number that lawkeep made up for testing"
    }
    assert discovery.describe_inputs(source, "missing") == {}
    assert discovery.describe_inputs("def broken(:", "x") == {}
