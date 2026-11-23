from lawkeep import law, given, st, example, assume
from lawkeep.exitcodes import combine, ALL_HOLD, SOME_FAIL, WAITING, SETUP_PROBLEM


@law("The order in which result codes are combined never changes the combined result")
@given(
    codes=st.lists(st.sampled_from([ALL_HOLD, SOME_FAIL, WAITING, SETUP_PROBLEM]), max_size=10),
    seed=st.randoms(use_true_random=False),
)
@example(codes=[WAITING, SOME_FAIL], seed=None)
@example(codes=[SETUP_PROBLEM, ALL_HOLD, WAITING], seed=None)
def combine_ignores_order(codes, seed):
    reordered = list(reversed(codes))
    if seed is not None:
        seed.shuffle(reordered)
    assert combine(*reordered) == combine(*codes)
