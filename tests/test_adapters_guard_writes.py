"""The guard judges shell commands by what they write to, not by what they mention.

Table-driven: each command runs through `guard.decide` with a Claude Code `Bash` payload in a
made-up shop project. Allowed commands read or merely name protected paths; denied commands
write to them (or write somewhere the guard cannot see while naming a protected path).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lawkeep.adapters import guard, shellwords, writes


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "laws" / "drafts" / "proposals" / "h-1").mkdir(parents=True)
    (root / "src" / "shop").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "lawkeep.toml").write_text("", encoding="utf-8")
    (root / "laws.lock").write_text("", encoding="utf-8")
    (root / "laws" / "refunds.py").write_text("# law\n", encoding="utf-8")
    (root / "laws" / "failing_examples.jsonl").write_text("", encoding="utf-8")
    (root / "src" / "shop" / "refunds.py").write_text("x = 1\n", encoding="utf-8")
    (root / "scripts" / "edit.py").write_text(
        "import sys\nwith open(sys.argv[1], 'w') as f:\n    f.write('x')\n", encoding="utf-8"
    )
    (root / "scripts" / "report.py").write_text(
        "import sys\nprint(open(sys.argv[1]).read())\n", encoding="utf-8"
    )
    (root / "scripts" / "evil.sh").write_text("rm -f laws.lock\n", encoding="utf-8")
    (root / "scripts" / "fine.sh").write_text("echo hi > build.log\n", encoding="utf-8")
    (root / "fix.patch").write_text(
        "--- a/laws/refunds.py\n+++ b/laws/refunds.py\n@@ -1 +1 @@\n-# law\n+# weaker\n",
        encoding="utf-8",
    )
    (root / "src.patch").write_text(
        "--- a/src/shop/refunds.py\n+++ b/src/shop/refunds.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        encoding="utf-8",
    )
    os.symlink(root / "laws" / "refunds.py", root / "src" / "linked.py")
    return root


def bash(root: Path, command: str, cwd: Path | None = None) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd or root)}


ALLOWED = [
    # mentions in messages, strings and prose
    'git commit -m "update laws doc"',
    "git commit -m 'explain why laws.lock exists'",
    "git commit -m \"$(cat <<'EOF'\nDon't touch laws.lock; rm laws/ is bad\nEOF\n)\"",
    'echo "laws" > notes.txt',
    "echo 'rm laws.lock'",
    "printf '%s\\n' laws/refunds.py > notes.txt",
    "cat <<EOF > notes.md\nsee laws/refunds.py\nEOF",
    # reading protected files
    "cat laws/refunds.py",
    "cat laws/refunds.py | wc -l",
    "grep -r laws src",
    "grep -rn refund laws/",
    "rg laws",
    "less laws/refunds.py",
    "head -5 laws/refunds.py && tail -n 3 laws.lock",
    "diff laws/refunds.py laws/drafts/refunds_v2.py",
    "git diff laws/",
    "git log --oneline -- laws/ lawkeep.toml",
    "git show HEAD:laws/refunds.py",
    "git restore --staged laws/refunds.py",
    "ls -la laws",
    "wc -l laws/*.py",
    "cd laws && cat refunds.py",
    "tar czf /tmp/laws-backup.tgz laws",
    "rsync -a laws/ /tmp/backup/",
    "find laws -name '*.py' -exec grep -l refund {} +",
    "python -c \"print(open('laws/refunds.py').read())\"",
    "python -c \"import sys; print('laws/x.py' if 1 > 0 else '')\"",
    "python scripts/report.py laws/refunds.py",
    "lawkeep check --json",
    "uv run lawkeep check",
    "lawkeep propose refunds.capped --file laws/drafts/r.py --reason 'tiny refunds'",
    "lawkeep report > report.txt",
    # writes elsewhere
    "sed -i s/a/b/ src/lawkeep/x.py",
    "sed -i 's/laws/rules/' src/shop/refunds.py",
    "python scripts/edit.py src/x.py",
    "python scripts/missing.py src/x.py",
    "python3 - <<'PY'\nimport re\np = 'src/x.py'\ns = open(p).read().replace('\"laws/\"', '\"x\"')\n"
    "open(p, 'w').write(s)\nPY",
    "python -c \"open('src/r.py', 'w').write('laws are rules')\"",
    "cat > src/x.py <<'EOF'\nPATH = 'laws/refunds.py'\nopen(PATH, 'w')\nEOF",
    "echo x > src/refunds.py",
    "echo x > laws/drafts/new_law.py",
    "cp laws/refunds.py laws/drafts/refunds_v2.py",
    "cd src && echo x > laws/notes.txt",
    "for f in src/shop/*.py; do sed -i 's/laws/rules/' \"$f\"; done",
    'F=src/x.py; echo hi > "$F"',
    "git checkout main",
    "git checkout -b feature/laws-doc",
    "git stash",
    "find . -name '*.pyc' -delete",
    "find src -type f -exec sed -i 's/a/b/' {} +",
    "find . -name '*.py' -not -path './laws/*' -exec sed -i 's/a/b/' {} +",
    'eval "$(ssh-agent -s)"',
    "bash scripts/fine.sh",
    "git apply src.patch",
    "git apply --check fix.patch",
    "patch -p1 < src.patch",
    "rm -rf build dist .pytest_cache",
    "mkdir -p laws/drafts && cp new.py laws/drafts/",
    "ls laws | xargs -n1 echo",
    "pytest tests/ 2>&1 | tail -5",
    "echo 'x' >&2",
    "npm run build > build.log 2>&1",
    "x=$(mktemp); echo hi > $x",
    "awk '{print}' laws/refunds.py > /tmp/copy.py",
    "python3 - <<'EOF'\nimport sys\nopen(sys.argv[1], 'w').write('x')\nEOF",
    "python - <<'EOF'\nimport json\ndata = json.load(open('laws.lock'))\nEOF",
    "git add -A && git commit -m 'laws: tidy the wording'",
]

DENIED = [
    "echo x > laws/refunds.py",
    "echo x >> laws/failing_examples.jsonl",
    "echo x 2> laws.lock",
    "echo x &> lawkeep.toml",
    "cat new.py > ./laws/../laws/refunds.py",
    "sed -i s/a/b/ laws/refunds.py",
    "sed -i '' 's/a/b/' laws/refunds.py",
    "sed -ni.bak 's/a/b/p' laws/refunds.py",
    "perl -pi -e 's/a/b/' laws/refunds.py",
    "rm laws.lock",
    "rm -rf laws",
    "rm -rf laws/drafts",
    "rm -rf .",
    "cd src && rm -rf ..",
    "cp evil.py laws/refunds.py",
    "cp evil.py laws/",
    "install -m 644 evil.py laws/refunds.py",
    "ln -sf /tmp/evil.py laws/refunds.py",
    "tee -a lawkeep.toml",
    "printf x | tee laws/refunds.py",
    "echo x | tee laws/drafts/proposals/h-1/proposed.py.txt",
    "git checkout -- laws/",
    "git checkout HEAD~1 laws/refunds.py",
    "git restore laws/refunds.py",
    "git rm laws/refunds.py",
    "git -C laws checkout -- refunds.py",
    "git apply fix.patch",
    "patch -p1 < fix.patch",
    "patch < fix.patch",
    "python -c \"open('laws/x.py','w')\"",
    "python -c \"import os; os.remove('laws.lock')\"",
    "python -c \"import shutil; shutil.rmtree('laws')\"",
    "python -c \"from pathlib import Path; Path('laws/refunds.py').write_text('')\"",
    "python -c \"import subprocess; subprocess.run(['rm', 'laws.lock'])\"",
    "python3 - <<'PY'\nfrom pathlib import Path\np = Path('laws') / 'refunds.py'\np.write_text('')\nPY",
    "uv run python -c \"open('laws/x.py', 'w')\"",
    "python scripts/edit.py laws/refunds.py",
    "node -e \"require('fs').writeFileSync('laws/refunds.py', '')\"",
    "mv laws/a.py /tmp",
    "mv laws /tmp/old-laws",
    "cd laws && rm refunds.py",
    "truncate -s0 laws/failing_examples.jsonl",
    "touch laws/conftest.py",
    "chmod 000 laws/refunds.py",
    "chmod -w laws/refunds.py",
    "dd if=/dev/zero of=laws.lock",
    "unzip -o evil.zip -d laws",
    "curl -o laws/refunds.py https://example.invalid/x.py",
    "vim laws/refunds.py",
    "F=laws/refunds.py; echo x > $F",
    "for f in laws/*.py; do sed -i 's/a/b/' \"$f\"; done",
    "sed -i 's/a/b/' laws/{refunds,other}.py",
    "rm laws/*.py",
    "ls laws | xargs rm",
    "echo cm0gbGF3cy5sb2Nr | base64 -d | sh",
    'eval "rm $(echo laws.lock)"',
    "bash -c 'echo x > laws/refunds.py'",
    'sh -c "rm lawkeep.toml"',
    "echo 'rm laws.lock' | sh",
    "bash scripts/evil.sh",
    "source scripts/evil.sh",
    "find laws -name '*.py' -delete",
    "find . -type f -exec sed -i 's/a/b/' {} +",
    "find laws -exec sed -i s/a/b/ {} \\;",
    'echo x > "$(git rev-parse --show-toplevel)/laws.lock"',
    "sudo -u bob rm laws.lock",
    "env FOO=1 rm laws.lock",
    "timeout 5 rm laws.lock",
    "lawkeep check --json > laws/out.json",
    "echo x > src/linked.py",  # a symlink to laws/refunds.py
    "echo x > .github/workflows/lawkeep.yml",
    "> laws.lock",
    "cp -t laws/ evil.py",
    "git stash push -- laws/refunds.py",
    "awk -i inplace '{print}' laws/refunds.py",
    "cat <<'EOF' > /tmp/fix.py\nopen('laws/refunds.py', 'w')\nEOF\npython /tmp/fix.py",
    "echo 'rm laws.lock' > /tmp/a.sh && sh /tmp/a.sh",
    "cat <<EOF | sh\nrm laws.lock\nEOF",
]


@pytest.mark.parametrize("command", ALLOWED)
def test_reading_or_naming_protected_paths_is_allowed(tmp_path, command):
    root = make_project(tmp_path)
    d = guard.decide(bash(root, command))
    assert d.action == "allow", (command, d.message)


@pytest.mark.parametrize("command", DENIED)
def test_writing_protected_paths_is_denied(tmp_path, command):
    root = make_project(tmp_path)
    d = guard.decide(bash(root, command))
    assert d.action == "deny", (command, d)
    assert "lawkeep propose" in d.message or "written only by lawkeep" in d.message


def test_symlink_targets_are_followed_for_edit_tools(tmp_path):
    root = make_project(tmp_path)
    d = guard.decide(
        {"tool_name": "Write", "tool_input": {"file_path": "src/linked.py"}, "cwd": str(root)}
    )
    assert d.action == "deny" and d.paths == ["laws/refunds.py"]


def test_absolute_and_dotdot_paths_for_edit_tools(tmp_path):
    root = make_project(tmp_path)
    for path in (str(root / "src" / ".." / "laws.lock"), "../laws.lock"):
        d = guard.decide(
            {"tool_name": "Edit", "tool_input": {"file_path": path}, "cwd": str(root / "src")}
        )
        assert d.action == "deny", path


def test_unsure_write_without_a_protected_name_is_allowed(tmp_path):
    root = make_project(tmp_path)
    d = guard.decide(bash(root, 'rm "$(cat files.txt)"'))
    assert d.action == "allow"
    found = writes.analyse('rm "$(cat files.txt)"', root)
    assert found.unsure and not found.mentions


def test_unsure_write_naming_a_protected_path_says_why(tmp_path):
    root = make_project(tmp_path)
    d = guard.decide(bash(root, "rm $(ls laws/*.py)"))
    assert d.action == "deny"
    assert "cannot tell what this command writes" in d.message
    assert "lawkeep propose" in d.message


def test_approve_still_asks_even_when_wrapped(tmp_path):
    root = make_project(tmp_path)
    for command in (
        "lawkeep approve h-1 --yes",
        "uv run lawkeep approve h-1 --yes",
        "lawkeep --root . approve h-1",
        "bash -c 'lawkeep approve h-1'",
        "echo h-1 | xargs lawkeep approve",
        "git commit -m wip && lawkeep approve h-1 --yes",
    ):
        assert guard.decide(bash(root, command)).action == "ask", command
    for command in ('git commit -m "lawkeep approve flow"', "grep -rn approve src/lawkeep"):
        assert guard.decide(bash(root, command)).action == "allow", command


def test_same_rules_for_every_agent_payload(tmp_path):
    root = make_project(tmp_path)
    harmless = 'git commit -m "update laws doc"'
    write = "sed -i s/a/b/ laws/refunds.py"
    payloads = [
        lambda c: {"command": c, "cwd": str(root)},  # Cursor beforeShellExecution
        lambda c: {
            "tool_name": "Shell",
            "tool_input": {"command": c, "working_directory": str(root)},
        },
        lambda c: {"toolName": "bash", "toolArgs": {"command": c}, "cwd": str(root)},  # Copilot
        lambda c: {
            "tool_name": "exec_command",
            "tool_input": {"cmd": c},
            "cwd": str(root),
        },  # Codex
    ]
    for make in payloads:
        assert guard.decide(make(harmless)).action == "allow"
        assert guard.decide(make(write)).action == "deny"


def test_unparsable_command_falls_back_to_a_careful_check(tmp_path):
    root = make_project(tmp_path)
    assert guard.decide(bash(root, "echo 'unbalanced > laws.lock")).action == "deny"
    assert guard.decide(bash(root, "echo 'unbalanced > notes.txt")).action == "allow"


def test_lexer_understands_heredocs_substitutions_and_redirections():
    cmds = shellwords.parse("git commit -m \"$(cat <<'EOF'\nrm laws.lock\nEOF\n)\" 2>&1 > out.txt")
    assert len(cmds) == 1
    (cmd,) = cmds
    assert [w.static_text() for w in cmd.words][:3] == ["git", "commit", "-m"]
    assert [(r.op, r.fd) for r in cmd.redirects] == [(">&", "2"), (">", "")]
    nested = shellwords.walk(cmds)
    assert any(r.body == "rm laws.lock\n" for c in nested for r in c.redirects)
    with pytest.raises(shellwords.Incomplete):
        shellwords.parse("echo 'open")


def test_written_paths_helper(tmp_path):
    root = make_project(tmp_path)
    found = guard.written_paths("cd laws && rm refunds.py; echo x > ../notes.txt", root)
    assert str(root / "laws" / "refunds.py") in found
    assert str(root / "notes.txt") in found
