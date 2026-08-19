"""Chargeback lifecycle: the hard 5% that is the job.

A chargeback is not "a negative settlement row". It is a dispute with a clock, a
reason code, an evidence deadline, and a set of outcomes that each post
different money. Treating it as a reversal loses the deadline, and the deadline
is the whole thing -- miss it and the funds are gone regardless of whether the
transaction was legitimate.

Lifecycle:

    received -> evidence_due -> represented -> won | lost
                     |                          |
                     +-> accepted (no defence) --+-> lost
                     +-> expired (deadline missed) -> lost

`expired` and `accepted` both end in a loss, and separating them matters: one is
a decision, the other is an operational failure, and a team that reports them
together can never tell how much money it is losing to its own process.

Deadlines are in CALENDAR days, because card network rules count calendar days,
not business days. Using business days here would silently grant several extra
days that do not exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Representative windows. Real values are network- and reason-specific and come
# from the scheme rulebooks; these are plausible stand-ins, labelled as such.
EVIDENCE_WINDOW_DAYS = {
    "fraud": 7,
    "product_not_received": 10,
    "duplicate_processing": 10,
    "credit_not_processed": 10,
    "unrecognised": 7,
}
DEFAULT_WINDOW_DAYS = 10

# Reason codes worth defending vs not. A dispute the merchant will lose is
# cheaper to accept than to fight, and knowing which is which is the difference
# between a chargeback team and a chargeback cost centre.
WINNABLE = {"product_not_received", "duplicate_processing", "credit_not_processed"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chargeback (
    chargeback_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ref             TEXT NOT NULL,
    file_id         TEXT NOT NULL,
    line_no         INTEGER NOT NULL,
    reason_code     TEXT NOT NULL,
    amount_minor    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    received_on     TEXT NOT NULL,
    original_auth_date TEXT,
    age_at_receipt_days INTEGER,
    evidence_due_on TEXT NOT NULL,
    state           TEXT NOT NULL,
    outcome_on      TEXT,
    UNIQUE (ref, file_id, line_no)
);
"""

TRANSITIONS = {
    ("received", "submit_evidence"): "represented",
    ("received", "accept"): "accepted",
    ("received", "expire"): "expired",
    ("represented", "win"): "won",
    ("represented", "lose"): "lost",
    ("accepted", "settle"): "lost",
    ("expired", "settle"): "lost",
}


class ChargebackError(Exception):
    pass


@dataclass
class ChargebackBook:
    con: object

    def __post_init__(self):
        self.con.executescript(SCHEMA)

    def receive(self, ref: str, file_id: str, line_no: int, reason_code: str,
                amount_minor: int, currency: str, received_on: str,
                original_auth_date: str | None) -> int:
        window = EVIDENCE_WINDOW_DAYS.get(reason_code, DEFAULT_WINDOW_DAYS)
        due = (date.fromisoformat(received_on) + timedelta(days=window)).isoformat()
        age = None
        if original_auth_date:
            age = (date.fromisoformat(received_on)
                   - date.fromisoformat(original_auth_date)).days
        cur = self.con.execute(
            "INSERT OR IGNORE INTO chargeback (ref, file_id, line_no, reason_code,"
            " amount_minor, currency, received_on, original_auth_date,"
            " age_at_receipt_days, evidence_due_on, state)"
            " VALUES (?,?,?,?,?,?,?,?,?,?, 'received')",
            (ref, file_id, line_no, reason_code, amount_minor, currency,
             received_on, original_auth_date, age, due))
        return cur.lastrowid

    def transition(self, chargeback_id: int, action: str, on_date: str) -> str:
        row = self.con.execute(
            "SELECT * FROM chargeback WHERE chargeback_id = ?",
            (chargeback_id,)).fetchone()
        if row is None:
            raise ChargebackError("unknown chargeback {}".format(chargeback_id))
        key = (row["state"], action)
        if key not in TRANSITIONS:
            raise ChargebackError(
                "illegal transition: cannot {} from {!r}".format(action, row["state"]))
        if action == "submit_evidence" and on_date > row["evidence_due_on"]:
            raise ChargebackError(
                "evidence submitted {} but the deadline was {} -- the network will "
                "not accept it. Missing the window is not a soft failure; the funds "
                "are gone regardless of the merits.".format(on_date,
                                                            row["evidence_due_on"]))
        new_state = TRANSITIONS[key]
        self.con.execute(
            "UPDATE chargeback SET state = ?, outcome_on = ? WHERE chargeback_id = ?",
            (new_state, on_date, chargeback_id))
        return new_state

    def expire_overdue(self, as_of: str) -> int:
        """Sweep: anything past its deadline with no evidence submitted is lost.

        Run daily. An unswept queue makes the loss invisible until the funding
        report, which is where 'we did not know we were losing that' comes from.
        """
        rows = self.con.execute(
            "SELECT chargeback_id FROM chargeback"
            " WHERE state = 'received' AND evidence_due_on < ?", (as_of,)).fetchall()
        for r in rows:
            self.transition(r["chargeback_id"], "expire", as_of)
        return len(rows)

    def deadline_report(self, as_of: str) -> list[dict]:
        rows = self.con.execute(
            "SELECT * FROM chargeback WHERE state = 'received'").fetchall()
        out = []
        for r in rows:
            days = (date.fromisoformat(r["evidence_due_on"])
                    - date.fromisoformat(as_of)).days
            out.append({
                "chargeback_id": r["chargeback_id"], "ref": r["ref"],
                "reason_code": r["reason_code"], "amount_minor": r["amount_minor"],
                "days_remaining": days,
                "urgency": ("OVERDUE" if days < 0 else "due today" if days == 0
                            else "urgent" if days <= 2 else "normal"),
                "winnable": r["reason_code"] in WINNABLE,
            })
        return sorted(out, key=lambda d: d["days_remaining"])

    def summary(self) -> dict:
        rows = self.con.execute(
            "SELECT state, COUNT(*) n, COALESCE(SUM(amount_minor),0) amt"
            " FROM chargeback GROUP BY state").fetchall()
        return {r["state"]: {"count": r["n"], "amount_minor": r["amt"]} for r in rows}

    def aged_reference_report(self) -> dict:
        """How far back chargebacks reach.

        This is the number that decides retention policy. If disputes routinely
        arrive 90 days after authorization, a 60-day archive means the original
        transaction is gone when the dispute lands -- and a dispute you cannot
        evidence is a dispute you lose.
        """
        row = self.con.execute(
            "SELECT MIN(age_at_receipt_days) mn, MAX(age_at_receipt_days) mx,"
            "       AVG(age_at_receipt_days) av, COUNT(*) n"
            "  FROM chargeback WHERE age_at_receipt_days IS NOT NULL").fetchone()
        return {"min_days": row["mn"], "max_days": row["mx"],
                "avg_days": row["av"], "n": row["n"]}
