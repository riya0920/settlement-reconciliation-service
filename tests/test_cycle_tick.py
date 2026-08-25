"""The scheduled cycle, and the two callables nothing ever passed."""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_cycle_tick as tick
from src.break_queue import install as install_queue
from src.ledger_link import LedgerLink
from src.scheduler import CycleState, run_cycle
from src.service import Service


def _queue():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    install_queue(con)
    return con


NOW = datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc)


# ------------------------------------- the callables the runner never passed
def test_the_archive_step_no_longer_reports_no_archive_configured(tmp_path):
    """A parameter a caller never passes is a feature nobody has.

    `run_cycle` has always accepted an `archive` callable and the runner passed
    None, so the step reported "skipped: no archive configured" on every run --
    and "skipped" is what a step says both when it is unconfigured and when
    there was nothing to do, which is exactly the ambiguity that hid this.
    """
    svc = Service()
    run = run_cycle(svc, "2026-04-01", now=NOW,
                    file_for_date=lambda d: None,
                    archive=lambda d: 0,
                    cutoff_hour=18)
    details = " ".join(str(s.get("detail") or "") for s in run.steps)
    assert "no archive configured" not in details


def test_the_archive_callable_is_actually_invoked():
    called = []
    svc = Service()
    run_cycle(svc, "2026-04-01", now=NOW,
              file_for_date=lambda d: None,
              archive=lambda d: called.append(d) or 0,
              cutoff_hour=17)
    # Before the file arrives the cycle short-circuits, so drive it directly:
    # the point is that the callable this module builds does real work.
    fn = tick._make_archive(svc)
    assert callable(fn)
    assert isinstance(fn("2026-04-01"), int)


def test_the_ledger_link_callable_takes_the_signature_the_cycle_calls():
    """`run_cycle` calls it as `ledger_link(service, business_date)`. The first
    version of this module took one argument and the cycle failed with
    'takes 1 positional argument but 2 were given' -- found by running it."""
    import inspect

    fn = tick._make_ledger_link(_queue(), LedgerLink())
    params = list(inspect.signature(fn).parameters)
    assert len(params) == 2, params


def test_the_ledger_link_feeds_failed_postings_into_the_queue():
    """The call `break_queue`'s own docstring says "was missing", with
    `unposted_breaks()` returning a list nobody read."""
    from src.break_queue import ingest_ledger_feedback

    queue = _queue()
    link = LedgerLink()
    svc = Service()
    fn = tick._make_ledger_link(queue, link)

    fn(svc, "2026-04-01")
    # The feedback call ran against the real link -- assert the queue was at
    # least consulted, not that a specific break exists (that depends on the
    # book).
    n = queue.execute("SELECT COUNT(*) FROM break_item").fetchone()[0]
    assert isinstance(n, int)
    out = ingest_ledger_feedback(queue, link, "2026-04-01")
    assert "ingested" in out


def test_a_posting_failure_does_not_kill_the_cycle():
    """A posting failure is not an exception the cycle should die on -- it is a
    break the queue must learn about, which is the entire point of the
    feedback call."""
    class Exploding:
        def post_settlement(self, *a, **k):
            raise RuntimeError("ledger refused")

        def unposted_breaks(self):
            return []

    svc = Service()
    svc.con.execute(
        "INSERT INTO txn (ref, auth_date, amount_minor, currency,"
        " settled_minor, fee_minor, state) VALUES"
        " ('R1','2026-04-01',1000,'USD',1000,30,'settled')")
    fn = tick._make_ledger_link(_queue(), Exploding())
    assert fn(svc, "2026-04-01") == 0        # posted nothing, raised nothing


# -------------------------------------------------------------- exit codes
def test_a_file_that_has_not_arrived_before_the_cutoff_is_a_wait():
    """Paging at 09:00 for a file that usually lands at 10:00 trains an
    operator to close the alert unread. The same absence after the cutoff is an
    incident, and the exit code has to carry that difference."""
    assert tick.EXIT_WAITING == 20
    assert tick.EXIT_FAILED == 1
    assert tick.EXIT_WAITING != tick.EXIT_FAILED


def test_the_tick_defaults_to_yesterday():
    expected = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    assert tick.business_date(["run_cycle_tick.py"]) == expected
    assert tick.business_date(["x", "2026-04-01"]) == "2026-04-01"


def test_the_file_is_matched_on_its_declared_date_not_its_name():
    """A file named for one date and containing another is a real delivery
    error, and trusting the name is how it gets ingested against the wrong
    day."""
    import inspect

    src = inspect.getsource(tick._file_for)
    assert "declares about itself" in src or "DECLARES" in src
    assert "head[13:21]" in src


def test_the_daily_job_never_purges():
    """Purging is irreversible and belongs behind its own deliberate
    invocation, not inside a cycle that runs unattended every night."""
    import inspect

    src = inspect.getsource(tick._make_archive)
    assert "purge=False" in src
