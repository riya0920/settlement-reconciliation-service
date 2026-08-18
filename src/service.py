"""Settlement service: idempotent file ingestion, lifecycle states, fee
reconciliation, and replay.

Lifecycle: pending -> settled | partially_settled | disputed
The state is the deliverable. Matching without lifecycle states is a diff, not a
settlement system: "this row matched" says nothing about whether the money
arrived, whether it arrived in full, or whether it was later taken back.

Idempotent ingestion: a file is identified by its file_id AND a content hash.
  same id + same content   -> already processed, no-op (the redelivery case)
  same id + different body -> REJECT. This is the dangerous one: a processor
                              that re-sends a file with an extra line is either
                              correcting or duplicating, and the system must not
                              guess. It rejects and asks.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .files import FileRejected, InternalTxn, SettlementRow, expected_fee, parse

FEE_TOLERANCE_MINOR = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS txn (
    ref             TEXT PRIMARY KEY,
    auth_date       TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    settled_minor   INTEGER NOT NULL DEFAULT 0,
    fee_minor       INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS ingested_file (
    file_id       TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    ingested_at   TEXT NOT NULL
);
-- Provenance on every state change: which file, which line, which rule.
CREATE TABLE IF NOT EXISTS match_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ref         TEXT NOT NULL,
    file_id     TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    rule        TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT,
    amount_minor INTEGER
);
CREATE TABLE IF NOT EXISTS fee_variance (
    ref          TEXT NOT NULL,
    file_id      TEXT NOT NULL,
    expected_minor INTEGER NOT NULL,
    actual_minor   INTEGER NOT NULL,
    variance_minor INTEGER NOT NULL
);
"""


class DuplicateFile(Exception):
    pass


class ConflictingRedelivery(Exception):
    pass


