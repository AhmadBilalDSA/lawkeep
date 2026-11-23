# Example projects

Two small, made-up projects with a full lawkeep setup. No code here comes from a real business.

| Example | What the code does | Laws | Planted bug |
| --- | --- | --- | --- |
| [`billing/`](billing/) | Corner Shop: card charges, refunds with card fees, discount codes, money rounding | 5 | the refund also hands back the shop's card-fee share |
| [`bookings/`](bookings/) | Riverside Barbers: appointment slots with a clock you can set | 4 | the overlap check only looks at the new slot's start time |

Each example has:

- `src/<package>/`: the code under test.
- `lawkeep.toml`: settings and permissions (default values, `source = ["src"]`).
- `laws/`: the approved laws, plus `laws/strategies.py` (input generators such as `st.charges()`).
- `laws.lock` and `laws/history.jsonl`: fingerprints and the record of how each law was added.
- `laws/failing_examples.jsonl`: saved failing inputs (empty to start with).
- `planted_bug.patch`: a realistic bug to show the gate catching it.

`billing/` also has `unprotected_change.patch`: a new `refund_v2()` that no law covers, to show
the protection report marking new code as not protected.

## Try it

```bash
# from the lawkeep repository root
uv run lawkeep check --root examples/billing        # ALL LAWS HOLD

cp -r examples/billing /tmp/billing-bug
cd /tmp/billing-bug && git apply planted_bug.patch
uv run --project ~/path/to/lawkeep lawkeep check     # SOME LAWS FAIL, with the simplest failing input
```

Always plant bugs in a copy, never in `examples/` itself: changing a law file there would (rightly)
show up as an unrecorded law change.

## How the history and lock were made

`laws.lock` and `laws/history.jsonl` were written by
[`make_example_history.py`](make_example_history.py), which records an `init` entry and then
one `add_law` entry per law through `lawkeep.history.record_change` (the same function the
lawkeep commands use). The examples were written while `lawkeep init`, `draft` and `approve`
were still being built; once those work, this script can be replaced by running them. If you
change an example's laws or `lawkeep.toml`, rebuild its record:

```bash
uv run python examples/make_example_history.py examples/billing
```

The cheating test suite in [`tests/tamper/`](../tests/tamper/) copies these examples into temp
folders and plays known tricks against them.
