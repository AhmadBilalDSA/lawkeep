# examples/bookings/src

Source folder of the bookings example (`[project] source = ["src"]` in `../lawkeep.toml`). It
holds one package, `bookings/`; see `bookings/AGENTS.md`.

lawkeep puts this folder on `sys.path` when it runs the laws, so laws import `bookings.*`
directly. It is not installed into the lawkeep venv and ruff does not lint it.
Check: `uv run lawkeep check --root examples/bookings`.
