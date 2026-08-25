"""Deciding which disputes to fight, and assembling the evidence to fight them.

`src/chargebacks.py` has the state machine -- received, represented, won, lost --
and a `WINNABLE` set of reason codes with a comment saying that knowing which
disputes to fight "is the difference between a chargeback team and a chargeback
cost centre". Nothing used it. Every dispute took the same path, evidence was
never assembled from anywhere, and `submit_evidence` was a state transition with
no evidence attached to it.

THREE DECISIONS THIS MAKES, in the order they actually bind.

1. CAN WE EVIDENCE IT AT ALL? Not "should we fight" -- "is the underlying record
   still retrievable". A representment submitted without the transaction record
   behind it is auto-lost, and the answer depends on `src/retention.py`: hot,
   archived, or purged.

   THIS IS THE RETENTION POLICY SEEN FROM THE OTHER END. `run_retention.py`
   frames it as "a retention window shorter than the dispute tail means evidence
   will be missing when it is needed". This is the moment it is needed. A purged
   record does not make a dispute hard to win; it makes it impossible, on the
   merits of a transaction that may well have been perfectly good.

2. IS IT WORTH FIGHTING? A dispute the merchant will lose is cheaper to accept
   than to contest. The comparison is expected recovery against the cost of
   contesting, and both sides are needed:

       expected recovery = win_probability x amount
       cost to fight     = staff time + the representment fee, WHICH IS CHARGED
                           WHETHER YOU WIN OR LOSE

   The fee is the term people leave out, and leaving it out makes every small
   dispute look worth contesting. On a $20 dispute with a $15 representment fee
   and a 40% win rate, fighting has an expected value of MINUS $7.

3. WHAT ORDER? By DEADLINE, not by amount. A $200 dispute due tomorrow outranks
   a $2,000 dispute due in three weeks, because the second one can still be
   fought later and the first cannot. Sorting a work queue by value is the
   intuitive thing and it loses the winnable disputes at the front of the queue
   -- and losing on a missed deadline is not a soft failure. `transition`
   already refuses late evidence for that reason; this stops the work arriving
   late in the first place.

WHAT THIS DELIBERATELY DOES NOT DO: fabricate a win probability. The rates below
are declared stand-ins, not estimates from this project's data -- it has no
representment outcomes to estimate from. A model fitted to nothing and quoted to
three decimal places would be the worst artifact in the repo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .chargebacks import WINNABLE

# Declared stand-ins. Real figures are network-, reason- and merchant-specific
# and come from a book of outcomes this project does not have. Named
# ASSUMED_ rather than ESTIMATED_ so nobody quotes them as measurements.
ASSUMED_WIN_RATE = {
    "product_not_received": 0.55,
    "duplicate_processing": 0.80,
    "credit_not_processed": 0.60,
    "fraud": 0.15,
    "unrecognised": 0.35,
}
DEFAULT_WIN_RATE = 0.25

# Charged whether you win or lose, which is the whole reason small disputes are
# not worth contesting.
REPRESENTMENT_FEE_MINOR = 1_500          # $15
STAFF_COST_MINOR = 800                   # $8 of analyst time


@dataclass
class Evidence:
    ref: str
    found: bool
    tier: str = ""
    business_date: str = ""
    amount_minor: int | None = None
    provenance: list = field(default_factory=list)
    lookup_seconds: float = 0.0

    @property
    def complete(self) -> bool:
        """The transaction AND its provenance.

        Both halves are required and the second is the one that gets dropped:
        the amount alone proves a number, and the file-and-line proves the
        number came from the network rather than from a spreadsheet. A
        representment carrying only the first is contestable.
        """
        return self.found and bool(self.provenance)

    @property
    def status(self) -> str:
        if not self.found:
            return "unavailable"
        return "complete" if self.provenance else "partial"


def assemble(evidence_lookup, ref: str, search_dates: list) -> Evidence:
    """Find the record wherever it now lives, or report that it does not.

    `evidence_lookup` is `archival_job.evidence_for` bound to a connection and
    store. Injected rather than imported so this module does not depend on how
    the archive happens to be wired.
    """
    found = evidence_lookup(ref, search_dates)
    if found is None:
        return Evidence(ref=ref, found=False)
    return Evidence(
        ref=ref,
        found=True,
        tier=found.get("tier", ""),
        business_date=str(found.get("auth_date") or found.get("found_in") or ""),
        amount_minor=found.get("amount_minor"),
        provenance=found.get("events", []),
        lookup_seconds=found.get("lookup_seconds", 0.0),
    )


@dataclass
class FightDecision:
    action: str                  # fight | fold | cannot_evidence
    reason: str
    expected_recovery_minor: int
    cost_to_fight_minor: int
    net_minor: int
    win_rate: float


def cost_to_fight(fee_minor: int = REPRESENTMENT_FEE_MINOR,
                  staff_minor: int = STAFF_COST_MINOR) -> int:
    return fee_minor + staff_minor


def decide(reason_code: str, amount_minor: int, evidence: Evidence,
           win_rates: dict | None = None,
           fee_minor: int = REPRESENTMENT_FEE_MINOR,
           staff_minor: int = STAFF_COST_MINOR) -> FightDecision:
    rates = win_rates or ASSUMED_WIN_RATE
    rate = rates.get(reason_code, DEFAULT_WIN_RATE)
    cost = cost_to_fight(fee_minor, staff_minor)

    # Evidence first. Not "should we", but "can we" -- a representment without
    # the record behind it is auto-lost, and paying the fee to lose is strictly
    # worse than accepting.
    if not evidence.found:
        return FightDecision(
            "cannot_evidence",
            "the transaction record is not retrievable; a representment "
            "without it is auto-lost, so the fee would buy a certain loss",
            0, 0, 0, rate)

    if not evidence.complete:
        # Partial evidence halves the assumed rate rather than blocking the
        # fight. A number with no provenance is contestable, not worthless --
        # and treating it as worthless would fold disputes that are still worth
        # more than the fee.
        rate = rate / 2

    expected = int(rate * amount_minor)
    net = expected - cost

    if net <= 0:
        return FightDecision(
            "fold",
            "expected recovery {} minor against {} minor to fight ({}% win rate"
            "{}); accepting costs less than contesting".format(
                expected, cost, round(rate * 100),
                ", halved for partial evidence" if not evidence.complete else ""),
            expected, cost, net, rate)

    return FightDecision(
        "fight",
        "expected recovery {} minor against {} minor to fight ({}% win rate{})"
        .format(expected, cost, round(rate * 100),
                ", halved for partial evidence" if not evidence.complete else ""),
        expected, cost, net, rate)


def work_queue(rows, as_of: str) -> list:
    """Open disputes, ordered by DEADLINE and then by value.

    Sorting by value is the intuitive thing and it loses the winnable disputes
    at the front of the queue: a $200 dispute due tomorrow outranks a $2,000 one
    due in three weeks, because the second can still be fought later and the
    first cannot.
    """
    out = []
    for r in rows:
        if r["state"] != "received":
            continue
        days = (date.fromisoformat(r["evidence_due_on"])
                - date.fromisoformat(as_of)).days
        out.append({**r, "days_to_deadline": days,
                    "overdue": days < 0,
                    "winnable_code": r["reason_code"] in WINNABLE})
    # Deadline ascending, then amount descending inside the same day.
    return sorted(out, key=lambda r: (r["days_to_deadline"], -r["amount_minor"]))


def portfolio(decisions) -> dict:
    """What the whole book is worth fighting, and what retention cost it.

    The last figure is the one worth having: value lost specifically because the
    record was not retrievable. It attributes a chargeback loss to a STORAGE
    decision, which is a connection nobody makes until someone draws it.
    """
    fight = [d for d in decisions if d.action == "fight"]
    fold = [d for d in decisions if d.action == "fold"]
    blocked = [d for d in decisions if d.action == "cannot_evidence"]
    return {
        "disputes": len(decisions),
        "fight": len(fight),
        "fold": len(fold),
        "cannot_evidence": len(blocked),
        "expected_recovery_minor": sum(d.expected_recovery_minor for d in fight),
        "cost_to_fight_minor": sum(d.cost_to_fight_minor for d in fight),
        "net_minor": sum(d.net_minor for d in fight),
        # Folding is a saving, not a loss: the alternative was paying the fee to
        # lose. Reported so a chargeback team is not measured on win rate alone,
        # which rewards fighting only the easy ones.
        "avoided_by_folding_minor": sum(
            cost_to_fight() - d.expected_recovery_minor for d in fold),
    }
