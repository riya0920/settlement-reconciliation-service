"""Daily settlement run, then the replay-after-fix demo.

The replay demo is the point: a matching bug is found, the logic is fixed, 30
days of archived files are reprocessed, and the breaks resolve. Reprocessability
is the first question after any logic bug in a settlement system, and a system
that cannot answer it accumulates manual adjustments forever.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import src.files as files_mod
from src.files import DATA, FileRejected, generate
from src.service import ConflictingRedelivery, DuplicateFile, Service

AS_OF = "2026-05-05"


def ingest_all(svc: Service, files, allow_replay=False):
    stats = {"ok": 0, "rejected": 0, "duplicate": 0, "conflict": 0}
    rejected_files = []
    for f in files:
        try:
            svc.ingest(f, allow_replay=allow_replay)
            stats["ok"] += 1
        except FileRejected as exc:
            stats["rejected"] += 1
            rejected_files.append((f.name, str(exc).split(" -- ")[-1][:90]))
        except DuplicateFile:
            stats["duplicate"] += 1
        except ConflictingRedelivery:
            stats["conflict"] += 1
    return stats, rejected_files


def main() -> int:
    internal, files, dup_stem = generate()
    svc = Service()
    svc.load_internal(internal)

    print("=" * 78)
    print("FILE INGESTION  ({} files, {:,} internal transactions)".format(
        len(files), len(internal)))
    print("-" * 78)
    stats, rejected = ingest_all(svc, files)
    print("accepted                 : {}".format(stats["ok"]))
    print("rejected on control total: {}".format(stats["rejected"]))
    print("duplicate delivery (noop): {}".format(stats["duplicate"]))
    print("conflicting redelivery   : {}".format(stats["conflict"]))
    for name, why in rejected[:4]:
        print("   rejected {}: {}".format(name, why))
    print("\nA rejected file is rejected IN FULL. Partial ingestion of a corrupt")
    print("settlement file is how books diverge quietly for a month.")

    # ---- duplicate + conflicting redelivery -------------------------------
    print("\n" + "=" * 78)
    print("IDEMPOTENT FILE HANDLING")
    print("-" * 78)
    dup = DATA / (dup_stem + "_REDELIVERY.txt")
    print("byte-identical redelivery of {}: {}".format(
        dup_stem, "no-op (already ingested)" if stats["duplicate"] else "NOT DETECTED"))

    # Same file_id, one extra line -> must NOT be guessed at.
    original = DATA / (dup_stem + ".txt")
    lines = original.read_text(encoding="utf-8").splitlines()
    tampered = DATA / (dup_stem + "_PLUS_ONE.txt")
    extra = lines[1]
    body = lines[:-1] + [extra]
    count = sum(1 for l in body if l.startswith("D"))
    gross = sum(int(l[25:40]) for l in body if l.startswith("D"))
    fee = sum(int(l[40:52]) for l in body if l.startswith("D"))
    body.append("T{:>10}{:>20}{:>15}".format(count, gross, fee))
    tampered.write_text("\n".join(body) + "\n", encoding="utf-8")
    try:
        svc.ingest(tampered)
        print("same file_id + extra line: ACCEPTED  <- wrong, this should conflict")
    except ConflictingRedelivery as exc:
        print("same file_id + extra line: REJECTED")
        print("   {}".format(str(exc)[:150]))

    # ---- lifecycle ---------------------------------------------------------
    print("\n" + "=" * 78)
    print("TRANSACTION LIFECYCLE")
    print("-" * 78)
    counts = svc.state_counts()
    total = sum(counts.values())
    for state in ("pending", "partially_settled", "settled", "disputed"):
        n = counts.get(state, 0)
        print("{:<20}{:>8,}  {:>7.2%}".format(state, n, n / total))
    print("{:<20}{:>8,}".format("total", total))
    print("\nThe payment is not done when the API returns 200. It is done when the")
    print("settlement file ties out, days later -- and a chargeback can undo that")
    print("a month after THAT.")

    # ---- fee reconciliation ------------------------------------------------
    fv = svc.fee_variance_summary()
    print("\n" + "=" * 78)
    print("FEE RECONCILIATION (tolerance: 2 minor units)")
    print("-" * 78)
    print("variances flagged        : {:,}".format(fv["count"]))
    print("total variance           : ${:,.2f}".format(fv["total_minor"] / 100))
    print("average per variance     : ${:,.4f}".format(fv["avg_minor"] / 100))
    print("\nMateriality is a business call, not an engineering one. The system's job")
    print("is to make the number exact and attributable; whether $0.003 x 2M rows")
    print("is worth a dispute is the finance owner's decision, and they need the")
    print("per-file breakdown to make it.")

    # ---- aged breaks -------------------------------------------------------
    aged = svc.aged_breaks(AS_OF)
    tiers = {}
    for b in aged:
        tiers[b["tier"]] = tiers.get(b["tier"], 0) + 1
    print("\n" + "=" * 78)
    print("AGED BREAKS as of {}  (escalation tiers)".format(AS_OF))
    print("-" * 78)
    for tier in ("T0 monitor", "T1 analyst", "T2 supervisor", "T3 write-off review"):
        print("{:<24}{:>8,}".format(tier, tiers.get(tier, 0)))

    # ---- replay after a logic fix ------------------------------------------
    print("\n" + "=" * 78)
    print("REPLAY AFTER A LOGIC FIX")
    print("-" * 78)
    before = svc.state_counts()
    print("Scenario: the fee expectation dropped the fixed per-transaction")
    print("component ($0.30) and charged only the bps rate. Every settled row")
    print("therefore looks like a fee variance. Fix the rule, replay all {} archived"
          .format(len(files)))
    print("files, and diff -- with no re-request to the processor.\n")

    # The planted bug, applied to the run above: rerun ingestion with a broken
    # fee expectation so the "before" column is what a real broken deploy looks
    # like, then fix it and replay the archive.
    import src.service as service_mod
    correct_fee = service_mod.expected_fee
    service_mod.expected_fee = lambda amt, ccy: (
        abs(amt) * files_mod.FEE_SCHEDULE_BPS[ccy] // 10_000)

    svc.reset_for_replay()
    ingest_all(svc, files)
    buggy_fv = svc.fee_variance_summary()
    buggy_states = svc.state_counts()

    service_mod.expected_fee = correct_fee          # <- the one-line fix
    svc.reset_for_replay()
    replay_stats, _ = ingest_all(svc, files)
    after_fv = svc.fee_variance_summary()
    after = svc.state_counts()

    print("{:<28}{:>16}{:>16}".format("", "with the bug", "after replay"))
    print("{:<28}{:>16,}{:>16,}".format(
        "fee variances", buggy_fv["count"], after_fv["count"]))
    print("{:<28}{:>16}{:>16}".format(
        "total variance",
        "${:,.2f}".format(buggy_fv["total_minor"] / 100),
        "${:,.2f}".format(after_fv["total_minor"] / 100)))
    for state in ("pending", "partially_settled", "settled", "disputed"):
        print("{:<28}{:>16,}{:>16,}".format(
            state, buggy_states.get(state, 0), after.get(state, 0)))

    print("\nreplay ingested {} files, {} duplicate no-ops, {} rejected on control"
          .format(replay_stats["ok"], replay_stats["duplicate"],
                  replay_stats["rejected"]))
    print("totals -- the same control path as the original run, which is what makes")
    print("the counts tie.")
    print("\n{} of {} fee breaks resolved by replaying the archive.".format(
        buggy_fv["count"] - after_fv["count"], buggy_fv["count"]))
    print("The {} that remain are the genuinely mis-deducted fees planted in the".format(
        after_fv["count"]))
    print("files -- real breaks that a processor dispute has to resolve, not ours.")
    print("\nLifecycle states: {}".format(
        "IDENTICAL across both passes -- replay does not double-count"
        if buggy_states == after == before
        else "DIFFERENT -- replay is NOT idempotent"))
    print("=" * 78)
    return 0 if buggy_states == after == before else 1


if __name__ == "__main__":
    raise SystemExit(main())
