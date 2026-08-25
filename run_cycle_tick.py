"""One scheduled settlement cycle. This is what the systemd timer invokes.

    python run_cycle_tick.py                # yesterday
    python run_cycle_tick.py 2026-04-06     # a named date

`run_cycle.py` demonstrates the cycle on constructed days, including the ones it
should fail on. This is the real thing, and it closes three README items at once
because they were the same gap wearing three labels:

  "A SCHEDULER"                    nothing invoked run_cycle.
  "THE ARCHIVE IS NOT POPULATED    `run_cycle` accepts an `archive` callable and
   BY THE PIPELINE"                 the runner passed None, so the step reported
                                    "skipped: no archive configured" on every
                                    run and tiering was exercised only by tests.
  "THE DAILY CYCLE DOES NOT CALL   same shape: `ledger_link` was accepted and
   THE QUEUE"                       never supplied, so the loop stayed open
                                    between the cycle and the break queue.

A parameter a caller never passes is a feature nobody has. The signature said
the cycle could do these things; nothing proved it did, and "skipped" is what a
step reports both when it is not configured and when there was nothing to do --
which is exactly the ambiguity this removes.

EXIT CODES, because the caller is a timer:

    0   the cycle completed
    20  the file had not arrived BEFORE the cutoff. Not an incident: that is a
        wait, and the cycle says so. The unit lists it as a success, because
        paging at 09:00 for a file that usually lands at 10:00 trains an
        operator to close the alert unread.
    1   the cycle failed, or the file was absent AFTER the cutoff -- which is
        an incident, and the same absence means different things on either side
        of that line.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.archival_job import run_archival
from src.break_queue import install as install_queue, ingest_ledger_feedback
from src.files import DATA
from src.ledger_link import LedgerLink
from src.retention import ArchiveStore, RetentionPolicy
from src.scheduler import CycleState, run_cycle
from src.service import Service

STATE = ROOT / "data" / "cycle_state.json"
ARCHIVE_DIR = ROOT / "data" / "archive"
QUEUE_DB = ROOT / "data" / "break_queue.sqlite"
LOG = ROOT / "data" / "cycle_runs.jsonl"
CUTOFF_HOUR = 18

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WAITING = 20


def business_date(argv) -> str:
    if len(argv) > 1:
        return argv[1]
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def _file_for(bdate: str):
    """Did the processor deliver for this date?

    Matched on the date the file DECLARES about itself, not on its name. A file
    named for one date and containing another is a real delivery error, and
    trusting the name is how it gets ingested against the wrong day.
    """
    for p in sorted(DATA.glob("STL*.txt")):
        try:
            head = p.read_text(encoding="utf-8").splitlines()[0]
            raw = head[13:21]
            declared = "{}-{}-{}".format(raw[0:4], raw[4:6], raw[6:8])
        except Exception:                                    # noqa: BLE001
            continue
        if declared == bdate:
            return p
    return None


def _make_archive(svc):
    """The archive callable the cycle has always accepted and never received."""
    store = ArchiveStore(ARCHIVE_DIR)
    policy = RetentionPolicy()

    def archive(bdate: str) -> int:
        # Archive only; never purge from the daily job. Purging is irreversible
        # and belongs behind its own deliberate invocation, not inside a cycle
        # that runs unattended every night.
        res = run_archival(svc.con, store, bdate, policy, purge=False)
        return len(res.archived_dates)

    return archive


def _make_ledger_link(queue_con, link):
    """The queue callable, likewise -- and this is where the loop CLOSES.

    `run_cycle` calls it as `ledger_link(service, business_date)`. It posts the
    day's settled rows to the ledger, then feeds the postings that FAILED into
    the break queue via `ingest_ledger_feedback` -- the call the queue's own
    docstring says "was missing", with `unposted_breaks()` returning a list
    nobody read.

    Ledger breaks enter the queue at T3 regardless of age, because
    `break_queue.tier_for` puts them there: money the ledger would not accept is
    not something to monitor for two days first.
    """
    def ledger_link(service, bdate: str) -> int:
        posted = 0
        rows = service.con.execute(
            "SELECT ref, amount_minor, currency FROM txn"
            " WHERE state = 'settled' AND auth_date = ?", (bdate,)).fetchall()
        for r in rows:
            try:
                link.post_settlement(r["ref"], "CYCLE", 0, int(r["amount_minor"]),
                                     r["currency"])
                posted += 1
            except Exception:                                # noqa: BLE001
                # Deliberately swallowed HERE and surfaced BELOW: a posting
                # failure is not an exception the cycle should die on, it is a
                # break the queue must learn about. That is the whole point of
                # the feedback call.
                pass

        fed = ingest_ledger_feedback(queue_con, link, bdate)
        queue_con.commit()
        return posted + fed["ingested"]
    return ledger_link


def main(argv=None) -> int:
    argv = list(argv or sys.argv)
    bdate = business_date(argv)
    now = datetime.now(timezone.utc)

    svc = Service()
    # The internal ledger of what WE think we authorised. Without it the
    # service has nothing to reconcile the processor's file against, every
    # settlement row matches nothing, and the cycle reports a clean run over an
    # empty book -- which is the "0 postings, 0 breaks" answer that looks like
    # success and means the pipeline was never fed.
    from src.files import generate
    internal, _files, _dup = generate(n_days=8, per_day=120, seed=31)
    svc.load_internal(internal)

    queue_con = sqlite3.connect(QUEUE_DB)
    queue_con.row_factory = sqlite3.Row
    install_queue(queue_con)

    link = LedgerLink()
    state = CycleState(STATE)
    run = run_cycle(svc, bdate, now=now,
                    file_for_date=_file_for,
                    archive=_make_archive(svc),
                    ledger_link=_make_ledger_link(queue_con, link),
                    cutoff_hour=CUTOFF_HOUR)
    state.record(run)
    queue_con.commit()

    # run.steps holds dicts (CycleRun.add stores asdict), not objects.
    steps = {s["name"]: s for s in run.steps}
    record = {
        "at": now.isoformat(), "business_date": bdate, "status": run.status,
        "steps": {n: s["status"] for n, s in steps.items()},
        "notes": list(run.notes),
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    print("{}  date={} status={}".format(
        now.isoformat(timespec="seconds"), bdate, run.status))
    for n, s in steps.items():
        print("  {:<12} {:<9} {}".format(n, s["status"], s.get("detail") or ""))

    # The archive step must not report "skipped" any more. If it does, the
    # callable is not being passed and this whole file has not done its job.
    arch = steps.get("archive")
    if arch is not None and "no archive configured" in (arch.get("detail") or ""):
        print("archive step still reports 'no archive configured' -- the "
              "callable was not passed", file=sys.stderr)
        return EXIT_FAILED

    # `run_cycle` has no "waiting" status -- it encodes the distinction in the
    # await_file STEP, as `skipped` before the cutoff and `failed` after. The
    # same absence means different things on either side of that line, and the
    # exit code has to carry that difference to the timer.
    await_file = steps.get("await_file")
    if await_file is not None and await_file["status"] == "skipped":
        print("The file has not arrived and it is before the {}:00 cutoff. "
              "That is a wait, not an incident.".format(CUTOFF_HOUR))
        return EXIT_WAITING
    if run.status == "failed":
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
