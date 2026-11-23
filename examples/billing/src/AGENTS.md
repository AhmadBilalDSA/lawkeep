# examples/billing/src

Source folder of the billing example (`[project] source = ["src"]` in `../lawkeep.toml`). It holds
one package, `billing/`; see `billing/AGENTS.md`.

lawkeep puts this folder on `sys.path` when it runs the laws, so laws import `billing.*`
directly. It is not installed into the lawkeep venv and ruff does not lint it.
Check: `uv run lawkeep check --root examples/billing`.
