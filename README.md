# SE-2 — Reconciliation & Settlement Service

**Status: ~95%.** File-handling discipline, transaction lifecycle, fee
reconciliation, break aging, replay-after-fix, multi-pass candidate matching, the
full chargeback lifecycle, **double-entry postings into SE-1's ledger with
feedback into the break queue**, an HTTP API, a three-tier retention policy, and
a **schedulable daily cycle with catch-up, cutoffs and two independent
idempotency guards** -- **45 tests**.

```bash
python run_settlement.py      # ingestion, lifecycle, fees, replay-after-fix
python run_cycle.py           # the daily cycle: catch-up, cutoff, idempotency
python run_ledger_link.py     # post settlements + chargebacks to SE-1's ledger
python -m pytest tests -q     # 45 tests
uvicorn serve:app --port 8200 # daily report, break queue, retention plan
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

## The daily cycle

`src/scheduler.py` turns the pipeline into a job something can call at a cutoff.
Four properties, each earning its place:

**Catch-up, oldest first.** The job runs *for* a business date, not for "today".
A cycle that only processes today silently drops the day the box was down, and
nobody finds out until a month-end that does not tie. Settlement state is
cumulative, so catch-up runs oldest first and **stops at a failure rather than
stepping over it** — a later day applied before an earlier one transitions a book
that has not received the earlier day's rows, producing a set of states no
sequence of events could have created.

**A cutoff, not a clock.** `now` is a parameter. The same absent file gets two
different verdicts:

```
2026-04-03  await_file  skipped  file has not arrived; still inside the 18:00 window
2026-04-03  await_file  failed   file has not arrived and the 18:00 cutoff has passed
```

A job that raises at 09:00 because the file usually arrives at 10:00 trains its
operators to close the alert unread. And a job that reads the system clock cannot
be tested for what it does on a Sunday, at a month end, or when it starts late —
which are the three cases that break it.

**A missing file is recorded, not raised.** "The file did not arrive" and "the
job did not run" are the same silence at the time and completely different
incidents afterwards. The run exists in the log either way, so the difference
survives.

**Two idempotency guards, because they fail differently.**

```
GUARD 1 -- the cycle skips what is on record.
   completed dates re-attempted : 0
GUARD 2 -- and if something calls it anyway, ingestion refuses.
   forced re-run of 2026-04-01 -> ingest skipped: already ingested, identical content
```

The state file is bookkeeping: it can be deleted, restored from a stale backup,
or simply not consulted by whatever fired the job. File-level idempotency lives
in the service and holds regardless. This project learned that the expensive way
— an earlier replay kept the ingested-file ledger and skipped duplicate
detection, so a redelivered file applied twice and the lifecycle counts silently
doubled. **A scheduler that fires twice is not an exotic failure; it is a retry.**

One detail worth the line it takes: a byte-identical redelivery is **skipped**,
not failed. Processors resend files routinely, and an incident raised every time
one does is an alert that means nothing. Same id with *different* content is a
different matter and still fails — the processor is either correcting or
duplicating and the system must not guess.

## The ledger link now points both ways

`_post` swallowed exceptions and returned `None`. That is the worst outcome
available: the settlement service goes on believing the transaction settled, the
general ledger never hears about it, and the two disagree by exactly that amount
forever with nothing anywhere pointing at the row. **A reconciliation platform
that loses postings silently becomes the thing that needs reconciling.**

Two additions close the loop:

- `unposted_breaks()` turns every posting failure into a `ledger_unposted` break
  carrying the file coordinate that produced it, so it ages and escalates like
  any other break rather than vanishing.
- `reconcile_to_ledger(expected_minor, account)` compares what settlement thinks
  it moved against what the journal holds. They must agree to the minor unit, and
  a difference is a `ledger_divergence` break — a control the one-directional
  link could not have had.

## What is NOT built

1. **A scheduler.** `run_cycle` is the job and something outside still has to
   call it at the cutoff — cron, Airflow, a systemd timer. That is deliberate for
   the same reason DATA-1 gives: a scheduler embedded in the application is one
   nobody can inspect, pause or back-fill from. But it means nothing here fires
   on its own.
2. **No dashboard.** The API returns the numbers; nothing renders them, and there
   is no alert rule on aged breaks or fee variance.
3. **The archive is still not populated by the pipeline.** `src/retention.py`
   tiers, archives, retrieves and purges with a recorded purge log, and the cycle
   has an `archive` step — but the runner passes no archive callable, so tiering
   is exercised by tests rather than by use.
4. **Representment evidence** is a state, not a document workflow: no evidence
   templates, no submission integration, no win-rate tracking by reason code.
5. **Fee variance materiality** is flagged per transaction and summarised per
   cycle, but not aggregated into a monthly dispute pack with a materiality
   threshold and a covering position.
6. **The break queue does not consume the ledger feedback.** `unposted_breaks()`
   and `reconcile_to_ledger()` produce break-shaped records; nothing yet inserts
   them into the persistent queue, so the loop is closed in the library and not
   in the running pipeline.
