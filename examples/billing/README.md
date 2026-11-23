# Example: Corner Shop billing

A made-up shop's billing code: card charges with a 2% card fee, refunds (the shop gets back the
same share of the card fee), discount codes (`SAVE10`, `SAVE25`, `FIVEOFF`) and rounding to
whole cents.

## The laws

| Law | In plain English |
| --- | --- |
| `refunds.refund_never_exceeds_payment` | A refund never exceeds the original payment |
| `refunds.full_refund_gives_back_exact_payment` | Refunding exactly what was paid gives back exactly the payment and the whole card fee |
| `refunds.partial_refund_is_exact_and_fee_share_is_fair` | A partial refund gives back exactly the amount asked for and a fair share of the card fee |
| `discounts.discount_stays_between_zero_and_price` | A discount code never raises the price and never takes it below zero |
| `rounding.rounding_is_to_the_nearest_cent` | Rounding money gives whole cents and is never off by more than half a cent |
| `discount_amounts.discount_takes_off_exactly_what_it_promises` | Each discount code takes off exactly what it promises: SAVE10 10%, SAVE25 25%, FIVEOFF $5.00 (never below $0.00), and no code or an unknown code takes off nothing |

Why the second law? The first one alone is weak: code that refuses every refund would pass it.
The "exact amount" companion closes that gap. The same goes for discounts: "never raises the
price and never below zero" also holds for code that makes everything free, so the last law
pins down exactly what each code takes off. It was added with `lawkeep propose` (a new law only
adds checks, so it went in automatically after passing the quick strength check).
`lawkeep strength` finds weak laws like that by swapping the code for deliberately broken
versions ("always refuse", "always 0") and checking whether any law notices; a law that only
checks a limit is fine when another law catches what it lets through, and
`lawkeep strength --root examples/billing` reports no weak laws.

## See the gate catch a bug

```bash
cp -r examples/billing /tmp/billing-bug && cd /tmp/billing-bug
git apply planted_bug.patch     # the refund now also hands back the card-fee share
lawkeep check
```

Expected: `SOME LAWS FAIL`. The refund laws fail, each with the simplest input Hypothesis could
shrink it to: a small charge such as $0.25 with a $0.01 fee, where the customer gets $0.26 back
for a $0.25 payment. The discount and rounding laws still hold.

## See the protection report flag unprotected code

```bash
cp -r examples/billing /tmp/billing-v2 && cd /tmp/billing-v2
git init -q && git add -A && git commit -qm before
git apply unprotected_change.patch   # adds refund_v2(), which no law tests
lawkeep check                        # still ALL LAWS HOLD: the laws only test refund()
lawkeep report --base HEAD           # refund_v2 is marked NOT protected: read that code
```

This is the headline in action: only review what your laws don't protect.
