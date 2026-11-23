"""Input generators for this project's laws.

Laws use them as ``st.<name>()``, for example ``@given(charge=st.charges())``. lawkeep looks
here for any name that is not a built-in Hypothesis strategy. This file is part of your laws:
changing it outside lawkeep is flagged as an unrecorded law change.

Example (made up)::

    from lawkeep import st

    def charges():
        return st.builds(Charge, total=st.money(min_value=0, max_value=10_000))
"""
