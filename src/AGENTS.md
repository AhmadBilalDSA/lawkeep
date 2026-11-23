# src

Source root (src layout). It holds a single package, `lawkeep/`; see `lawkeep/AGENTS.md`.

- Packaging is in the root `pyproject.toml` (hatchling): the wheel ships `src/lawkeep`, the
  version comes from `src/lawkeep/__init__.py`, the console script is `lawkeep = lawkeep.cli:main`
  and the pytest plugin entry point is `pytest11: lawkeep = lawkeep.pytest_plugin`.
- `uv sync` installs it in editable mode into `.venv`, so tests and `uv run lawkeep` use this code.
- Do not add other top-level packages here without updating `pyproject.toml`.

Test: `uv run pytest` (whole suite) and `uv run ruff check src tests && uv run ruff format --check src tests`.
