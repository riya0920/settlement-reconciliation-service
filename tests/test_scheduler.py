"""The daily cycle: catch-up, cutoffs, and the two idempotency guards."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.scheduler import (CycleState, business_days, catch_up, render,
                           run_cycle)
from src.service import ConflictingRedelivery, DuplicateFile

NOW_EARLY = datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc)
NOW_LATE = datetime(2026, 4, 6, 19, 0, tzinfo=timezone.utc)


class FakeService:
    """Enough of `Service` to drive the cycle, with a settable ingest outcome."""

    def __init__(self, outcome=None):
        self.ingested = []
        self.outcome = outcome

    def ingest(self, path):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        self.ingested.append(path)
        return {"applied": 10}

    def aged_breaks(self, as_of):
        return [{"tier": "T1"}, {"tier": "T0"}]

    def fee_variance_summary(self):
        return {"count": 2, "total_minor": 500}


def _file_for(mapping):
    return lambda d: mapping.get(d)


# ------------------------------------------------------------- calendar
def test_weekends_are_not_business_days():
    """A cycle that expects a file on Saturday raises a missing-file alert every
    week until the team learns to ignore it -- and that habit is the damage."""
    days = business_days(date(2026, 4, 3), date(2026, 4, 6))   # Fri..Mon
    assert [d.isoformat() for d in days] == ["2026-04-03", "2026-04-06"]


# --------------------------------------------------------- file arrival
def test_a_missing_file_before_the_cutoff_is_a_wait():
    run = run_cycle(FakeService(), "2026-04-06", now=NOW_EARLY,
                    file_for_date=_file_for({}), cutoff_hour=18)
    assert run.steps[0]["status"] == "skipped"
    assert run.status == "ok"


def test_a_missing_file_after_the_cutoff_is_an_incident():
    run = run_cycle(FakeService(), "2026-04-06", now=NOW_LATE,
                    file_for_date=_file_for({}), cutoff_hour=18)
    assert run.steps[0]["status"] == "failed"
    assert run.status == "failed"


def test_a_missing_file_is_recorded_rather_than_raising():
    """'The file did not arrive' and 'the job did not run' are the same silence
    at the time and different incidents afterwards."""
    run = run_cycle(FakeService(), "2026-04-06", now=NOW_LATE,
                    file_for_date=_file_for({}))
    assert any("did not run" in n for n in run.notes)


# --------------------------------------------------------------- ingest
def test_a_full_day_runs_every_step(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    run = run_cycle(FakeService(), "2026-04-06", now=NOW_LATE,
                    file_for_date=_file_for({"2026-04-06": p}))
    names = [s["name"] for s in run.steps]
    assert names[:4] == ["ingest", "age_breaks", "fee_variance", "post_to_ledger"]
    assert run.status == "ok"


def test_an_identical_redelivery_is_skipped_not_failed(tmp_path):
    """Processors resend files routinely. Treating that as an incident produces
    a daily alert that means nothing."""
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    svc = FakeService(outcome=DuplicateFile("already ingested, identical"))
    run = run_cycle(svc, "2026-04-06", now=NOW_LATE,
                    file_for_date=_file_for({"2026-04-06": p}))
    assert run.steps[0]["status"] == "skipped"
    assert run.status == "ok"


def test_a_conflicting_redelivery_fails(tmp_path):
    """Same id, different content: the processor is either correcting or
    duplicating and the system must not guess."""
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    svc = FakeService(outcome=ConflictingRedelivery("same id, different bytes"))
    run = run_cycle(svc, "2026-04-06", now=NOW_LATE,
                    file_for_date=_file_for({"2026-04-06": p}))
    assert run.status == "failed"


def test_a_rejected_file_stops_before_the_ledger(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    svc = FakeService(outcome=ValueError("control totals do not tie"))
    run = run_cycle(svc, "2026-04-06", now=NOW_LATE,
                    file_for_date=_file_for({"2026-04-06": p}))
    assert [s["name"] for s in run.steps] == ["ingest"]
    assert run.status == "failed"


# ------------------------------------------------------------ catch-up
def _three_days(tmp_path):
    mapping = {}
    for d in ("2026-04-06", "2026-04-07", "2026-04-08"):
        p = tmp_path / (d + ".txt")
        p.write_text(d, encoding="utf-8")
        mapping[d] = p
    return mapping


def test_catch_up_runs_missed_dates_oldest_first(tmp_path):
    """Settlement state is cumulative. A later day applied before an earlier one
    transitions a book that has not received the earlier day's rows."""
    state = CycleState(tmp_path / "state.json")
    runs = catch_up(FakeService(), state, date(2026, 4, 6), date(2026, 4, 8),
                    now=NOW_LATE, file_for_date=_file_for(_three_days(tmp_path)))
    assert [r.business_date for r in runs] == [
        "2026-04-06", "2026-04-07", "2026-04-08"]


def test_catch_up_stops_at_a_failure_rather_than_stepping_over_it(tmp_path):
    mapping = _three_days(tmp_path)
    del mapping["2026-04-07"]
    state = CycleState(tmp_path / "state.json")
    runs = catch_up(FakeService(), state, date(2026, 4, 6), date(2026, 4, 8),
                    now=NOW_LATE, file_for_date=_file_for(mapping))
    assert [r.business_date for r in runs] == ["2026-04-06", "2026-04-07"]
    assert runs[-1].status == "failed"


def test_a_completed_date_is_not_re_run(tmp_path):
    mapping = _three_days(tmp_path)
    state = CycleState(tmp_path / "state.json")
    svc = FakeService()
    catch_up(svc, state, date(2026, 4, 6), date(2026, 4, 8), now=NOW_LATE,
             file_for_date=_file_for(mapping))
    first_pass = len(svc.ingested)
    catch_up(svc, state, date(2026, 4, 6), date(2026, 4, 8), now=NOW_LATE,
             file_for_date=_file_for(mapping))
    assert len(svc.ingested) == first_pass


def test_completed_dates_survive_a_restart(tmp_path):
    """The state is the point: an in-memory record is reborn empty every night
    and the cycle re-applies the whole week."""
    path = tmp_path / "state.json"
    mapping = _three_days(tmp_path)
    catch_up(FakeService(), CycleState(path), date(2026, 4, 6), date(2026, 4, 8),
             now=NOW_LATE, file_for_date=_file_for(mapping))
    assert len(CycleState(path).completed) == 3


def test_a_failed_date_is_not_marked_complete(tmp_path):
    state = CycleState(tmp_path / "state.json")
    catch_up(FakeService(), state, date(2026, 4, 6), date(2026, 4, 6),
             now=NOW_LATE, file_for_date=_file_for({}))
    assert state.completed == set()


def test_every_run_is_recorded_including_the_failures(tmp_path):
    path = tmp_path / "state.json"
    state = CycleState(path)
    catch_up(FakeService(), state, date(2026, 4, 6), date(2026, 4, 6),
             now=NOW_LATE, file_for_date=_file_for({}))
    assert len(CycleState(path)._data["runs"]) == 1


def test_render_shows_each_step(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x", encoding="utf-8")
    text = render([run_cycle(FakeService(), "2026-04-06", now=NOW_LATE,
                             file_for_date=_file_for({"2026-04-06": p}))])
    assert "ingest" in text and "fee_variance" in text
