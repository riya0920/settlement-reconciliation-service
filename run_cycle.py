"""The daily settlement cycle, run over a month including days it should fail on.

    python run_cycle.py

A schedulable job is only worth having if it does the right thing on the days
nobody plans for: the day the file does not arrive, the day the box was down and
three dates are outstanding, and the weekend when no file is expected at all.
This runs all three deliberately.
"""
from __future__ import annotations

import shutil
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.files import generate
from src.scheduler import CycleState, business_days, catch_up, render, run_cycle
from src.service import Service

DATA = ROOT / "data" / "cycle"
STATE = ROOT / "data" / "cycle_state.json"


def _header_date(path: Path) -> str:
    """Business date from the file header. The date on the OUTSIDE of a file is
    a guess; the date it declares about itself is the fact."""
    head = path.read_text(encoding="utf-8").splitlines()[0]
    raw = head[13:21]
    return "{}-{}-{}".format(raw[0:4], raw[4:6], raw[6:8])


def _prepare():
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        STATE.unlink()

    internal, files, _dup = generate(n_days=8, per_day=120, seed=31)
    paths = {}
    good = [f for f in files if f.stem.startswith("STL")]
    for i, src in enumerate(sorted(good)):
        bdate = _header_date(src)
        # The file for the third business day never arrives. That is the case
        # the cycle exists to REPORT rather than crash on.
        if i == 2:
            continue
        dst = DATA / src.name
        shutil.copy(src, dst)
        paths[bdate] = dst
    return internal, paths


def main() -> int:
    internal, paths = _prepare()
    svc = Service()
    svc.load_internal(internal)
    state = CycleState(STATE)

    dates = sorted(paths) or []
    all_dates = sorted(set(dates) | {d for d in paths})
    start = date.fromisoformat(min(all_dates))
    end = date.fromisoformat(max(all_dates))

    print("=" * 78)
    print("DAILY SETTLEMENT CYCLE")
    print("=" * 78)
    print("business dates in scope : {} .. {}".format(start, end))
    print("weekday dates           : {}".format(len(business_days(start, end))))
    print("files delivered         : {}".format(len(paths)))
    print()
    print("The cycle takes `now` as a PARAMETER rather than reading the clock.")
    print("A job that reads the system clock cannot be tested for what it does")
    print("on a Sunday, at a month end, or when it starts late -- and those are")
    print("the three cases that break it.")

    def file_for(bdate):
        return paths.get(bdate)

    # ------------------------------------------------------- normal day
    print("\n" + "=" * 78)
    print("1. A NORMAL DAY")
    print("-" * 78)
    first = min(paths)
    run = run_cycle(svc, first, now=datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc),
                    file_for_date=file_for)
    state.record(run)
    print(render([run]))

    # -------------------------------------------------- file not arrived
    print("\n" + "=" * 78)
    print("2. THE FILE HAS NOT ARRIVED")
    print("-" * 78)
    missing_date = (date.fromisoformat(first)).isoformat()
    absent = sorted(set(business_days(start, end)) -
                    {date.fromisoformat(d) for d in paths})
    target = absent[0].isoformat() if absent else "2026-04-15"

    early = run_cycle(svc, target,
                      now=datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc),
                      file_for_date=file_for, cutoff_hour=18)
    late = run_cycle(svc, target,
                     now=datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc),
                     file_for_date=file_for, cutoff_hour=18)
    print(render([early, late]))
    print()
    print("Same absent file, two different verdicts, and the only difference is")
    print("the time. Before the cutoff a missing file is a wait; after it, it is")
    print("an incident. A job that raises at 09:00 because the file usually")
    print("arrives at 10:00 trains its operators to close the alert unread.")

    # -------------------------------------------------------- catch-up
    print("\n" + "=" * 78)
    print("3. THE BOX WAS DOWN -- CATCH UP, OLDEST FIRST")
    print("-" * 78)
    outstanding = state.missed(end, start)
    print("dates outstanding: {}".format(len(outstanding)))
    runs = catch_up(svc, state, start, end,
                    now=datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc),
                    file_for_date=file_for)
    print(render(runs))

    print("\n" + "-" * 78)
    print("completed dates on record : {}".format(len(state.completed)))
    print("runs recorded             : {}".format(len(runs) + 3))
    print()
    print("The catch-up stops at the missing file rather than stepping over it.")
    print("Settlement state is cumulative: a later day applied before an earlier")
    print("one transitions a book that has not received the earlier day's rows,")
    print("and the result is a set of states that no sequence of events could")
    print("have produced.")

    # --------------------------------------------------------- idempotency
    print("\n" + "=" * 78)
    print("4. RUNNING A COMPLETED DATE AGAIN -- TWO INDEPENDENT GUARDS")
    print("-" * 78)
    completed = sorted(state.completed)
    print("dates already complete: {}".format(", ".join(completed) or "none"))

    before = svc.state_counts()
    again = catch_up(svc, state, start, end,
                     now=datetime(2026, 4, 2, 19, 0, tzinfo=timezone.utc),
                     file_for_date=file_for)
    print()
    print("GUARD 1 -- the cycle skips what is on record.")
    print("   dates the catch-up attempted : {}".format(
        ", ".join(r.business_date for r in again) or "none"))
    print("   completed dates re-attempted : {}".format(
        len([r for r in again if r.business_date in completed])))

    print()
    print("GUARD 2 -- and if something calls it anyway, ingestion refuses.")
    replay = run_cycle(svc, completed[0],
                       now=datetime(2026, 4, 2, 19, 0, tzinfo=timezone.utc),
                       file_for_date=file_for)
    ingest_step = next((st for st in replay.steps if st["name"] == "ingest"), {})
    print("   forced re-run of {} -> ingest {}: {}".format(
        completed[0], ingest_step.get("status"),
        (ingest_step.get("detail") or "")[:44]))

    after = svc.state_counts()
    print()
    print("state counts before : {}".format(before))
    print("state counts after  : {}".format(after))
    print("IDENTICAL           : {}".format(before == after))
    print()
    print("Two guards rather than one, because they fail differently. The state")
    print("file is bookkeeping and can be deleted, restored from a stale backup,")
    print("or simply not consulted by whatever fired the job. File-level")
    print("idempotency lives in the service and holds regardless.")
    print()
    print("This project learned the lesson the expensive way: an earlier replay")
    print("kept the ingested-file ledger and passed a flag that skipped duplicate")
    print("detection, so a redelivered file was applied twice and the lifecycle")
    print("counts silently doubled. A scheduler that fires twice is not an exotic")
    print("failure -- it is a retry.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
