"""Settlement -> ledger: the SE-1 / SE-2 pairing, wired.

Proves three things:
  1. Settlement events post balanced double-entry journal entries.
  2. Replaying a settlement file does NOT double-post -- the ledger's
     idempotency key is the file coordinate.
  3. The books balance and SE-1's four invariants hold afterwards.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.files import DATA, FileRejected, generate, parse
from src.ledger_link import BANK, CB_LOSS, CB_RESERVE, FEE_EXPENSE, LedgerLink
from src.ledger_link import NETWORK_RECEIVABLE


def usd(minor):
    return "${:,.2f}".format(minor / 100)


def ingest_to_ledger(link: LedgerLink, files) -> dict:
    stats = {"settlements": 0, "chargebacks": 0, "rejected_files": 0}
    for f in files:
        try:
            file_id, rows = parse(f)
        except FileRejected:
            stats["rejected_files"] += 1
            continue
        for r in rows:
            if r.row_type == "chargeback":
                link.post_chargeback(r.ref, file_id, r.line_no, r.gross_minor)
                stats["chargebacks"] += 1
            else:
                link.post_settlement(r.ref, file_id, r.line_no,
                                     r.gross_minor, r.fee_minor)
                stats["settlements"] += 1
    return stats


def main() -> int:
    _internal, files, _dup = generate()
    link = LedgerLink()

    print("=" * 76)
    print("SETTLEMENT -> LEDGER (SE-2 x SE-1)")
    print("-" * 76)
    if not link.available:
        print("SE-1 ledger unavailable: {}".format(link.reason))
        print("The linkage is a no-op and this run proves nothing about the books.")
        return 1
    print("SE-1 ledger: linked")

    stats = ingest_to_ledger(link, files)
    print("settlement rows posted : {:,}".format(stats["settlements"]))
    print("chargeback rows posted : {:,}".format(stats["chargebacks"]))
    print("files rejected on control totals (nothing posted): {}".format(
        stats["rejected_files"]))
    print("journal transactions   : {:,}".format(link.posted))

    print("\n" + "-" * 76)
    print("BALANCES (debit-positive)")
    print("-" * 76)
    b = link.balances()
    for acct in (BANK, NETWORK_RECEIVABLE, FEE_EXPENSE, CB_RESERVE, CB_LOSS):
        print("{:<34}{:>20}".format(acct, usd(b[acct])))

    tb = link.trial_balance()
    print("\n" + "-" * 76)
    print("TRIAL BALANCE")
    print("-" * 76)
    print("total debits  : {:>20}".format(usd(tb["debits"])))
    print("total credits : {:>20}".format(usd(tb["credits"])))
    print("balanced      : {}   difference {}".format(
        tb["balanced"], usd(tb["difference"])))

    # ---- replay must not double-post --------------------------------------
    print("\n" + "=" * 76)
    print("REPLAY: reprocessing the archive must NOT double-post")
    print("-" * 76)
    before_posted, before_bank = link.posted, b[BANK]
    ingest_to_ledger(link, files)
    after = link.balances()
    print("new journal transactions on replay : {}".format(
        link.posted - before_posted))
    print("idempotent replays suppressed      : {:,}".format(
        link.skipped_duplicate))
    print("bank balance before / after        : {} / {}".format(
        usd(before_bank), usd(after[BANK])))
    unchanged = after[BANK] == before_bank and link.posted == before_posted
    print("books unchanged by replay          : {}".format(unchanged))
    print("\nThe idempotency key is the settlement file coordinate (file_id, line).")
    print("Without it, SE-2's replay-after-fix feature -- the thing that makes a")
    print("matching bug recoverable -- would corrupt the books every time it ran.")

    # ---- chargeback outcomes ----------------------------------------------
    print("\n" + "=" * 76)
    print("CHARGEBACK OUTCOMES: reserve vs P&L")
    print("-" * 76)
    link.post_chargeback_outcome("TESTWIN", won=True, amount_minor=50_000,
                                 request_id="r-win")
    link.post_chargeback_outcome("TESTLOSS", won=False, amount_minor=50_000,
                                 request_id="r-loss")
    final = link.balances()
    print("chargeback reserve : {}".format(usd(final[CB_RESERVE])))
    print("chargeback losses  : {}".format(usd(final[CB_LOSS])))
    print("\nA chargeback is a CONTINGENT loss until the dispute resolves, so")
    print("receipt draws on a reserve and only the outcome touches P&L. Expensing")
    print("on receipt overstates losses in the month it arrives and understates")
    print("them in the month it is lost -- and both errors land in a period")
    print("somebody reports on.")

    # ---- invariants --------------------------------------------------------
    violations = link.check_invariants()
    print("\n" + "=" * 76)
    print("SE-1 INVARIANTS AFTER {} POSTINGS".format(link.posted))
    print("-" * 76)
    if violations:
        for v in violations:
            print("  [{}] {}".format(v.invariant, v.detail))
    else:
        print("ALL INVARIANTS HOLD (I1 balance, I2 floors, I3 derived, I4 idempotency)")
    print("=" * 76)
    return 0 if not violations and unchanged and tb["balanced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
