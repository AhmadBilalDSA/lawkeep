from lawkeep import law, given, st, example, assume
from lawkeep.exitcodes import combine, ALL_HOLD


@law("Combining no result codes at all, or only 'all laws hold' codes, gives all laws hold (0)")
@given(count=st.integers(min_value=0, max_value=10))
@example(count=0)
@example(count=1)
def combine_of_nothing_is_all_hold(count):
    assert combine(*([ALL_HOLD] * count)) == ALL_HOLD
