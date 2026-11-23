# .github

GitHub settings for the lawkeep repository itself. Today it holds only `workflows/` (CI, the
nightly deep search and copy-me templates for users); see `workflows/AGENTS.md`.

- Nothing here runs locally; none of it has run on GitHub yet.
- The workflows use the composite action in the repository root (`action.yml`, `uses: ./`).
- lawkeep checks itself (decided 2026-11-22): `workflows/lawkeep.yml` was installed by
  `lawkeep init` for this repository, is fingerprinted in the root `laws.lock` and uses the local
  action (`uses: ./`, `lawkeep-spec: "."`). Agents cannot edit it (the guard blocks it); a person
  changes it and records the change through lawkeep.
- Gotcha: the Copilot adapter writes `copilot-instructions.md` and `hooks/` into `.github/`, and
  their presence makes `init` detect Copilot. Do not add those here: this repository's lawkeep
  setup is for Claude Code only.

Test: there is no local test for this folder; `ci.yml` is exercised only on GitHub.
