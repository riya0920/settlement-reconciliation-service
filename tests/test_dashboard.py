"""The operator view, and the thresholds it must not re-type."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.break_queue import TIERS, tier_for
from src.dashboard import (Alert, break_alerts, build, deadline_alerts,
                           fee_variance_alerts, render_html)
from src.dispute_pack import DEFAULT_MATERIALITY_MINOR

AS_OF = "2026-05-05"


def _brk(ref, age, btype="amount_mismatch"):
    return {"ref": ref, "age_days": age, "break_type": btype}


def _cb(ref, due, amount=50_000, code="fraud", state="received"):
    return {"ref": ref, "evidence_due_on": due, "amount_minor": amount,
            "reason_code": code, "state": state}


# ------------------------------------- the constraint the whole module is about
def test_the_dashboard_uses_the_queue_s_own_tier_function():
    """Not a re-implementation. A tier boundary that moves in `break_queue`
    must move here on the same commit, and the only way to guarantee that is to
    call the same function."""
    import inspect

    import src.dashboard as dash

    src = inspect.getsource(dash)
    assert "from .break_queue import TIERS, tier_for" in src
    assert "from .dispute_pack import DEFAULT_MATERIALITY_MINOR" in src
    # And no hand-written day thresholds anywhere in the module.
    for t in TIERS:
        assert "age_days >= {}".format(t.min_days) not in src, (
            "a tier boundary was re-typed into the dashboard")


def test_materiality_is_the_dispute_pack_s_number():
    """If the dashboard used a different floor, the screen would show variances
    the pack silently drops -- or stay quiet about ones it raises."""
    view = build([], {"rows": 1, "total_variance_minor": 1}, [], AS_OF)
    assert view["materiality_minor"] == DEFAULT_MATERIALITY_MINOR


def test_moving_a_tier_boundary_moves_the_alert(monkeypatch):
    """The property the import buys, demonstrated rather than asserted."""
    breaks = [_brk("a", 6)]
    before = break_alerts(breaks)
    assert any("T2" in a.detail for a in before)

    import src.break_queue as bq
    patched = [bq.Tier("T0", 0, "monitor"), bq.Tier("T1", 2, "assign"),
               bq.Tier("T2", 99, "escalate"), bq.Tier("T3", 100, "controller")]
    monkeypatch.setattr(bq, "TIERS", patched)
    monkeypatch.setattr("src.dashboard.TIERS", patched)
    after = break_alerts(breaks)
    assert not any("T2" in a.detail for a in after), (
        "the dashboard kept the old boundary after the queue's moved")


# ------------------------------------------------------------ break alerts
def test_a_t3_break_pages():
    alerts = break_alerts([_brk("a", 12)])
    assert alerts and alerts[0].severity == "page"
    assert "T3" in alerts[0].detail


def test_a_t0_break_raises_nothing():
    """Totals are not actionable. A break inside its monitoring window is
    context, not work."""
    assert break_alerts([_brk("a", 0), _brk("b", 1)]) == []


def test_alerts_are_ordered_worst_first():
    alerts = break_alerts([_brk("a", 12), _brk("b", 6), _brk("c", 3)])
    assert [a.severity for a in alerts] == ["page", "ticket", "info"]


def test_every_alert_carries_an_action():
    """An alert with no action is noise. The action comes from the tier that
    raised it, so the row says what to do rather than only that something is
    wrong."""
    for a in break_alerts([_brk("a", 12), _brk("b", 6), _brk("c", 3)]):
        assert a.action and a.action.strip()


def test_the_action_is_the_tiers_own_action():
    a = break_alerts([_brk("x", 12)])[0]
    assert a.action == TIERS[-1].action


def test_a_ledger_break_goes_straight_to_the_top_tier():
    """`break_queue` starts ledger breaks at T3 regardless of age, and the
    dashboard inherits that by calling `tier_for` rather than bucketing on age
    itself."""
    a = break_alerts([_brk("l", 0, "ledger_divergence")])
    assert a and a[0].severity == "page"


# ------------------------------------------------------ fee variance alerts
def test_variance_below_the_floor_is_silent():
    assert fee_variance_alerts(
        {"rows": 3, "total_variance_minor": DEFAULT_MATERIALITY_MINOR - 1}) == []


def test_variance_at_the_floor_fires():
    assert fee_variance_alerts(
        {"rows": 3, "total_variance_minor": DEFAULT_MATERIALITY_MINOR})


def test_a_large_variance_pages_rather_than_tickets():
    small = fee_variance_alerts({"rows": 3,
                                 "total_variance_minor": DEFAULT_MATERIALITY_MINOR * 2})
    large = fee_variance_alerts({"rows": 3,
                                 "total_variance_minor": DEFAULT_MATERIALITY_MINOR * 50})
    assert small[0].severity == "ticket" and large[0].severity == "page"


def test_a_negative_variance_is_material_too():
    """Being over-charged and under-charged are both wrong. Taking the absolute
    value is the decision, and it is tested rather than incidental."""
    assert fee_variance_alerts(
        {"rows": 2, "total_variance_minor": -DEFAULT_MATERIALITY_MINOR * 3})


def test_no_rows_means_no_alert():
    assert fee_variance_alerts({"rows": 0, "total_variance_minor": 0}) == []


# --------------------------------------------------------- deadline alerts
def test_an_overdue_dispute_pages_and_says_it_is_already_lost():
    a = deadline_alerts([_cb("a", "2026-05-01")], AS_OF)
    assert a[0].severity == "page"
    assert "already lost" in a[0].action


def test_a_dispute_due_today_pages():
    a = deadline_alerts([_cb("a", AS_OF)], AS_OF)
    assert a[0].severity == "page" and "today" in a[0].detail


def test_a_dispute_due_next_week_is_silent():
    assert deadline_alerts([_cb("a", "2026-05-20")], AS_OF) == []


def test_a_closed_dispute_raises_nothing():
    assert deadline_alerts([_cb("a", "2026-05-01", state="won")], AS_OF) == []


# ---------------------------------------------------------------- the view
def test_deadlines_beat_amounts_in_the_queue():
    """A small dispute due today outranks a large one due next week, because
    the second can still be fought. Same ordering as
    `representment.work_queue`."""
    view = build([], {"rows": 0, "total_variance_minor": 0},
                 [_cb("big", "2026-05-20", 900_000),
                  _cb("small", "2026-05-06", 1_000)], AS_OF)
    assert [c["ref"] for c in view["chargebacks"]] == ["small", "big"]


def test_breaks_are_shown_oldest_first():
    view = build([_brk("young", 1), _brk("old", 40)],
                 {"rows": 0, "total_variance_minor": 0}, [], AS_OF)
    assert view["breaks"][0]["ref"] == "old"


def test_pages_are_counted_for_the_top_of_the_screen():
    view = build([_brk("a", 12)], {"rows": 0, "total_variance_minor": 0},
                 [_cb("b", "2026-05-01")], AS_OF)
    assert view["pages"] == 2


def test_a_clean_book_produces_no_alerts():
    view = build([_brk("a", 0)], {"rows": 0, "total_variance_minor": 0},
                 [_cb("b", "2026-06-30")], AS_OF)
    assert view["alerts"] == []


# --------------------------------------------------------------- rendering
def test_the_page_is_self_contained():
    """A settlement dashboard is looked at during an incident, and an incident
    is exactly when a page that fetches a chart library from the internet does
    not load."""
    view = build([_brk("a", 12)], {"rows": 2, "total_variance_minor": 90_000},
                 [_cb("b", "2026-05-01")], AS_OF)
    page = render_html(view)
    for forbidden in ("http://", "https://", "<script", "cdn."):
        assert forbidden not in page, "the page reaches outside itself"


def test_the_page_renders_the_alerts_and_their_actions():
    view = build([_brk("a", 12)], {"rows": 0, "total_variance_minor": 0},
                 [], AS_OF)
    page = render_html(view)
    assert "page" in page and TIERS[-1].action in page


def test_a_clean_book_says_so_rather_than_showing_an_empty_box():
    """An empty alert panel and a healthy one look identical, and the reader
    has to guess which they are looking at."""
    view = build([], {"rows": 0, "total_variance_minor": 0}, [], AS_OF)
    page = render_html(view)
    assert "Nothing is past a threshold" in page


def test_values_are_escaped():
    """A break ref comes from a settlement file, which is input from outside."""
    view = build([_brk("<script>alert(1)</script>", 12)],
                 {"rows": 0, "total_variance_minor": 0}, [], AS_OF)
    page = render_html(view)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_page_carries_both_themes():
    page = render_html(build([], {"rows": 0, "total_variance_minor": 0}, [],
                             AS_OF))
    assert "prefers-color-scheme:dark" in page


# ----------------------------------------- the silent zero, and its refusal
def test_a_break_with_no_age_raises_rather_than_reading_as_new():
    """The bug this module shipped with, pinned so it cannot return.

    The dashboard read `age_days` and `break_type`; `Service.aged_breaks`
    returns `age` and `state`. Every one of 625 breaks -- some 34 days old --
    defaulted to age 0, landed in T0, and raised nothing. The page showed "625
    open breaks" and "nothing needs action" on the same screen.

    Zero is the one value that means "everything is fine", so it is the one
    value a missing field must never become.
    """
    from src.dashboard import MissingAge

    with pytest.raises(MissingAge, match="Refusing to default"):
        break_alerts([{"ref": "a", "state": "pending"}])


def test_the_service_s_own_field_names_are_accepted():
    """`Service.aged_breaks` is the actual producer, so its shape is the one
    that has to work."""
    from src.service import Service

    svc = Service()
    rows = svc.aged_breaks("2026-05-05")
    assert isinstance(rows, list)

    service_shaped = [{"ref": "a", "age": 34, "state": "pending"}]
    alerts = break_alerts(service_shaped)
    assert alerts and alerts[0].severity == "page", (
        "a 34-day-old break must reach T3 through the service's own field names")


def test_both_field_spellings_agree():
    queue_shaped = [{"ref": "a", "age_days": 12, "break_type": "amount_mismatch"}]
    service_shaped = [{"ref": "a", "age": 12, "state": "pending"}]
    assert [a.severity for a in break_alerts(queue_shaped)] == \
           [a.severity for a in break_alerts(service_shaped)]


def test_an_unknown_break_type_ages_normally_rather_than_raising():
    """The type may default where the age may not, and the asymmetry is the
    point: an unknown type misses the ledger fast-path and ages normally, where
    an unknown age silently ages nothing."""
    alerts = break_alerts([{"ref": "a", "age": 12}])
    assert alerts and alerts[0].severity == "page"


def test_the_rendered_page_shows_the_real_ages():
    view = build([{"ref": "a", "age": 34, "state": "pending"}],
                 {"rows": 0, "total_variance_minor": 0}, [], AS_OF)
    page = render_html(view)
    assert ">34<" in page, "the page rendered an age of 0 for a 34-day break"
