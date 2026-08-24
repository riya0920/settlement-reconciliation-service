"""A persistent break queue that CONSUMES the ledger feedback.

`ledger_link.unposted_breaks()` and `reconcile_to_ledger()` produce break-shaped
records, and until now nothing inserted them anywhere. That closed the loop in
the library and left it open in the running pipeline -- which is the difference
between "we detect posting failures" and "somebody is told about posting
failures".

WHY A QUEUE RATHER THAN A LOG. A log records that something happened. A queue
has the two properties that make a break actionable:

  IT SURVIVES THE RUN     aging only means anything if the item is still there
                          tomorrow. DATA-1 learned this one the hard way: an
                          in-memory queue is reborn one day old every morning
                          and nothing ever escalates.

  IT KEEPS FIRST_SEEN     a break that comes back keeps its ORIGINAL age. If
                          recurrence reset the clock, something unresolved for
                          three weeks would be forever one day old -- the most
                          common way a break queue fails silently.

THE LEDGER BREAKS ARE DIFFERENT FROM THE OTHERS, and the difference matters. A
settlement break is a disagreement between two records of the same event. A
`ledger_unposted` break is a disagreement between what the service BELIEVES and
what the general ledger CONTAINS -- which means the service's own view is
already wrong, and every downstream report built on it is wrong too. So they get
their own type and the highest tier from the start rather than aging into it.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS break_item (
    ref           TEXT NOT NULL,
    break_type    TEXT NOT NULL,
    detail        TEXT NOT NULL,
    core_amount   INTEGER NOT NULL DEFAULT 0,
    proc_amount   INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    resolved_by   TEXT,
    resolution    TEXT,
    PRIMARY KEY (ref, break_type)
);

-- Append-only. An audit trail you can edit is application logging with a nicer
-- name, so UPDATE and DELETE raise.
CREATE TABLE IF NOT EXISTS break_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ref        TEXT NOT NULL,
    break_type TEXT NOT NULL,
    at         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);

CREATE TRIGGER IF NOT EXISTS break_event_no_update
BEFORE UPDATE ON break_event
BEGIN SELECT RAISE(ABORT, 'break_event is append-only'); END;

CREATE TRIGGER IF NOT EXISTS break_event_no_delete
BEFORE DELETE ON break_event
BEGIN SELECT RAISE(ABORT, 'break_event is append-only'); END;
"""

# A ledger disagreement is not an ordinary break: it means our own books are
# wrong, so it starts at the top tier instead of aging into it.
LEDGER_TYPES = {"ledger_unposted", "ledger_divergence"}


@dataclass
class Tier:
    name: str
    min_days: int
    action: str


TIERS = [
    Tier("T0", 0, "monitor"),
    Tier("T1", 2, "assign to an analyst"),
    Tier("T2", 5, "escalate to the team lead"),
    Tier("T3", 10, "controller review / write-off decision"),
]


def install(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def tier_for(age_days: int, break_type: str) -> Tier:
    if break_type in LEDGER_TYPES:
        return TIERS[-1]
    chosen = TIERS[0]
    for t in TIERS:
        if age_days >= t.min_days:
            chosen = t
    return chosen


def upsert(con: sqlite3.Connection, item: dict, as_of: str) -> None:
    """Record a break, preserving `first_seen` if it is already known."""
    ref, btype = item["ref"], item["break_type"]
    existing = con.execute(
        "SELECT first_seen FROM break_item WHERE ref = ? AND break_type = ?",
        (ref, btype)).fetchone()

    if existing:
        # last_seen moves; first_seen does NOT. That is the whole point.
        con.execute(
            "UPDATE break_item SET last_seen = ?, detail = ?, status = 'open'"
            " WHERE ref = ? AND break_type = ?",
            (as_of, item.get("detail", ""), ref, btype))
        action = "recurred"
    else:
        con.execute(
            "INSERT INTO break_item (ref, break_type, detail, core_amount,"
            " proc_amount, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
            (ref, btype, item.get("detail", ""), item.get("core_amount", 0),
             item.get("proc_amount", 0), as_of, as_of))
        action = "opened"

    con.execute(
        "INSERT INTO break_event (ref, break_type, at, actor, action, detail)"
        " VALUES (?,?,?,?,?,?)",
        (ref, btype, as_of, "settlement-service", action,
         item.get("detail", "")[:200]))


def ingest_ledger_feedback(con: sqlite3.Connection, link, as_of: str,
                           expected_minor: int | None = None,
                           account: str | None = None) -> dict:
    """Turn posting failures and GL divergence into queue items.

    This is the call that was missing. `unposted_breaks()` existed and returned
    a list nobody read.
    """
    added = 0
    for item in link.unposted_breaks():
        upsert(con, item, as_of)
        added += 1

    divergence = None
    if expected_minor is not None and account:
        res = link.reconcile_to_ledger(expected_minor, account)
        if res.get("break"):
            upsert(con, res["break"], as_of)
            added += 1
            divergence = res
    return {"ingested": added, "divergence": divergence}


def aged(con: sqlite3.Connection, as_of: str) -> list[dict]:
    rows = con.execute(
        "SELECT ref, break_type, detail, first_seen, last_seen, status"
        "  FROM break_item WHERE status = 'open' ORDER BY first_seen").fetchall()
    out = []
    for ref, btype, detail, first, last, status in rows:
        age = _days_between(first, as_of)
        t = tier_for(age, btype)
        out.append({"ref": ref, "break_type": btype, "detail": detail,
                    "first_seen": first, "last_seen": last, "age_days": age,
                    "tier": t.name, "action": t.action, "status": status})
    return out


def resolve(con: sqlite3.Connection, ref: str, break_type: str, actor: str,
            resolution: str, as_of: str) -> None:
    if not resolution or not resolution.strip():
        raise ValueError("a resolution needs a reason code, not blank text")
    con.execute(
        "UPDATE break_item SET status='resolved', resolved_by=?, resolution=?"
        " WHERE ref=? AND break_type=?", (actor, resolution, ref, break_type))
    con.execute(
        "INSERT INTO break_event (ref, break_type, at, actor, action, detail)"
        " VALUES (?,?,?,?,'resolved',?)",
        (ref, break_type, as_of, actor, resolution))


def _days_between(a: str, b: str) -> int:
    from datetime import date

    try:
        return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days
    except ValueError:
        return 0
