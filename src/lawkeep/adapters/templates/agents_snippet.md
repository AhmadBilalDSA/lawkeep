## lawkeep: laws this code must obey

This project uses [lawkeep](https://github.com/lawkeep/lawkeep). Laws are rules the code must
obey for every input. Each law is one plain-English sentence plus an executable check, kept in
`laws/` and fingerprinted in `laws.lock`. People decide on laws; coding agents write code that
obeys them. Every law change is recorded and shown to the person.

1. **Before you finish any task, run `lawkeep check`** (or the `check` tool of the `lawkeep`
   MCP server; add `--json` for machine-readable output). It prints `ALL LAWS HOLD` or
   `SOME LAWS FAIL`. Result codes: 0 all laws hold, 1 some fail, 2 waiting for a person's
   decision, 3 setup problem.
2. **When a law fails, fix the code, not the law.** Every failure comes with the simplest
   failing example, for example `refund(charge=40, amount=50) returned 50, but a refund must
   never exceed the charge`. Read it, fix the code, and run `lawkeep check` again.
3. **Never change `laws/`, `laws.lock` or `lawkeep.toml` directly**: not with edit tools, not
   with shell commands. Do not add `conftest.py`, `sitecustomize.py`, `.pth` files, skip or xfail
   markers, or special cases for the failing input to get around a law; lawkeep flags all of
   these. To change a law, write the new version to a file under `laws/drafts/` and run
   `lawkeep propose <LAW_ID> --file laws/drafts/<file>.py --reason "<why>"`. Stricter changes
   may apply right away; anything that weakens a law waits for the person's yes.
4. **If a law conflicts with your task, stop and ask the person** (when they are there). Say
   which law, show the example that fails and what you would change. Stopping to ask is a
   successful outcome, not a failure. Nobody there? See "When no person is there" below.
5. **Run `lawkeep approve` only when the person asks you to**, after showing them the law's
   sentence and examples. Your coding agent will ask them to confirm the command; never
   approve a law change on your own.
6. **At the end of the session, show the person `lawkeep report`**: the law changes made in
   this session, anything changed outside lawkeep, and how many things wait for their
   decision in `lawkeep approve`.

### When no person is there (long runs on your own)

A person is there when they are chatting with you in this session. If you are unsure, read
`person_present` in `lawkeep check --json` (lawkeep's guess; it is `false` in CI, in
`claude -p` style runs and when `LAWKEEP_PERSON_PRESENT=0`). When nobody is there and a law
blocks you:

1. **Park it:** write the change you need to `laws/drafts/` and run `lawkeep propose
   <LAW_ID> --file laws/drafts/<file>.py --reason "<why>"`. Looser or unclear changes wait for
   the person; note the id it prints (`h-...`).
2. **Never work around the law:** do not edit `laws/`, `laws.lock` or `lawkeep.toml`, do not
   special-case the failing input, and never run `lawkeep approve`.
3. **Keep going** with every part of the task that does not depend on the blocked law.
   Leave the parts that do depend on it undone.
4. **End with a summary in three lists:** done; waiting for the person (each `h-...` id or
   draft, with one line on why); not done because a law blocked it. The person decides
   everything at once with `lawkeep approve`.
