"""Which disputes to fight, and whether the evidence still exists to fight them."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.representment import (ASSUMED_WIN_RATE, Evidence,
                               REPRESENTMENT_FEE_MINOR, STAFF_COST_MINOR,
                               assemble, cost_to_fight, decide, portfolio,
                               work_queue)

COMPLETE = Evidence(ref="r1", found=True, tier="hot", amount_minor=50_000,
                    provenance=[{"file_id": "STL1", "line_no": 3,
                                 "rule": "exact_ref"}])
PARTIAL = Evidence(ref="r1", found=True, tier="archive", amount_minor=50_000,
                   provenance=[])
MISSING = Evidence(ref="r1", found=False)


# --------------------------------------------------------------- evidence
def test_complete_evidence_needs_the_provenance_not_just_the_amount():
    """The amount alone proves a number; the file-and-line proves the number
    came from the network rather than from a spreadsheet."""
    assert COMPLETE.complete is True and COMPLETE.status == "complete"
    assert PARTIAL.complete is False and PARTIAL.status == "partial"
    assert MISSING.status == "unavailable"


def test_assemble_reports_a_missing_record_rather_than_an_empty_one():
    e = assemble(lambda ref, dates: None, "r1", ["2026-01-01"])
    assert e.found is False and e.status == "unavailable"


def test_assemble_carries_the_tier_that_answered():
    """"Found hot" and "found in the archive" are the same answer to the dispute
    and different answers to a capacity question."""
    def lookup(ref, dates):
        return {"tier": "archive", "found_in": "2020-01-28",
                "amount_minor": 14_700, "events": [{"rule": "exact_ref"}],
                "lookup_seconds": 0.059}

    e = assemble(lookup, "r1", ["2020-01-28"])
    assert e.tier == "archive" and e.business_date == "2020-01-28"
    assert e.complete is True


# ------------------------------------------------------ can we evidence it
def test_a_purged_record_makes_the_dispute_unwinnable_regardless_of_merits():
    """The retention policy seen from the other end. A purged record does not
    make a dispute hard to win; it makes it impossible, on the merits of a
    transaction that may well have been perfectly good."""
    d = decide("duplicate_processing", 500_000, MISSING)
    assert d.action == "cannot_evidence"
    assert d.cost_to_fight_minor == 0, "no fee should be spent on a certain loss"


def test_the_strongest_reason_code_cannot_rescue_missing_evidence():
    """duplicate_processing has the highest assumed win rate in the table and it
    does not matter."""
    assert ASSUMED_WIN_RATE["duplicate_processing"] == max(ASSUMED_WIN_RATE.values())
    assert decide("duplicate_processing", 10_000_000, MISSING).action == "cannot_evidence"


def test_partial_evidence_halves_the_rate_rather_than_blocking_the_fight():
    """A number with no provenance is contestable, not worthless -- treating it
    as worthless folds disputes still worth more than the fee."""
    full = decide("duplicate_processing", 500_000, COMPLETE)
    part = decide("duplicate_processing", 500_000, PARTIAL)
    assert part.action == "fight"
    assert part.win_rate == pytest.approx(full.win_rate / 2)
    assert "partial evidence" in part.reason


# ------------------------------------------------------- is it worth it
def test_a_small_dispute_is_not_worth_fighting():
    """The fee is charged whether you win or lose, and leaving it out of the
    comparison makes every small dispute look worth contesting."""
    d = decide("unrecognised", 2_000, COMPLETE)      # $20 at a 35% rate
    assert d.action == "fold"
    assert d.net_minor < 0


def test_the_representment_fee_is_what_makes_it_fold():
    """Stated as its own test because the fee is the term people leave out. The
    same dispute with no fee and no staff cost is worth fighting."""
    with_fee = decide("unrecognised", 2_000, COMPLETE)
    without = decide("unrecognised", 2_000, COMPLETE, fee_minor=0, staff_minor=0)
    assert with_fee.action == "fold"
    assert without.action == "fight"


def test_a_large_winnable_dispute_is_worth_fighting():
    d = decide("duplicate_processing", 500_000, COMPLETE)
    assert d.action == "fight" and d.net_minor > 0


def test_a_large_dispute_on_a_weak_reason_code_can_still_fold():
    """Size alone does not decide it. Fraud at a 15% assumed rate needs to be
    big before the arithmetic works."""
    small_fraud = decide("fraud", 12_000, COMPLETE)
    big_fraud = decide("fraud", 500_000, COMPLETE)
    assert small_fraud.action == "fold"
    assert big_fraud.action == "fight"


def test_the_breakeven_is_where_expected_recovery_equals_the_cost():
    cost = cost_to_fight()
    rate = ASSUMED_WIN_RATE["fraud"]
    breakeven = int(cost / rate)
    assert decide("fraud", breakeven - 1000, COMPLETE).action == "fold"
    assert decide("fraud", breakeven + 5000, COMPLETE).action == "fight"


def test_an_unknown_reason_code_uses_the_default_rate_not_the_best_one():
    """A code the table does not know must not inherit an optimistic rate."""
    from src.representment import DEFAULT_WIN_RATE
    d = decide("some_new_code", 500_000, COMPLETE)
    assert d.win_rate == DEFAULT_WIN_RATE
    assert d.win_rate < max(ASSUMED_WIN_RATE.values())


def test_the_win_rates_are_labelled_as_assumptions_not_estimates():
    """This project has no representment outcomes to estimate from. A model
    fitted to nothing and quoted to three decimals would be the worst artifact
    in the repo."""
    import src.representment as rep
    assert hasattr(rep, "ASSUMED_WIN_RATE")
    assert not hasattr(rep, "ESTIMATED_WIN_RATE")
    assert "declared stand-ins" in rep.__doc__ or "stand-ins" in rep.__doc__


# ------------------------------------------------------------ the queue
def _cb(ref, due, amount, state="received", code="fraud"):
    return {"ref": ref, "evidence_due_on": due, "amount_minor": amount,
            "state": state, "reason_code": code}


def test_the_queue_is_ordered_by_deadline_not_by_value():
    """Sorting a work queue by value is the intuitive thing and it loses the
    winnable disputes at the front of the queue."""
    rows = [_cb("big", "2026-03-25", 200_000), _cb("small", "2026-03-05", 20_000)]
    q = work_queue(rows, as_of="2026-03-04")
    assert [r["ref"] for r in q] == ["small", "big"]


def test_value_breaks_ties_within_the_same_deadline():
    rows = [_cb("a", "2026-03-05", 20_000), _cb("b", "2026-03-05", 200_000)]
    assert [r["ref"] for r in work_queue(rows, "2026-03-04")] == ["b", "a"]


def test_overdue_items_sort_first_and_are_flagged():
    rows = [_cb("ontime", "2026-03-10", 100_000),
            _cb("late", "2026-03-01", 100_000)]
    q = work_queue(rows, "2026-03-04")
    assert q[0]["ref"] == "late" and q[0]["overdue"] is True
    assert q[1]["overdue"] is False


def test_closed_disputes_are_not_in_the_work_queue():
    rows = [_cb("open", "2026-03-10", 100_000),
            _cb("done", "2026-03-05", 100_000, state="won")]
    assert [r["ref"] for r in work_queue(rows, "2026-03-04")] == ["open"]


def test_the_queue_marks_which_codes_are_worth_defending():
    rows = [_cb("a", "2026-03-10", 100_000, code="duplicate_processing"),
            _cb("b", "2026-03-11", 100_000, code="fraud")]
    q = work_queue(rows, "2026-03-04")
    assert q[0]["winnable_code"] is True
    assert q[1]["winnable_code"] is False


# --------------------------------------------------------- the portfolio
def test_the_portfolio_separates_folding_from_being_blocked():
    """Different causes with different fixes. Folding is a pricing decision;
    being unable to evidence is a storage decision."""
    ds = [decide("duplicate_processing", 500_000, COMPLETE),
          decide("unrecognised", 2_000, COMPLETE),
          decide("duplicate_processing", 500_000, MISSING)]
    p = portfolio(ds)
    assert p["fight"] == 1 and p["fold"] == 1 and p["cannot_evidence"] == 1


def test_folding_is_reported_as_a_saving_not_a_loss():
    """The alternative was paying the fee to lose. A chargeback team measured on
    win rate alone is rewarded for fighting only the easy ones."""
    p = portfolio([decide("unrecognised", 2_000, COMPLETE)])
    assert p["avoided_by_folding_minor"] > 0


def test_the_net_only_counts_disputes_actually_fought():
    ds = [decide("duplicate_processing", 500_000, COMPLETE),
          decide("unrecognised", 2_000, COMPLETE)]
    p = portfolio(ds)
    assert p["cost_to_fight_minor"] == cost_to_fight(), (
        "the folded dispute's fee was counted, but folding means not paying it")


def test_an_empty_book_does_not_divide_by_zero():
    assert portfolio([])["disputes"] == 0
