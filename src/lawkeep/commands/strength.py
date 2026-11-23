"""`lawkeep strength`: check whether your laws are strong enough to catch real bugs.

Owner: workstream C (strength & protection).

`lawkeep strength [--full] [--law LAW_ID ...] [--drafts]`:

1. coverage map: which functions each law runs (coverage.py, one context per law);
2. quick check: swap each function a law calls for dumb versions (always None / 0 / its input /
   an error) in the test process only; a law that still holds is flagged one-sided or vacuous;
3. planted bugs: a few per function (``--full``: every bug lawkeep can plant, in every function
   a law runs); a law that catches fewer than ``[strength] min_bugs_caught`` is flagged;
4. weak test data: Hypothesis statistics (inputs thrown away by assume()/filter) and lines the
   generated inputs never reached;
5. gaps: public functions no law runs.

Result codes: 0 no law flagged · 1 some law flagged weak · 3 setup problem.
Nothing in the project is changed: dumb versions and planted bugs live only in the memory of a
separate test process.
"""

from __future__ import annotations

import argparse

from lawkeep import config, output, paths
from lawkeep.strength import run_strength
from lawkeep.strength.render import strength_text

HELP = "test whether your laws are strong enough to catch real mistakes in the code"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--full",
        action="store_true",
        help="plant every small bug lawkeep can make in every function a law runs (slower; "
        "for nightly runs or before approving important laws)",
    )
    parser.add_argument(
        "--law",
        dest="law_ids",
        metavar="LAW_ID",
        action="append",
        default=None,
        help="measure only this law (repeatable), e.g. --law refunds.refund_is_capped",
    )
    parser.add_argument(
        "--drafts",
        action="store_true",
        help="also measure the drafts in laws/drafts/ (they are not approved yet)",
    )
    parser.add_argument(
        "--details",
        "-v",
        action="store_true",
        help="also show law ids, every planted bug a law missed and test-data notes",
    )


def run(args: argparse.Namespace) -> int:
    root = paths.require_root(args.root)
    cfg = config.load_config(root)
    report = run_strength(
        root,
        cfg,
        law_ids=args.law_ids,
        full=bool(args.full),
        include_drafts=bool(getattr(args, "drafts", False)),
    )
    text = None
    if not args.json:
        text = strength_text(report, details=bool(getattr(args, "details", False)))
    output.emit(report, args.json, text=text)
    return report.exit_code
