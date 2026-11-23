# Gate test fixtures (made-up project)

`refunds_project/` is a tiny made-up shop with a `refund` function and three laws.
Tests copy it into a temp folder, create `laws.lock` with `history.record_change(action="init")`,
and then plant bugs or tricks in the copy. `buggy_refunds.py` is a variant of
`src/shop/refunds.py` with a planted bug (refunds above the charge are allowed).

No `conftest.py` lives here on purpose: this repository's own pytest run would load it.
