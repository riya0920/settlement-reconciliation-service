"""The daily settlement cycle, as a schedulable job with a clock it does not own.

Everything in this project was reachable only by hand: files were ingested by a
script, the break queue was aged when someone asked, retention was exercised by
tests. That is the gap the README called the biggest one, and it is a real gap --
a control that runs when somebody remembers is not a control.

FOUR THINGS THAT MAKE A DAILY JOB SAFE, and what each prevents.

  CATCH-UP, OLDEST FIRST   the job is asked to run FOR a business date, not for
                           "today", and a run that was missed is not skipped. A
                           settlement cycle that only ever processes today
                           silently drops the day the box was down, and nobody
                           notices until a month-end that does not tie.

  IDEMPOTENT PER DATE      running the same date twice must not double-apply.
                           This project already learned that lesson the hard way
                           -- `reset_for_replay` exists because a replay kept the
                           ingested-file ledger and applied a redelivered file
                           twice -- so the cycle records completed dates and
                           refuses to redo one without `force`.

  A CUTOFF, NOT A CLOCK    the job takes `now` as a parameter. A cycle that reads
                           the system clock cannot be tested for what it does on
                           a Sunday, on a month end, or when it starts late, and
                           those are the three cases that break it.

  A RECORDED OUTCOME       every run appends what it did, so "the file did not
                           arrive" and "the job did not run" are distinguishable
                           after the fact. They are the same silence at the time
                           and completely different incidents.

WHY THIS IS NOT A SCHEDULER. It does not schedule itself, and that is deliberate
in the same way DATA-1's is: something outside has to call `run_cycle` at the
cutoff, and that something is cron, Airflow or a systemd timer, where the state
is visible to an operator. A scheduler embedded in the application is one nobody
can inspect, pause, or back-fill from.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .service import ConflictingRedelivery, DuplicateFile


@dataclass
class StepResult:
    name: str
    status: str                      # ok | skipped | failed
    detail: str = ""
    metrics: dict = field(default_factory=dict)


@dataclass
class CycleRun:
    business_date: str
    started_at: str
    steps: list = field(default_factory=list)
    status: str = "ok"
    notes: list = field(default_factory=list)

    def add(self, step: StepResult) -> "CycleRun":
        self.steps.append(asdict(step))
        if step.status == "failed":
            self.status = "failed"
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def business_days(start: date, end: date) -> list[date]:
    """Calendar weekdays. Settlement files do not arrive at the weekend, and a
    cycle that expects one on Saturday raises a missing-file alert every week
    until the team learns to ignore the alert -- which is the real damage."""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class CycleState:
    """Which dates have completed. Append-only on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data = {"completed": [], "runs": []}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def completed(self) -> set:
        return set(self._data["completed"])

    def record(self, run: CycleRun) -> None:
        if run.status == "ok" and run.business_date not in self._data["completed"]:
            self._data["completed"].append(run.business_date)
        self._data["runs"].append(json.loads(run.to_json()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=1), encoding="utf-8")

    def missed(self, through: date, since: date) -> list[date]:
        done = self.completed
        return [d for d in business_days(since, through)
                if d.isoformat() not in done]


