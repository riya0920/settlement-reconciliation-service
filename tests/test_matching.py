"""Matching and chargeback-lifecycle tests."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.chargebacks import (EVIDENCE_WINDOW_DAYS, ChargebackBook, ChargebackError)
from src.matching import (MIN_CANDIDATE_SCORE, match, score_candidate, summarise)


def _internal(ref="AUTH1", amount=100_000, d="2026-04-01", ccy="USD"):
    return {"ref": ref, "amount_minor": amount, "auth_date": d, "currency": ccy}


def _row(ref="AUTH1", gross=100_000, d="2026-04-02", ccy="USD", line=1):
    return {"ref": ref, "gross_minor": gross, "settled_date": d,
            "currency": ccy, "line_no": line}


# ------------------------------------------------------------------ matching
def test_pass1_exact_reference():
    out = match([_internal()], [_row()])
    assert out[0].pass_name == "pass1_exact_reference"
    assert out[0].certainty == 1.00


def test_pass2_tolerance_and_amount_break_are_distinguished():
    near = match([_internal()], [_row(gross=100_030)])[0]
    far = match([_internal()], [_row(gross=90_000)])[0]
    assert near.pass_name == "pass2_reference_tolerance"
    assert far.pass_name == "pass2_reference_amount_break"


def test_pass3_scores_a_referenceless_row():
    """No reference on the settlement row, but amount, date and currency all
    agree and there is only one candidate."""
    out = match([_internal()], [_row(ref="UNKNOWN")])[0]
    assert out.pass_name == "pass3_candidate_scored"
    assert out.internal_ref == "AUTH1"
    assert "amount_exact" in out.detail


def test_ambiguous_candidates_are_refused_not_guessed():
    """Two identical candidates carry no evidence identifying either. Picking the
    higher score is guessing with extra steps."""
    twins = [_internal(ref="A"), _internal(ref="B")]
    out = match(twins, [_row(ref="UNKNOWN")])[0]
    assert out.pass_name == "pass4_ambiguous"
    assert out.internal_ref is None
    assert "refusing to guess" in out.rule


def test_weak_candidate_does_not_win():
    weak = _internal(ref="C", amount=999_999, d="2026-01-01", ccy="EUR")
    out = match([weak], [_row(ref="UNKNOWN")])[0]
    assert out.pass_name == "pass4_unmatched"
    assert out.internal_ref is None


def test_score_components_are_recorded_not_just_the_total():
    """'The system matched it' is not an answer to an auditor."""
    c = score_candidate(_internal(), _row(ref="UNKNOWN"))
    assert c.components
    assert c.score == pytest.approx(sum(c.components.values()))
    assert "amount_exact" in c.explain()


def test_currency_mismatch_is_disqualifying():
    c = score_candidate(_internal(ccy="USD"), _row(ref="X", ccy="EUR"))
    assert c.score < MIN_CANDIDATE_SCORE


def test_an_internal_txn_is_only_matched_once():
    rows = [_row(ref="UNKNOWN", line=1), _row(ref="UNKNOWN", line=2)]
    out = match([_internal()], rows)
    matched = [o for o in out if o.internal_ref is not None]
    assert len(matched) == 1, "the same internal transaction was matched twice"


def test_summary_reports_auto_match_rate():
    out = match([_internal()], [_row()])
    s = summarise(out)
    assert s["auto_match_rate"] == 1.0


# --------------------------------------------------------------- chargebacks
@pytest.fixture
def book():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    return ChargebackBook(con)


def _cb(book, reason="fraud", received="2026-05-01", auth="2026-04-01"):
    return book.receive("AUTH1", "F1", 1, reason, 100_000, "USD", received, auth)


def test_evidence_deadline_is_derived_from_the_reason_code(book):
    cid = _cb(book, reason="fraud")
    row = book.con.execute("SELECT * FROM chargeback WHERE chargeback_id = ?",
                           (cid,)).fetchone()
    assert row["evidence_due_on"] == "2026-05-08"      # 7 calendar days
    assert EVIDENCE_WINDOW_DAYS["fraud"] == 7


def test_deadlines_are_calendar_days_not_business_days(book):
    """Card network rules count calendar days. Using business days would grant
    several days that do not exist."""
    cid = book.receive("A", "F", 1, "fraud", 1, "USD", "2026-05-01", None)
    row = book.con.execute("SELECT evidence_due_on FROM chargeback"
                           " WHERE chargeback_id = ?", (cid,)).fetchone()
    assert row["evidence_due_on"] == "2026-05-08"


def test_evidence_after_the_deadline_is_refused(book):
    cid = _cb(book)
    with pytest.raises(ChargebackError, match="deadline"):
        book.transition(cid, "submit_evidence", "2026-05-20")


def test_evidence_within_the_deadline_is_accepted(book):
    cid = _cb(book)
    assert book.transition(cid, "submit_evidence", "2026-05-05") == "represented"
    assert book.transition(cid, "win", "2026-05-20") == "won"


def test_illegal_transitions_are_refused(book):
    cid = _cb(book)
    with pytest.raises(ChargebackError, match="illegal transition"):
        book.transition(cid, "win", "2026-05-05")


def test_expiry_sweep_separates_accepted_from_expired(book):
    """Both end in a loss, but one is a decision and the other is an operational
    failure. Reporting them together hides how much the process itself costs."""
    lost_on_purpose = book.receive("A", "F", 1, "fraud", 1, "USD", "2026-05-01", None)
    lost_by_neglect = book.receive("B", "F", 2, "fraud", 1, "USD", "2026-05-01", None)
    book.transition(lost_on_purpose, "accept", "2026-05-02")
    assert book.expire_overdue("2026-05-20") == 1

    states = book.summary()
    assert states["accepted"]["count"] == 1
    assert states["expired"]["count"] == 1


def test_deadline_report_flags_urgency(book):
    _cb(book, received="2026-05-01")
    rep = book.deadline_report("2026-05-07")
    assert rep[0]["days_remaining"] == 1
    assert rep[0]["urgency"] == "urgent"


def test_aged_reference_report_drives_retention_policy(book):
    """A dispute you cannot evidence is a dispute you lose, so retention has to
    outlast the oldest chargeback."""
    book.receive("A", "F", 1, "fraud", 1, "USD", "2026-06-30", "2026-04-01")
    rep = book.aged_reference_report()
    assert rep["max_days"] == 90
