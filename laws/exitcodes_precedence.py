from lawkeep import law, given, st, example, assume
from lawkeep.exitcodes import combine


@law(
    "Combining result codes gives setup problem (3) if any code is 3, otherwise some laws "
    "fail (1) if any code is 1, otherwise waiting for a person (2) if any code is 2, "
    "otherwise all laws hold (0)"
)
@given(codes=st.lists(st.sampled_from([0, 1, 2, 3]), min_size=1, max_size=10))
@example(codes=[2, 1])
@example(codes=[0, 3, 1, 2])
@example(codes=[0, 2, 0])
def combine_picks_most_important_code(codes):
    if 3 in codes:
        expected = 3
    elif 1 in codes:
        expected = 1
    elif 2 in codes:
        expected = 2
    else:
        expected = 0
    assert combine(*codes) == expected