def run_cycle(service, business_date: str, *, now: datetime,
              file_for_date=None, archive=None, ledger_link=None,
              cutoff_hour: int = 18) -> CycleRun:
    """One settlement day, end to end.

    `file_for_date(business_date) -> Path | None` is how the caller says whether
    the processor delivered. Returning None is a first-class outcome, not an
    error: a file that has not arrived is an operational fact the cycle has to
    report rather than crash on.
    """
    run = CycleRun(business_date=business_date, started_at=now.isoformat())

    # ---------------------------------------------------------- 1. arrival
    path = file_for_date(business_date) if file_for_date else None
    if path is None:
        late = now.hour >= cutoff_hour
        run.add(StepResult(
            "await_file", "failed" if late else "skipped",
            ("file has not arrived and the {}:00 cutoff has passed"
             if late else
             "file has not arrived; still inside the {}:00 window"
             ).format(cutoff_hour)))
        run.notes.append(
            "A missing file and a job that did not run are the same silence at "
            "the time and different incidents afterwards. This run exists in "
            "the log so the difference survives.")
        return run

    # ---------------------------------------------------------- 2. ingest
    try:
        res = service.ingest(path)
        run.add(StepResult("ingest", "ok", str(path.name),
                           {"rows": res.get("applied", res.get("rows", 0))}))
    except DuplicateFile as exc:
        # A byte-identical redelivery is a NO-OP, not a failure. Processors
        # resend files routinely; treating that as an incident produces a daily
        # alert that means nothing, and an alert that means nothing is how the
        # one that matters gets closed unread.
        run.add(StepResult("ingest", "skipped", str(exc)))
        run.notes.append(
            "Identical redelivery. The state file said this date was done and "
            "the service said so independently -- two guards that fail "
            "differently, because a state file can be deleted or restored "
            "stale and file-level idempotency cannot.")
        return run
    except ConflictingRedelivery as exc:
        # Same file id, DIFFERENT content. The processor is either correcting or
        # duplicating and the system must not guess.
        run.add(StepResult("ingest", "failed", str(exc)))
        run.notes.append(
            "Same file id with different content is a question for the "
            "processor, not a merge.")
        return run
    except Exception as exc:                                  # noqa: BLE001
        run.add(StepResult("ingest", "failed", str(exc)))
        run.notes.append(
            "Ingestion is gated on control totals, so a rejected file stops the "
            "cycle here rather than letting a partial day reach the ledger.")
        return run

    # ------------------------------------------------------ 3. break aging
    aged = service.aged_breaks(business_date)
    tiers: dict = {}
    for b in aged:
        tiers[b.get("tier", "T0")] = tiers.get(b.get("tier", "T0"), 0) + 1
    run.add(StepResult("age_breaks", "ok",
                       "{} open breaks".format(len(aged)), tiers))

    # ------------------------------------------------------ 4. fee variance
    fees = service.fee_variance_summary()
    run.add(StepResult("fee_variance", "ok",
                       "{} variances".format(fees.get("count", 0)), fees))

    # ------------------------------------------------------- 5. post to GL
    if ledger_link is not None:
        try:
            posted = ledger_link(service, business_date)
            run.add(StepResult("post_to_ledger", "ok",
                               "{} postings".format(posted), {"postings": posted}))
        except Exception as exc:                              # noqa: BLE001
            run.add(StepResult("post_to_ledger", "failed", str(exc)))
            return run
    else:
        run.add(StepResult("post_to_ledger", "skipped", "no ledger configured"))

    # --------------------------------------------------------- 6. retention
    if archive is not None:
        moved = archive(business_date)
        run.add(StepResult("archive", "ok", "{} files tiered".format(moved),
                           {"files": moved}))
    else:
        run.add(StepResult("archive", "skipped", "no archive configured"))

    return run


def catch_up(service, state: CycleState, since: date, through: date, *,
             now: datetime, **kwargs) -> list[CycleRun]:
    """Run every business date not yet completed, oldest first.

    Oldest first for the same reason DATA-1's backfill is: settlement state is
    cumulative, so a later day processed before an earlier one applies its
    transitions to a book that has not received the earlier day's.
    """
    runs = []
    for d in state.missed(through, since):
        run = run_cycle(service, d.isoformat(), now=now, **kwargs)
        state.record(run)
        runs.append(run)
        if run.status == "failed":
            run.notes.append(
                "Stopping. Continuing past a failure leaves a hole in the "
                "middle of the sequence and every later day is applied to a "
                "book missing this one.")
            break
    return runs


def render(runs: list[CycleRun]) -> str:
    lines = ["{:<14}{:<18}{:<10}{}".format("date", "step", "status", "detail")]
    lines.append("-" * 78)
    for run in runs:
        for i, step in enumerate(run.steps):
            lines.append("{:<14}{:<18}{:<10}{}".format(
                run.business_date if i == 0 else "", step["name"],
                step["status"], step["detail"][:36]))
        for n in run.notes:
            lines.append("   note: " + n[:70])
    return "\n".join(lines)
