# SE-2 — Reconciliation & Settlement Service

**Status: ~20% slice.** File-handling discipline, the transaction lifecycle, fee
reconciliation, break aging, and the replay-after-fix demo are built. There is no
service, no API, no dashboard, and no ledger integration.

```bash
python run_settlement.py
```

## What is built

**Control-total-gated ingestion** (`src/files.py`). Fixed-width header/detail/
trailer files; the trailer is the contract. Parsed detail records must tie to the
declared count, gross total, and fee total or the **whole file is rejected** and
nothing is applied. 7 of 31 generated files carry a malformed line and are
rejected in full — that rate is deliberately exaggerated to exercise the path.

**File idempotency, with the hard case handled**:

| Delivery | Behaviour |
|---|---|
| same `file_id`, byte-identical | no-op — already ingested |
| same `file_id`, **different content** | **rejected**, not merged |

The second is the one that matters. A processor re-sending a file with one extra
line is either correcting or duplicating, and the system must not guess. It
refuses and asks.

**Transaction lifecycle**, not a diff: `pending → settled / partially_settled /
disputed`, with T+1/T+2 lag, partial settlements, and chargebacks referencing
transactions up to 30 days old.

```
pending                4,934   41.12%
partially_settled        269    2.24%
settled                6,745   56.21%
disputed                  52    0.43%
```

**Provenance on every state change** — `match_event` records ref, file id, line
number, the rule that fired, and the from/to states.

**Fee reconciliation** against a defined schedule with a stated tolerance, and
**break aging with escalation tiers** (T0 monitor → T3 write-off review).

## The replay-after-fix demo

A planted bug: the fee expectation drops the fixed $0.30 per-transaction
component and charges only the bps rate, so nearly every settled row looks like a
variance. Fix the rule, replay all 31 archived files, diff:

```
                                with the bug    after replay
fee variances                          8,309             160
total variance                     $2,822.11         $329.41
pending                                4,934           4,934
partially_settled                        269             269
settled                                6,745           6,745
disputed                                  52              52

8,149 of 8,309 fee breaks resolved by replaying the archive.
Lifecycle states: IDENTICAL across both passes -- replay does not double-count
```

The 160 survivors are the genuinely mis-deducted fees planted in the files —
real breaks for a processor dispute, not ours. No file was re-requested from the
processor; the archive was enough.

**This demo caught a real bug in my own replay design.** The first version kept
the ingested-file ledger across a replay and passed an `allow_replay` flag that
skipped duplicate detection. The redelivered file was then applied twice and the
lifecycle counts did not match the original run — a settlement replay that
silently double-counts, which is the exact failure it is supposed to be immune
to. `reset_for_replay()` now clears the file ledger so replay runs the *same*
control path as the original ingest, and the counts tie.

## What is NOT built (the other 80%)

1. **No service.** Library + script only: no API, no scheduler, no daily run
   automation, no alerting.
2. **No SE-1 ledger integration.** Chargebacks and settlements post no journal
   entries. The pairing that makes both projects worth more is not wired.
3. **Matching is single-pass on exact reference.** No amount+date-window pass,
   no candidate scoring for residuals — so a settlement row whose reference is
   mangled is simply unmatched. This is the biggest functional gap.
4. **Chargeback lifecycle is a state flip.** No dispute record, no evidence
   deadline countdown, no representment, no write-off flow.
5. **No dashboard**: file-level received/parsed/rejected view, break queue UI,
   and the daily rec report exist only as console output.
6. **Retention/archival** — the "chargeback for an archived transaction" case is
   named in the spec and not handled here.
7. **No tests.** This project has none, which is a real gap next to SE-1 and
   DATA-1; the replay assertion in `run_settlement.py` (states must tie) is the
   only automated check.
