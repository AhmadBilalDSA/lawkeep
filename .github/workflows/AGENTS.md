# .github/workflows

CI for the lawkeep repository itself (see `README.md` here for the table).

- `ci.yml`: tests on Python 3.10 / 3.12 / 3.13, ruff and pyright (the `lint` job; pyright's
  version comes from `uv.lock` through `uv sync --locked`), both examples through
  `lawkeep check`, each planted bug failing the gate, and a self-test of `action.yml`.
- `lawkeep.yml`: lawkeep checking its own repository (the root `laws/`, three laws about
  `lawkeep.exitcodes`). Installed by `lawkeep init`, fingerprinted in the root `laws.lock`, so it
  is protected: agents cannot edit it, and a hand edit shows up as an unrecorded law change.
- `nightly.yml`: deep search on the examples.
- `templates/`: workflows for users to copy (GitHub does not run files in sub-folders; see
  `templates/AGENTS.md`). `lawkeep init --ci` installs its own copy from
  `src/lawkeep/adapters/templates/lawkeep-workflow.yml`.
- `README.md`: the table of what runs where.

All three workflows use the local action (`uses: ./`, `lawkeep-spec: ${{ github.workspace }}`) and
may only pass inputs that the root `action.yml` defines. `ci.yml` repeats the local commands:
change it when the test or lint commands change.

None of these have run on GitHub yet; there is no local test for them.

## release.yml

Publishes lawkeep to PyPI when a `v*` tag is pushed: lint, pyright, full tests, tag must equal `lawkeep.__version__`, `uv build`, `twine check`, then upload with PyPI trusted publishing (environment `pypi`, no token) and a GitHub release. One-time PyPI setup: project lawkeep → Publishing → GitHub publisher (owner `lawkeep`, repo `lawkeep`, workflow `release.yml`, environment `pypi`). To release: bump `__version__`, add a `CHANGELOG.md` entry, commit, tag `v<version>`, push the tag.
