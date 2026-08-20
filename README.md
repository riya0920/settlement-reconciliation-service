# SE-2 — Reconciliation & Settlement Service

**Status: ~75%.** File-handling discipline, transaction lifecycle, fee
reconciliation, break aging, replay-after-fix, multi-pass candidate matching, the
full chargeback lifecycle, and **double-entry postings into SE-1's ledger** (17
tests). There is still no service, no API and no dashboard.

```bash
python run_settlement.py      # ingestion, lifecycle, fees, replay-after-fix
python run_ledger_link.py     # post settlements + chargebacks to SE-1's ledger
python -m pytest tests -q     # 17 matching + chargeback lifecycle tests
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

## Multi-pass matching (`src/matching.py`)

Exact-reference matching handles the easy 95% and calls the rest "unmatched",
which pushes the actual work onto a human. Four passes now, each less certain
than the last and each recording *why* it fired:

| pass | rule | certainty |
|---|---|---|
| 1 | exact reference + amount | 1.00 |
| 2 | reference agrees, amount within tolerance | 0.95 |
| 3 | no reference — amount + date window + currency, scored | 0.60–0.90 |
| 4 | residual → break, with the rejected runner-up recorded | — |

Pass 3 is where judgement enters, so the score is **additive and every component
is stored**. "The system matched it" is not an answer to an auditor; "amount
agreed exactly (+0.50), settled one business day later (+0.20), currency matched
(+0.15)" is.

The rule that keeps it honest: **a candidate only wins if it beats the runner-up
by a margin.** Two plausible candidates mean the evidence does not identify
either one, and taking the higher score is guessing with extra steps. Ambiguous
rows go to the break queue for a human —
`test_ambiguous_candidates_are_refused_not_guessed` pins it.

## Chargeback lifecycle (`src/chargebacks.py`)

A chargeback is not a negative settlement row. It is a dispute with a clock:

```
received -> evidence_due -> represented -> won | lost
     |                                       |
     +-> accepted (a decision) ---------------+
     +-> expired (an operational failure) ----+
```

`accepted` and `expired` both end in a loss, and separating them is the point —
one is a decision, the other is your own process losing money, and a team that
reports them together can never tell how much the process costs.

Deadlines are **calendar days**, because card network rules count calendar days;
using business days would silently grant several days that do not exist. Evidence
submitted after the deadline raises rather than being accepted and quietly lost.

## What is NOT built

1. **No service.** Library + scripts: no API, no scheduler, no daily automation,
   no alerting. This is now the biggest gap.
2. **The ledger link is one-directional.** `run_ledger_link.py` posts settlements
   and chargebacks into SE-1's journal and proves the books balance and survive
   replay, but nothing feeds the ledger's view BACK into the break queue -- so a
   posting failure would not raise a reconciliation break.
4. **No dashboard**: file-level received/parsed/rejected, break queue, and the
   daily rec report exist only as console output.
5. **Retention/archival** — `aged_reference_report()` computes how far back
   disputes reach (which is what should *drive* retention policy), but no
   archival tier exists and the "chargeback for an archived transaction" case is
   still unhandled.
6. **Representment evidence** is a state, not a document workflow: no evidence
   templates, no submission integration, no win-rate tracking by reason code.
7. **Fee variance materiality** is flagged per transaction but not aggregated
   into a monthly dispute pack with a materiality threshold.
