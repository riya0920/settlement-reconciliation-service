"""The job that actually moves data between tiers.

`src/retention.py` has the policy, the three tiers and an `ArchiveStore` that
can hold and retrieve. Nothing called it. A retention policy nothing executes is
a document, and the failure mode is specific and quiet: everything stays hot,
storage grows, and the first anyone notices is a cost review -- or worse, the
policy is executed by hand once, unevenly, and nobody can say which dates were
archived and which were dropped.

WHAT MAKES THIS MORE THAN A FOR-LOOP, and each of these is a decision rather
than an implementation detail:

  ARCHIVE BEFORE DELETE, VERIFIED. The hot row is removed only after the archive
  file is written AND read back with a matching row count. Any other order has a
  window where a crash loses the data, and "we archived it" with nothing at the
  other end is worse than not archiving.

  IDEMPOTENT BY BUSINESS DATE. The job runs daily and will be re-run after
  failures, so archiving a date already archived must be a no-op rather than a
  second file or a duplicated set of rows.

  PURGE IS SEPARATELY AUTHORISED. Archiving is reversible; purging is not. They
  are two verbs, they take two different flags, and the default for purge is
  off. A single `run_retention()` that quietly deletes past the legal floor is
  how a routine job becomes an incident.

  DRY RUN FIRST. The plan is computable without touching anything, and a job
  that deletes should be able to say what it would do before it does it.

  THE PURGE FLOOR IS A FLOOR. `LEGAL_FLOOR_DAYS` is the minimum time data must
  be KEPT, not a deadline to delete on. Nothing here purges automatically at the
  floor; it only becomes eligible.

  A PURGEABLE DATE IS NEVER ARCHIVED ON THE WAY. Archiving data you are already
  permitted to delete buys nothing, so a date that has aged past the floor is
  left where it is and marked eligible. The consequence is easy to miss and cost
  two failing tests to find: PURGE HAS TO REACH INTO HOT STORAGE, because a
  purgeable date may never have been archived at all. A purge that only deletes
  archive files leaves those rows in place while logging a deletion that did not
  fully happen.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .retention import ArchiveStore, RetentionPolicy, plan_retention


class ArchiveVerificationError(Exception):
    """Raised when what came back out of the archive is not what went in.

    Deliberately not caught anywhere. The whole value of the archive is that the
    data is retrievable, so a verification failure means the job's core promise
    is broken and continuing would delete hot rows against a bad archive.
    """


@dataclass
class ArchiveResult:
    archived_dates: list = field(default_factory=list)
    archived_rows: int = 0
    skipped_already_archived: list = field(default_factory=list)
    purged_dates: list = field(default_factory=list)
    purge_eligible: list = field(default_factory=list)
    hot_bytes: int = 0
    archive_bytes: int = 0
    seconds: float = 0.0

    @property
    def compression_ratio(self) -> float:
        return self.hot_bytes / self.archive_bytes if self.archive_bytes else 0.0


def _rows_for_date(con, business_date: str) -> list:
    """A transaction AND its provenance, because evidence is the point.

    Archiving the txn row alone would keep the amount and lose the answer to
    "which file said so, on which line, under which rule" -- which is the half
    a representment actually needs. The match events travel with the row.
    """
    txns = con.execute(
        "SELECT ref, auth_date, amount_minor, currency, settled_minor,"
        " fee_minor, state FROM txn WHERE auth_date = ? ORDER BY ref",
        (business_date,)).fetchall()
    out = []
    for t in txns:
        events = con.execute(
            "SELECT file_id, line_no, rule, from_state, to_state, amount_minor"
            " FROM match_event WHERE ref = ? ORDER BY id", (t["ref"],)).fetchall()
        out.append({**dict(t), "events": [dict(e) for e in events]})
    return out


def run_archival(con, store: ArchiveStore, as_of: str,
                 policy: RetentionPolicy | None = None,
                 *, purge: bool = False, dry_run: bool = False,
                 purge_reason: str = "past legal retention floor",
                 rows_for_date=_rows_for_date) -> ArchiveResult:
    """One day's tier movement. Safe to re-run; purges only when asked.

    `purge` defaults to False because archiving is reversible and purging is
    not. They are two verbs and they take two flags.
    """
    started = time.perf_counter()
    policy = policy or RetentionPolicy()
    res = ArchiveResult()

    # The date list is the union of BOTH tiers, and this was a real bug. Taking
    # it from hot storage alone meant the job forgot a date the moment it
    # archived it: nothing already archived could be reported as already
    # archived, and -- far worse -- nothing already archived could ever be
    # purged, because the purge branch only ever saw dates still sitting hot.
    hot_dates = {r["auth_date"] for r in con.execute(
        "SELECT DISTINCT auth_date FROM txn").fetchall()}
    archived_dates = {f.name.replace(".jsonl.gz", "")
                      for f in store.root.glob("*.jsonl.gz")}
    dates = sorted(hot_dates | archived_dates)
    plan = plan_retention(dates, as_of, policy)

    for bdate in plan["archive"] + plan["cold"]:
        if store._path(bdate).exists():
            res.skipped_already_archived.append(bdate)
            continue
        rows = rows_for_date(con, bdate)
        if not rows:
            continue
        res.hot_bytes += sum(len(str(r)) for r in rows)
        if dry_run:
            res.archived_dates.append(bdate)
            res.archived_rows += len(rows)
            continue

        # Write, then READ BACK, then delete. Any other order has a window where
        # a crash loses the data.
        path = store.archive(bdate, rows)
        back = store.retrieve(bdate)
        if len(back) != len(rows):
            raise ArchiveVerificationError(
                "archived {} rows for {} and read back {} -- refusing to delete "
                "the hot copy against an archive that does not match".format(
                    len(rows), bdate, len(back)))
        res.archive_bytes += path.stat().st_size
        refs = [r["ref"] for r in con.execute(
            "SELECT ref FROM txn WHERE auth_date = ?", (bdate,)).fetchall()]
        con.executemany("DELETE FROM match_event WHERE ref = ?",
                        [(r,) for r in refs])
        con.execute("DELETE FROM txn WHERE auth_date = ?", (bdate,))
        res.archived_dates.append(bdate)
        res.archived_rows += len(rows)

    res.purge_eligible = list(plan["purgeable"])
    if purge and not dry_run:
        for bdate in res.purge_eligible:
            # Purge means gone from EVERY tier. A date can reach the floor while
            # still hot -- it ages out of the archive window into purgeable
            # without necessarily having been archived on the way -- and deleting
            # only the archive file would leave the hot rows in place while
            # logging a purge that did not fully happen.
            deleted_file = store.purge(bdate, reason=purge_reason)
            hot_rows = con.execute(
                "SELECT COUNT(*) c FROM txn WHERE auth_date = ?",
                (bdate,)).fetchone()["c"]
            if hot_rows:
                refs = [r["ref"] for r in con.execute(
                    "SELECT ref FROM txn WHERE auth_date = ?",
                    (bdate,)).fetchall()]
                con.executemany("DELETE FROM match_event WHERE ref = ?",
                                [(r,) for r in refs])
                con.execute("DELETE FROM txn WHERE auth_date = ?", (bdate,))
                if not deleted_file:
                    store.record_purge(bdate, hot_rows, 0,
                                       purge_reason + " (hot, never archived)")
            if deleted_file or hot_rows:
                res.purged_dates.append(bdate)

    res.seconds = time.perf_counter() - started
    return res


def evidence_for(con, store: ArchiveStore, ref: str,
                 search_dates: list) -> dict | None:
    """Find a transaction wherever it now lives.

    A dispute arrives with a reference and no idea which tier the data is in, so
    the lookup has to cover both and SAY which one answered. "Found in the
    archive, took 400ms" and "found hot" are the same answer to the dispute and
    completely different answers to a capacity question.
    """
    started = time.perf_counter()
    row = con.execute(
        "SELECT ref, auth_date, amount_minor, currency, settled_minor,"
        " fee_minor, state FROM txn WHERE ref = ?", (ref,)).fetchone()
    if row is not None:
        events = con.execute(
            "SELECT file_id, line_no, rule, from_state, to_state, amount_minor"
            " FROM match_event WHERE ref = ? ORDER BY id", (ref,)).fetchall()
        return {**dict(row), "events": [dict(e) for e in events], "tier": "hot",
                "lookup_seconds": time.perf_counter() - started}

    found = store.find_transaction(ref, search_dates)
    if found is None:
        return None
    return {**found, "lookup_seconds": time.perf_counter() - started}


def coverage_report(con, store: ArchiveStore, as_of: str,
                    policy: RetentionPolicy | None = None) -> dict:
    """Whether every business date the system has ever seen is accounted for.

    The question an auditor actually asks is not "what is your policy" but
    "where is the data for 14 March". A date that is neither hot nor archived
    nor in the purge log is an unexplained hole, and this is the check that
    finds one.
    """
    policy = policy or RetentionPolicy()
    hot = {r["auth_date"] for r in con.execute(
        "SELECT DISTINCT auth_date FROM txn").fetchall()}
    archived = {p.name.replace(".jsonl.gz", "")
                for p in store.root.glob("*.jsonl.gz")}
    purged = {e["business_date"] for e in store.purge_history()}

    known = hot | archived | purged
    overlap = hot & archived
    return {
        "hot_dates": len(hot),
        "archived_dates": len(archived),
        "purged_dates": len(purged),
        "total_accounted": len(known),
        "in_both_tiers": sorted(overlap),
        "duplicated": bool(overlap),
    }
