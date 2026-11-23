# Workflows

| File | Runs in this repository? | What it does |
| --- | --- | --- |
| `ci.yml` | yes, on pushes to `main` and pull requests | Tests (Python 3.10, 3.12, 3.13, including the cheating test suite), ruff, pyright (type check), both example projects through `lawkeep check`, each planted bug failing the gate, and a self-test of `action.yml` on `examples/billing`. |
| `lawkeep.yml` | yes, on pull requests, pushes to `main` and nightly | lawkeep checking its own laws (the root `laws/`), with the action and package from this checkout. Installed by `lawkeep init`; protected by `laws.lock`. |
| `nightly.yml` | yes, nightly | `lawkeep check --profile deep` on both examples; uploads the report and any newly saved failing examples. |
| `templates/lawkeep.yml` | no (GitHub only runs files directly in this folder) | Copy into your project: checks laws on every pull request and posts one comment. |
| `templates/lawkeep-nightly.yml` | no | Copy into your project: nightly deep search. |

The templates use `lawkeep/lawkeep@v0`, which works once a release tag exists. Until then,
point `uses:` at a commit of this repository.
