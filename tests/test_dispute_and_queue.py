"""The dispute pack and the break queue that consumes ledger feedback."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import break_queue
from src.dispute_pack import build_pack, group_variances, render


def _var(ref, gross, variance, ccy="USD"):
    return {"ref": ref, "gross_minor": gross, "variance_minor": variance,
            "currency": ccy}


# ------------------------------------------------------- dispute pack
def test_one_mis_applied_tier_collapses_into_one_group():
    """The processor does not care that transaction 4471 was 3 cents light. It
    cares that one tier was wrong 8,000 times."""
    rows = [_var("t{}".format(i), 100_000, -300) for i in range(500)]
    groups = group_variances(rows)
    assert len(groups) == 1
    assert groups[0].count == 500


def test_scattered_variances_do_not_collapse():
    """A hundred rows short by different amounts is a hundred mistakes, not one
    tier problem -- and grouping them would invent a root cause."""
    rows = [_var("t{}".format(i), 100_000, -(50 * (i + 1))) for i in range(12)]
    assert len(group_variances(rows)) > 3


def test_zero_variances_are_not_groups():
    assert group_variances([_var("a", 100_000, 0)]) == []


def test_variances_in_their_favour_and_ours_are_both_reported():
    """A pack that reports only what we are owed is a negotiating position
    dressed as a reconciliation."""
    rows = ([_var("a{}".format(i), 100_000, -400) for i in range(50)]
            + [_var("b{}".format(i), 100_000, 400) for i in range(50)])
    pack = build_pack(rows, "2026-04")
    assert pack["owed_to_us_minor"] > 0
    assert pack["owed_to_them_minor"] > 0
    assert "owed_to_them" in {g.direction for g in pack["material"]}


def test_immaterial_groups_are_recommended_for_absorption():
    rows = [_var("a", 100_000, -100)]
    pack = build_pack(rows, "2026-04", materiality_minor=5_000)
    assert pack["groups_material"] == 0
    assert pack["immaterial_absorbed_minor"] == 100
    assert "absorbing" in render(pack)


def test_the_materiality_threshold_is_a_parameter():
    """It is a commercial judgement that belongs to finance, not a constant
    buried in a comparison."""
    rows = [_var("a{}".format(i), 100_000, -400) for i in range(10)]
    strict = build_pack(rows, "2026-04", materiality_minor=1_000)
    loose = build_pack(rows, "2026-04", materiality_minor=100_000)
    assert strict["groups_material"] == 1
    assert loose["groups_material"] == 0


def test_the_net_claim_nets():
    rows = ([_var("a{}".format(i), 100_000, -400) for i in range(50)]
            + [_var("b{}".format(i), 100_000, 400) for i in range(30)])
    pack = build_pack(rows, "2026-04")
    assert pack["net_claim_minor"] == (
        pack["owed_to_us_minor"] - pack["owed_to_them_minor"])


# --------------------------------------------------------- break queue
@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    break_queue.install(c)
    return c


class FakeLink:
    def __init__(self, failures=(), divergence=None):
        self._f = list(failures)
        self._d = divergence

    def unposted_breaks(self):
        return [{"ref": r, "break_type": "ledger_unposted",
                 "detail": "GL posting failed", "core_amount": 100,
                 "proc_amount": 0} for r in self._f]

    def reconcile_to_ledger(self, expected_minor, account):
        if self._d is None:
            return {"status": "ok", "break": None}
        return {"status": "BREAK", "break": {
            "ref": "GL:" + account, "break_type": "ledger_divergence",
            "detail": "expected {} got {}".format(expected_minor, self._d),
            "core_amount": expected_minor, "proc_amount": self._d}}


def test_posting_failures_become_queue_items(con):
    """The call that was missing: unposted_breaks() returned a list nobody
    read."""
    res = break_queue.ingest_ledger_feedback(
        con, FakeLink(failures=["r1", "r2"]), "2026-04-06")
    assert res["ingested"] == 2
    assert len(break_queue.aged(con, "2026-04-06")) == 2


def test_a_ledger_divergence_becomes_a_queue_item(con):
    res = break_queue.ingest_ledger_feedback(
        con, FakeLink(divergence=999), "2026-04-06",
        expected_minor=1000, account="cash")
    assert res["ingested"] == 1
    assert res["divergence"]["status"] == "BREAK"


def test_ledger_breaks_start_at_the_top_tier(con):
    """Our own books are wrong, so every report built on them is wrong. That
    does not age into severity, it starts there."""
    break_queue.ingest_ledger_feedback(con, FakeLink(failures=["r1"]),
                                       "2026-04-06")
    item = break_queue.aged(con, "2026-04-06")[0]
    assert item["tier"] == "T3"
    assert item["age_days"] == 0


def test_an_ordinary_break_ages_into_its_tier(con):
    break_queue.upsert(con, {"ref": "x", "break_type": "amount_fee",
                             "detail": "fee short"}, "2026-04-01")
    assert break_queue.aged(con, "2026-04-01")[0]["tier"] == "T0"
    assert break_queue.aged(con, "2026-04-04")[0]["tier"] == "T1"
    assert break_queue.aged(con, "2026-04-14")[0]["tier"] == "T3"


def test_recurrence_does_not_reset_the_age_clock(con):
    """The most common way a break queue fails silently: something unresolved
    for three weeks is forever one day old and never escalates."""
    item = {"ref": "x", "break_type": "amount_fee", "detail": "d"}
    break_queue.upsert(con, item, "2026-04-01")
    break_queue.upsert(con, item, "2026-04-10")
    row = break_queue.aged(con, "2026-04-11")[0]
    assert row["first_seen"].startswith("2026-04-01")
    assert row["age_days"] == 10


def test_the_queue_survives_a_restart(tmp_path):
    """Aging only means something if the item is still there tomorrow."""
    path = tmp_path / "q.db"
    c1 = sqlite3.connect(path)
    break_queue.install(c1)
    break_queue.upsert(c1, {"ref": "x", "break_type": "missing", "detail": "d"},
                       "2026-04-01")
    c1.commit()
    c1.close()

    c2 = sqlite3.connect(path)
    assert len(break_queue.aged(c2, "2026-04-05")) == 1


def test_the_event_trail_is_append_only(con):
    break_queue.upsert(con, {"ref": "x", "break_type": "missing", "detail": "d"},
                       "2026-04-01")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE break_event SET actor='someone else'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM break_event")


def test_resolving_requires_a_reason(con):
    break_queue.upsert(con, {"ref": "x", "break_type": "missing", "detail": "d"},
                       "2026-04-01")
    with pytest.raises(ValueError, match="reason code"):
        break_queue.resolve(con, "x", "missing", "analyst", "  ", "2026-04-02")


def test_a_resolved_break_leaves_the_open_queue(con):
    break_queue.upsert(con, {"ref": "x", "break_type": "missing", "detail": "d"},
                       "2026-04-01")
    break_queue.resolve(con, "x", "missing", "analyst", "processor_error",
                        "2026-04-02")
    assert break_queue.aged(con, "2026-04-03") == []