class Service:
    def __init__(self, path: str = ":memory:"):
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)

    def load_internal(self, txns: list[InternalTxn]) -> None:
        self.con.executemany(
            "INSERT OR REPLACE INTO txn (ref, auth_date, amount_minor, currency)"
            " VALUES (?,?,?,?)",
            [(t.ref, t.auth_date, t.amount_minor, t.currency) for t in txns])
        self.con.commit()

    # -- ingestion ---------------------------------------------------------
    def ingest(self, path: Path, *, allow_replay: bool = False) -> dict:
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        file_id, rows = parse(path)          # raises FileRejected on bad totals

        prior = self.con.execute(
            "SELECT * FROM ingested_file WHERE file_id = ?", (file_id,)).fetchone()
        if prior is not None:
            if prior["content_hash"] == content_hash and not allow_replay:
                raise DuplicateFile(
                    "{} already ingested, identical content -> no-op".format(file_id))
            if prior["content_hash"] != content_hash and not allow_replay:
                raise ConflictingRedelivery(
                    "{} re-delivered with DIFFERENT content ({} rows now vs {} before). "
                    "Not processed: a redelivery that differs is either a correction "
                    "or a duplicate, and the system must not guess.".format(
                        file_id, len(rows), prior["row_count"]))

        applied = self._apply(rows)
        self.con.execute(
            "INSERT OR REPLACE INTO ingested_file VALUES (?,?,?, datetime('now'))",
            (file_id, content_hash, len(rows)))
        self.con.commit()
        return {"file_id": file_id, "rows": len(rows), **applied}

    def _apply(self, rows: list[SettlementRow]) -> dict:
        stats = {"settled": 0, "partial": 0, "chargebacks": 0,
                 "unmatched": 0, "fee_variances": 0}
        for r in rows:
            t = self.con.execute("SELECT * FROM txn WHERE ref = ?", (r.ref,)).fetchone()
            if t is None:
                stats["unmatched"] += 1
                self._event(r.ref, r.file_id, r.line_no, "no_internal_txn", None, None,
                            r.gross_minor)
                continue

            if r.row_type == "chargeback":
                self._transition(t, "disputed", r, "chargeback_received")
                stats["chargebacks"] += 1
                continue

            settled = t["settled_minor"] + r.gross_minor
            state = "settled" if settled >= t["amount_minor"] else "partially_settled"
            self.con.execute(
                "UPDATE txn SET settled_minor = ?, fee_minor = fee_minor + ?,"
                " state = ? WHERE ref = ?", (settled, r.fee_minor, state, r.ref))
            self._event(r.ref, r.file_id, r.line_no,
                        "exact_ref_match", t["state"], state, r.gross_minor)
            stats["settled" if state == "settled" else "partial"] += 1

            exp = expected_fee(r.gross_minor, r.currency)
            if abs(exp - r.fee_minor) > FEE_TOLERANCE_MINOR:
                self.con.execute(
                    "INSERT INTO fee_variance VALUES (?,?,?,?,?)",
                    (r.ref, r.file_id, exp, r.fee_minor, r.fee_minor - exp))
                stats["fee_variances"] += 1
        return stats

    def _transition(self, t, to_state: str, r: SettlementRow, rule: str) -> None:
        self.con.execute("UPDATE txn SET state = ? WHERE ref = ?", (to_state, t["ref"]))
        self._event(t["ref"], r.file_id, r.line_no, rule, t["state"], to_state,
                    r.gross_minor)

    def _event(self, ref, file_id, line_no, rule, frm, to, amount) -> None:
        self.con.execute(
            "INSERT INTO match_event (ref, file_id, line_no, rule, from_state,"
            " to_state, amount_minor) VALUES (?,?,?,?,?,?,?)",
            (ref, file_id, line_no, rule, frm, to, amount))

    # -- reporting ---------------------------------------------------------
    def state_counts(self) -> dict:
        return {r["state"]: r["n"] for r in self.con.execute(
            "SELECT state, COUNT(*) n FROM txn GROUP BY state")}

    def aged_breaks(self, as_of: str) -> list[dict]:
        """Unsettled transactions, aged. Escalation tiers are the operations
        layer: a break that ages is not a data problem any more, it is somebody's
        assignment."""
        rows = self.con.execute(
            "SELECT ref, auth_date, amount_minor, currency, state,"
            "       CAST(julianday(?) - julianday(auth_date) AS INTEGER) AS age"
            "  FROM txn WHERE state IN ('pending','partially_settled')", (as_of,)
        ).fetchall()
        out = []
        for r in rows:
            age = r["age"]
            tier = ("T0 monitor" if age <= 2 else "T1 analyst" if age <= 5
                    else "T2 supervisor" if age <= 10 else "T3 write-off review")
            out.append({"ref": r["ref"], "age": age, "state": r["state"], "tier": tier})
        return out

    def fee_variance_summary(self) -> dict:
        r = self.con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(variance_minor),0) total,"
            "       COALESCE(AVG(variance_minor),0) avg FROM fee_variance").fetchone()
        return {"count": r["n"], "total_minor": r["total"], "avg_minor": r["avg"]}

    def reset_for_replay(self) -> None:
        """Reset derived state for a full reprocess.

        `ingested_file` is cleared too, and that is the subtle part. Replay must
        run through the SAME duplicate-detection path as the original ingest --
        otherwise a redelivered file gets applied twice during replay and the
        reprocess silently double-counts. An earlier version of this method kept
        the file ledger and passed an `allow_replay` flag that skipped the
        duplicate check; it produced a replay whose lifecycle counts did not
        match the original run, which is exactly the failure a settlement replay
        is supposed to be immune to.

        The archive on disk is the source of truth for what was received; this
        table is derived state like any other.
        """
        self.con.executescript(
            "UPDATE txn SET settled_minor = 0, fee_minor = 0, state = 'pending';"
            "DELETE FROM match_event;"
            "DELETE FROM fee_variance;"
            "DELETE FROM ingested_file;")
        self.con.commit()
