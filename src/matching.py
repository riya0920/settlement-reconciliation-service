"""Multi-pass matching with candidate scoring.

Single-pass exact-reference matching handles the easy 95% and then declares the
rest "unmatched", which pushes the actual work onto a human. The hard 5% is the
job, and it needs passes that get progressively less certain and progressively
more explicit about why they fired:

  PASS 1  exact reference                        certainty 1.00
  PASS 2  reference + amount within tolerance    certainty 0.95
  PASS 3  amount + date window, no reference     certainty scored, 0.50-0.90
  PASS 4  residual -> break, with the best
          rejected candidate recorded

Pass 3 is where judgement enters, so its scoring is explicit and every component
is recorded on the match. "The system matched it" is not an answer to an
auditor; "amount agreed exactly (0.5), settled one business day later (0.25),
same merchant (0.15), no competing candidate within 0.1 (0.1) = 0.90" is.

The rule that keeps pass 3 honest: **a candidate only wins if it is materially
better than the runner-up.** Two plausible candidates mean the evidence does not
identify one of them, and picking the higher score is guessing with extra steps.
Ambiguous cases go to the break queue, where a human decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

AMOUNT_TOLERANCE_MINOR = 50          # fee/FX noise on a reference-matched pair
DATE_WINDOW_DAYS = 3                 # T+0 through T+3 settlement
MIN_CANDIDATE_SCORE = 0.60
MIN_MARGIN_OVER_RUNNER_UP = 0.10


@dataclass
class Candidate:
    internal_ref: str
    settlement_line: int
    score: float
    components: dict = field(default_factory=dict)

    def explain(self) -> str:
        return ", ".join("{} ({:+.2f})".format(k, v)
                         for k, v in self.components.items())


@dataclass
class MatchOutcome:
    pass_name: str
    internal_ref: str | None
    settlement_line: int | None
    certainty: float
    rule: str
    detail: str = ""
    rejected_runner_up: Candidate | None = None


def _business_days_apart(a: str, b: str) -> int:
    d1, d2 = date.fromisoformat(a), date.fromisoformat(b)
    if d1 > d2:
        d1, d2 = d2, d1
    n, cur = 0, d1
    while cur < d2:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def score_candidate(internal: dict, row: dict) -> Candidate:
    """Explicit, additive scoring. Every component is recorded, not just the sum."""
    comp = {}

    delta = abs(internal["amount_minor"] - abs(row["gross_minor"]))
    if delta == 0:
        comp["amount_exact"] = 0.50
    elif delta <= AMOUNT_TOLERANCE_MINOR:
        comp["amount_within_tolerance"] = 0.35
    elif delta <= abs(internal["amount_minor"]) * 0.005:
        comp["amount_within_50bps"] = 0.20
    else:
        comp["amount_mismatch"] = -0.40

    gap = _business_days_apart(internal["auth_date"], row["settled_date"])
    if gap == 0:
        comp["same_business_day"] = 0.20
    elif gap <= DATE_WINDOW_DAYS:
        comp["within_settlement_window"] = 0.25 - 0.05 * gap
    else:
        comp["outside_settlement_window"] = -0.30

    if internal.get("currency") == row.get("currency"):
        comp["currency_match"] = 0.15
    else:
        comp["currency_mismatch"] = -0.50

    return Candidate(internal["ref"], row["line_no"],
                     round(sum(comp.values()), 4), comp)


def match(internal_txns: list[dict], settlement_rows: list[dict]) -> list[MatchOutcome]:
    """Returns one outcome per settlement row."""
    by_ref = {t["ref"]: t for t in internal_txns}
    unmatched_internal = dict(by_ref)
    outcomes: list[MatchOutcome] = []

    # ---- pass 1 + 2: reference-keyed -------------------------------------
    deferred = []
    for row in settlement_rows:
        t = by_ref.get(row["ref"])
        if t is None:
            deferred.append(row)
            continue
        delta = abs(t["amount_minor"] - abs(row["gross_minor"]))
        if delta == 0:
            outcomes.append(MatchOutcome("pass1_exact_reference", t["ref"],
                                         row["line_no"], 1.00,
                                         "reference and amount agree exactly"))
        elif delta <= AMOUNT_TOLERANCE_MINOR:
            outcomes.append(MatchOutcome("pass2_reference_tolerance", t["ref"],
                                         row["line_no"], 0.95,
                                         "reference agrees; amount within {} minor"
                                         .format(AMOUNT_TOLERANCE_MINOR),
                                         "delta {}".format(delta)))
        else:
            outcomes.append(MatchOutcome("pass2_reference_amount_break", t["ref"],
                                         row["line_no"], 0.90,
                                         "reference agrees but amount does not",
                                         "delta {}".format(delta)))
        unmatched_internal.pop(t["ref"], None)

    # ---- pass 3: candidate scoring on the residual -----------------------
    for row in deferred:
        candidates = sorted(
            (score_candidate(t, row) for t in unmatched_internal.values()),
            key=lambda c: -c.score)
        best = candidates[0] if candidates else None
        runner = candidates[1] if len(candidates) > 1 else None

        if best is None or best.score < MIN_CANDIDATE_SCORE:
            outcomes.append(MatchOutcome(
                "pass4_unmatched", None, row["line_no"], 0.0,
                "no candidate scored above {:.2f}".format(MIN_CANDIDATE_SCORE),
                "best {:.2f}".format(best.score) if best else "no candidates",
                rejected_runner_up=best))
            continue

        if runner and (best.score - runner.score) < MIN_MARGIN_OVER_RUNNER_UP:
            # Two plausible answers means the evidence does not identify one.
            outcomes.append(MatchOutcome(
                "pass4_ambiguous", None, row["line_no"], 0.0,
                "top two candidates within {:.2f} -- refusing to guess"
                .format(MIN_MARGIN_OVER_RUNNER_UP),
                "{} ({:.2f}) vs {} ({:.2f})".format(
                    best.internal_ref, best.score, runner.internal_ref, runner.score),
                rejected_runner_up=runner))
            continue

        outcomes.append(MatchOutcome(
            "pass3_candidate_scored", best.internal_ref, row["line_no"],
            round(best.score, 2), "scored candidate match", best.explain(),
            rejected_runner_up=runner))
        unmatched_internal.pop(best.internal_ref, None)

    return outcomes


def summarise(outcomes: list[MatchOutcome]) -> dict:
    by_pass: dict[str, int] = {}
    for o in outcomes:
        by_pass[o.pass_name] = by_pass.get(o.pass_name, 0) + 1
    matched = sum(1 for o in outcomes if o.internal_ref is not None)
    return {
        "total": len(outcomes),
        "matched": matched,
        "auto_match_rate": matched / len(outcomes) if outcomes else 0.0,
        "by_pass": by_pass,
    }
