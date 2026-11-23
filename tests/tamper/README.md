# Cheating test suite

Known tricks a coding agent might use to make failing laws "pass", each scripted against a
throwaway copy of `examples/billing` in a temp folder, with an assertion that lawkeep catches it
and says so. No test calls a real coding agent or the network.

| Test | Trick | What lawkeep must do |
| --- | --- | --- |
| `test_trick_editing_a_law_is_flagged_as_unrecorded_law_change` | weaken an assert in a law | flag `unrecorded_law_change`, result code 2 |
| `test_trick_editing_a_law_on_top_of_a_real_bug_still_fails` | weaken one law while the bug is in place | flag it; the other laws still fail (code 1) |
| `test_trick_deleting_a_law_file_is_flagged` | delete a law file | flag the removal |
| `test_trick_editing_laws_strategies_is_flagged` | make the input generator produce only easy inputs | flag `laws/strategies.py` |
| `test_trick_adding_a_skipping_conftest_inside_laws_is_flagged` | add `laws/conftest.py` that skips every law | flag the new file; laws not reported as holding |
| `test_trick_lowering_examples_in_lawkeep_toml_is_flagged` | "try 1 case" in `lawkeep.toml` | flag `lawkeep.toml` |
| `test_trick_settings_max_examples_1_on_a_law_is_flagged` | `@settings(max_examples=1)` on a law | flag the law file |
| `test_trick_adding_skip_or_xfail_to_a_law_is_flagged[...]` | `@pytest.mark.skip`, `@pytest.mark.xfail`, `pytest.skip()` | flag the change and `skip_marker`; the law is not "holds" |
| `test_trick_conftest_outside_laws_is_not_loaded_and_is_flagged` | `./conftest.py` that patches the buggy function | never load it (a marker file proves it), flag `side_door_file`, laws fail |
| `test_trick_sitecustomize_is_not_run_and_is_flagged[...]` | `sitecustomize.py` in `src/` or the root | never run it, flag it, laws fail |
| `test_trick_usercustomize_and_pth_files_are_flagged` | `usercustomize.py`, a `.pth` file | never run them, flag them |
| `test_trick_root_pytest_config_cannot_deselect_laws[...]` | `addopts = -k 'not refund'` in pytest.ini / tox.ini / setup.cfg / pyproject.toml | laws still run and fail |
| `test_trick_patched_code_pyc_with_matching_mtime_is_ignored` | bytecode of the clean code stamped with the buggy file's mtime and size | ignore it: laws fail |
| `test_trick_patched_law_pyc_with_matching_mtime_is_ignored_and_flagged` | vacuous law bytecode (plain and pytest-rewrite caches) next to an unchanged law file | ignore it, flag `bytecode_file`, laws fail |
| `test_trick_deleting_laws_lock_is_flagged` | delete `laws.lock` | flag `lock_missing` |
| `test_trick_editing_a_law_and_rewriting_laws_lock_by_hand_is_flagged` | edit a law and regenerate the lock by hand | flag `lock_rewritten` |
| `test_trick_deleting_a_history_line_is_flagged` | drop the last history entry | flag `append_only_violation` |
| `test_trick_removing_a_saved_failing_example_is_flagged` | drop a saved failing input from the ledger | flag `append_only_violation` |
| `test_trick_special_casing_the_simplest_failing_example_still_fails` | return the right answer only for the input the gate printed | the law still fails, with a different simplest input |
| `test_trick_routing_around_a_law_with_refund_v2_shows_it_as_not_protected` | add `refund_v2()` that no law tests | gate green, protection report marks `refund_v2` as not protected |

Where it helps, a test first shows the trick really works on plain Python or pytest (for example
that plain pytest loads `./conftest.py`, or that Python picks up the stale `.pyc`), so a pass
means lawkeep defended against something real.

`test_tamper_examples.py` checks the example projects themselves: their lock and history are
consistent, their laws hold and the planted bug breaks them (run directly through Hypothesis),
and, through the real gate, that each example passes and each planted bug fails with a small
failing input.

`test_tamper_in_process.py` covers tricks played by the code under test from inside the law
process (skips, `assume()` calls, patching Hypothesis). These are flagged, not prevented: the code
under test runs in the same process as the laws (a known v0.1 limit).

All 45 tests pass; none is marked `xfail`.

Run: `uv run pytest tests/tamper -rxX`
