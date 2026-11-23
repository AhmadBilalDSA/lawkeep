"""Input generators for the made-up shop."""

from hypothesis import strategies as st


def cents():
    return st.integers(min_value=0, max_value=100_000)
