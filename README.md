# Reconciliation & Settlement Service

## What it is

When a business takes card payments, the money does not arrive right away. A
card processor sends back **settlement files** a day or two later saying which
payments were paid, how much, and what fees were taken off.

This service reads those files and checks them against our own record of each
payment. Anything that does not agree (a missing payment, a wrong fee, a
chargeback) becomes a **break**: an item a person needs to look at.

It also runs the full daily cycle around that: rejecting bad files, tracking
chargebacks against their deadlines, posting results into the ledger from our
sister project [Double-Entry Ledger](https://github.com/riya0920/double-entry-ledger-core), and keeping old data for as long as the law requires.

## What we did

1. **Built file intake with control totals.** Each file ends with a trailer
   stating the row count and totals. If the rows do not add up, the whole file is
   rejected and nothing is applied.
2. **Tracked each payment's lifecycle**: pending, settled, partially settled or
   disputed, with 1 to 2 day delays and chargebacks up to 30 days old.
3. **Checked fees** against a fee schedule with a stated tolerance, and aged
   breaks through escalation tiers (T0 monitor up to T3 write-off review).
4. **Built replay-after-fix**: fix a rule, replay the archived files, and see
   what changed.
5. **Added multi-pass matching** for payments that do not match exactly, with a
   score and a stored reason for every match.
6. **Modeled the chargeback lifecycle** with calendar-day deadlines.
7. **Linked to the Double-Entry Ledger** (double-entry postings), with failed postings fed
   back into a persistent break queue.
8. **Built a daily cycle** with catch-up, cutoffs and two idempotency guards
   (running it twice does no harm), installed as a real systemd timer.
9. **Added the rest**: a dispute pack for fee errors, an operator dashboard, a
   three-tier retention job, representment (fighting a chargeback) triage, and an
   HTTP API.

Work was done in several passes. Later passes closed gaps the earlier README
listed as open, and running things for real found bugs the tests had missed.

## Results

**File intake**

- 7 of 31 generated files have a bad line and are rejected in full (this rate is
  set high on purpose to test the path).
- Same file id, same bytes: skipped as already done. Same file id, **different
  bytes**: rejected, not merged.

**Lifecycle after one run**

| state | count | share |
|---|---|---|
| pending | 4,934 | 41.12% |
| partially_settled | 269 | 2.24% |
| settled | 6,745 | 56.21% |
| disputed | 52 | 0.43% |

**Replay-after-fix (planted fee bug)**

| | with the bug | after replay |
|---|---|---|
| fee variances | 8,309 | 160 |
| total variance | $2,822.11 | $329.41 |

- 8,149 of 8,309 fee breaks resolved just by replaying the archive.
- Lifecycle counts identical in both runs: replay does not double-count.
- The 160 left are the wrong fees we planted in the files: real disputes with the
  processor.

**Retention** (3,300 transactions over eight years, see RETENTION.md)

- Archive pass moved 1,800 rows; hot store went from 3,300 to 1,500 rows.
- Archive is **15.9x** smaller than the raw data.
- A second run moved 0 rows (all 6 dates skipped as already archived).
- Looking up an archived row took 0.037 s, 96x slower than a hot one.

**Chargebacks** (see REPRESENTMENT.md)

- 28 of 160 open disputes were already past their evidence deadline.
- Retention keeps data 2,015 days longer than the longest dispute window.

**Tests**

- **147 tests**, all passing.

**Bugs found by testing (and fixed)**

- The first replay design skipped duplicate checks, so a resent file was applied
  twice and the counts silently doubled.
- The retention job forgot a date once it was archived, so archived data could
  never be purged.
- A purgeable date was never archived, so purge had to also reach hot storage
  (found by two failing tests).
- The daily cycle passed two arguments to the ledger link, which took one.
- The cycle never passed the archive step, which reported "skipped" every run.
- Ledger feedback made breaks that nothing put into the queue.

## Key decisions and why

**The trailer is the contract; a bad file is rejected whole.**
Applying the good rows of a broken file leaves the books in a state no one can
explain. All or nothing is easy to reason about and easy to retry.

**Same file id with different content is refused, not guessed.**
The processor is either correcting or duplicating. The system cannot tell which,
so it stops and asks a person.

**A fuzzy match only wins if it clearly beats the runner-up.**
Two close candidates mean the evidence does not pick one. Taking the higher
score would be guessing, so the row goes to the break queue instead.

**Every match stores its reason.**
"The system matched it" is not an answer for an auditor. "Amount equal, settled
one day later, same currency" is.

**"Accepted" and "expired" chargebacks are kept apart.**
Both lose money, but one is a choice and the other is our own process failing.
Merging them hides what the process costs.

**Deadlines use calendar days.**
Card network rules count calendar days. Business days would quietly grant extra
days that do not exist.

**The cycle runs for a date, catches up oldest first, and stops at a failure.**
Settlement state builds day on day. Applying a later day before an earlier one
creates states no real sequence of events could produce.

**Time is an input, and a missing file is logged, not raised.**
Passing `now` in lets us test Sundays, month ends and late starts. Before the
18:00 cutoff a missing file is a wait; after it, a failure.

**Two idempotency guards.**
The cycle's state file can be lost or ignored, so the ingest step refuses
duplicates on its own too. A scheduler firing twice is just a retry.

**Posting failures become breaks.**
A swallowed error means settlement and the ledger disagree forever with nothing
pointing at why. Ledger breaks start at T3, because our own view is already
wrong.

**A break keeps its first-seen date.**
If a repeat reset the clock, a three-week-old problem would always look one day
old and never escalate.

**Archive, check, then delete. Purge is a separate flag.**
Deleting before the archive is read back risks losing data in a crash. Purging
cannot be undone, so it never runs by default.

**The dashboard imports its thresholds.**
If the screen kept its own copy of the rules, the two would drift, and people
trust the screen.

## Limits

- The dispute pack is not run on a schedule and has no covering letter or
  attachments. It gives the numbers, not the document.
- Representment has no evidence templates and no submission integration.
- Chargeback win rates are assumed, not measured (we have no real outcomes).
- All files and transactions are generated test data, not a real processor feed.
- The timer never purges; purging stays a manual step.

## How to run

```bash
pip install -r requirements.txt
python run_settlement.py       # intake, lifecycle, fees, replay-after-fix
python run_cycle.py            # daily cycle: catch-up, cutoff, idempotency
python run_ledger_link.py      # post settlements and chargebacks to the Double-Entry Ledger
python run_retention.py        # three-tier retention on eight years of data
python run_representment.py    # which chargebacks to fight, and in what order
python run_dashboard.py        # render the operator dashboard
python -m pytest tests -q      # 147 tests
uvicorn serve:app --port 8200  # API: daily report, break queue, /dashboard
```

Full write-ups: [RETENTION](docs/RETENTION.md) ·
[REPRESENTMENT](docs/REPRESENTMENT.md) · [DASHBOARD](docs/DASHBOARD.md) ·
[dashboard page](docs/dashboard.html)

## Layout

```
src/files.py          fixed-width file parsing, control totals
src/service.py        intake, lifecycle, fees, replay
src/matching.py       four-pass matching with scores
src/chargebacks.py    chargeback lifecycle and deadlines
src/ledger_link.py    postings into the Double-Entry Ledger, feedback
src/break_queue.py    persistent break queue with aging
src/scheduler.py      daily cycle: catch-up, cutoffs, guards
src/dispute_pack.py   fee variances grouped by root cause
src/retention.py      tier policy
src/archival_job.py   archive, verify, delete, purge
src/representment.py  fight-or-fold and deadline queue
src/dashboard.py      operator view and alert rules
serve.py              HTTP API
ops/install_timers.sh systemd timer for the daily cycle
```
