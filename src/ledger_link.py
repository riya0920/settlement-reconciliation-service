"""Post settlement events to a double-entry ledger.

This is the pairing the spec calls out: SE-1's ledger records intent,
reconciliation proves reality matched it. Until now SE-2 tracked lifecycle
states and moved no money, which meant "settled" was a string in a table rather
than a claim anyone could audit.

The accounting, stated so it can be argued with. From the acquirer's books:

  SETTLEMENT (funds arrive from the network, net of fees)
      D  bank:settlement_account        gross - fee    cash actually received
      D  expense:processor_fees         fee            what the processor kept
      C  network:receivable             gross          the claim is discharged

  CHARGEBACK (funds taken back)
      D  liability:chargeback_reserve   gross          drawn from the reserve
      C  bank:settlement_account        gross          cash leaves

  CHARGEBACK WON (representment succeeds -- the reserve draw is reversed)
      D  bank:settlement_account        gross
      C  liability:chargeback_reserve   gross

  CHARGEBACK LOST (the loss crystallises against P&L)
      D  expense:chargeback_losses      gross
      C  liability:chargeback_reserve   gross

Why a RESERVE rather than expensing the chargeback immediately: a chargeback is
a contingent loss until the dispute resolves. Booking it straight to P&L on
receipt overstates losses in the month it arrives and understates them in the
month it is lost, and both errors land in a period someone reports on. The
reserve holds the contingency; the expense recognises it when the outcome is
known.

Every posting is idempotent on the settlement file's (file_id, line_no), so
replaying a file -- which SE-2 does on purpose after a logic fix -- cannot
double-post to the ledger. That is the property that makes replay safe at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

# SE-1 lives in a sibling repo. When it is not importable the linkage degrades
# to a no-op with a stated reason rather than crashing the settlement run --
# but `available` is False and the report says so, so the absence is visible.
_SE1 = Path(__file__).resolve().parents[2] / "se1-ledger-core"

BANK = "bank:settlement_account"
NETWORK_RECEIVABLE = "network:receivable"
FEE_EXPENSE = "expense:processor_fees"
CB_RESERVE = "liability:chargeback_reserve"
CB_LOSS = "expense:chargeback_losses"

ACCOUNTS = [
    (BANK, "asset"),
    (NETWORK_RECEIVABLE, "asset"),
    (FEE_EXPENSE, "expense"),
    (CB_RESERVE, "liability"),
    (CB_LOSS, "expense"),
]


class LedgerLink:
    failures: list

    def __init__(self, currency: str = "USD", db_path: str = ":memory:"):
        self.failures = []
        self.currency = currency
        self.available = False
        self.reason = ""
        self.posted = 0
        self.skipped_duplicate = 0
        try:
            if str(_SE1) not in sys.path:
                sys.path.insert(0, str(_SE1))
            from ledger import invariants
            from ledger.core import Ledger, credit, debit

            self._invariants = invariants
            self._credit, self._debit = credit, debit
            self.ledger = Ledger(db_path)
            unlimited = -10**15
            for acct, kind in ACCOUNTS:
                self.ledger.open_account(acct, kind, currency,
                                         floor_minor=unlimited,
                                         overdraft_allowed=True)
            self.available = True
        except Exception as exc:                       # pragma: no cover
            self.reason = "SE-1 ledger not importable: {}".format(exc)

    # -- postings ----------------------------------------------------------
    def post_settlement(self, ref: str, file_id: str, line_no: int,
                        gross_minor: int, fee_minor: int) -> int | None:
        """Cash arrives net of fees; the receivable is discharged gross."""
        if not self.available or gross_minor <= 0:
            return None
        entries = [
            self._debit(BANK, gross_minor - fee_minor, self.currency),
            self._credit(NETWORK_RECEIVABLE, gross_minor, self.currency),
        ]
        if fee_minor:
            entries.insert(1, self._debit(FEE_EXPENSE, fee_minor, self.currency))
        return self._post(entries, "settlement:" + ref, file_id, line_no,
                          {"ref": ref, "gross": gross_minor, "fee": fee_minor})

    def post_chargeback(self, ref: str, file_id: str, line_no: int,
                        gross_minor: int) -> int | None:
        """Contingent loss: drawn from the reserve, not expensed yet."""
        if not self.available:
            return None
        amount = abs(gross_minor)
        entries = [
            self._debit(CB_RESERVE, amount, self.currency),
            self._credit(BANK, amount, self.currency),
        ]
        return self._post(entries, "chargeback:" + ref, file_id, line_no,
                          {"ref": ref, "gross": amount})

    def post_chargeback_outcome(self, ref: str, won: bool, amount_minor: int,
                                request_id: str) -> int | None:
        """Resolution. Won reverses the reserve draw; lost crystallises it."""
        if not self.available:
            return None
        amount = abs(amount_minor)
        if won:
            entries = [self._debit(BANK, amount, self.currency),
                       self._credit(CB_RESERVE, amount, self.currency)]
            reason = "chargeback_won:" + ref
        else:
            entries = [self._debit(CB_LOSS, amount, self.currency),
                       self._credit(CB_RESERVE, amount, self.currency)]
            reason = "chargeback_lost:" + ref
        return self._post(entries, reason, "outcome", 0,
                          {"ref": ref, "won": won, "amount": amount})

    # ------------------------------------------------- feedback to the queue
    def unposted_breaks(self) -> list[dict]:
        """Posting failures, shaped as reconciliation breaks.

        The link used to be one-directional: settlements went into the journal
        and nothing came back, so a posting that failed produced no break, no
        alert and no queue item. This closes the loop -- every failure becomes a
        `ledger_unposted` break with the file coordinate that produced it, so it
        ages and escalates like any other.
        """
        return [{
            "ref": f["ref"], "break_type": "ledger_unposted",
            "detail": "GL posting failed ({}): {}".format(f["reason"], f["error"]),
            "core_amount": f["amount_minor"], "proc_amount": 0,
            "file_id": f["file_id"], "line_no": f["line_no"],
        } for f in self.failures]

    def reconcile_to_ledger(self, expected_minor: int, account: str) -> dict:
        """Does the GL agree with what settlement thinks it moved?

        A control the one-directional link could not have: the service's own
        total against the journal's balance. They must agree to the minor unit,
        and a difference is a break in its own right -- not a rounding note.
        """
        if not self.available:
            return {"status": "no_ledger"}
        actual = self.ledger.balance(account)
        diff = actual - expected_minor
        return {
            "status": "ok" if diff == 0 else "BREAK",
            "account": account,
            "expected_minor": expected_minor,
            "ledger_minor": actual,
            "difference_minor": diff,
            "break": None if diff == 0 else {
                "ref": "GL:{}".format(account),
                "break_type": "ledger_divergence",
                "detail": ("settlement expected {} minor in {}, ledger holds {}"
                           .format(expected_minor, account, actual)),
                "core_amount": expected_minor, "proc_amount": actual,
            },
        }

    def _post(self, entries, reason: str, file_id: str, line_no: int,
              payload: dict) -> int | None:
        # Idempotency key is the file coordinate, so replaying a settlement file
        # after a logic fix cannot double-post. Without this, SE-2's replay
        # feature would corrupt the books every time it ran.
        key = "{}:{}:{}".format(file_id, line_no, reason)
        try:
            body, _status, replayed = self.ledger.post_idempotent(
                key=key, payload=payload, entries=entries,
                actor="settlement-service", reason=reason, request_id=key)
        except Exception as exc:                              # noqa: BLE001
            # A swallowed posting failure is the worst outcome available here.
            # The settlement service goes on believing the transaction settled,
            # the general ledger never hears about it, and the two disagree by
            # exactly that amount forever -- with nothing anywhere pointing at
            # the row. Returning None quietly, which this did, is how a
            # reconciliation platform becomes the thing that needs reconciling.
            self.failures.append({
                "ref": payload.get("ref", "?"), "file_id": file_id,
                "line_no": line_no, "reason": reason,
                "amount_minor": payload.get("amount_minor", 0),
                "error": "{}: {}".format(type(exc).__name__, exc),
            })
            return None
        if replayed:
            self.skipped_duplicate += 1
        else:
            self.posted += 1
        return body.get("txn_id")

    # -- reporting ---------------------------------------------------------
    def balances(self) -> dict:
        if not self.available:
            return {}
        return {acct: self.ledger.balance(acct) for acct, _ in ACCOUNTS}

    def check_invariants(self) -> list:
        if not self.available:
            return []
        return self._invariants.check_all(self.ledger._conn())

    def trial_balance(self) -> dict:
        """Debits must equal credits across the whole book. This is the number a
        controller looks at first, and it is derived from the journal rather than
        from any running total the service kept."""
        if not self.available:
            return {"available": False}
        row = self.ledger._conn().execute(
            "SELECT SUM(CASE direction WHEN 'D' THEN amount_minor ELSE 0 END) dr,"
            "       SUM(CASE direction WHEN 'C' THEN amount_minor ELSE 0 END) cr"
            "  FROM journal_entry").fetchone()
        dr, cr = row["dr"] or 0, row["cr"] or 0
        return {"available": True, "debits": dr, "credits": cr,
                "balanced": dr == cr, "difference": dr - cr}
