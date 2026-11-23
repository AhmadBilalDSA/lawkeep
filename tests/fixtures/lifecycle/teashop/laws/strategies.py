"""Input generators for the tea shop laws."""

from hypothesis import strategies as st


def percents():
    return st.integers(min_value=0, max_value=100)
