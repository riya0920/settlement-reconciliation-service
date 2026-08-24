"""The job that moves data between tiers, and what it refuses to do."""
import gzip
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.archival_job import (ArchiveVerificationError, coverage_report,
                              evidence_for, run_archival)
from src.retention import ArchiveStore, RetentionPolicy
from src.service import SCHEMA


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _seed(con, dates, per_day=5):
    for d in dates:
        for i in range(per_day):
            ref = "{}-{:03d}".format(d, i)
            con.execute(
                "INSERT INTO txn (ref, auth_date, amount_minor, currency,"
                " settled_minor, fee_minor, state) VALUES (?,?,?,?,?,?,?)",
                (ref, d, 10_000 + i, "USD", 10_000 + i, 59, "settled"))
            con.execute(
                "INSERT INTO match_event (ref, file_id, line_no, rule,"
                " from_state, to_state, amount_minor)"
                " VALUES (?,?,?,?,?,?,?)",
                (ref, "STL{}".format(d.replace("-", "")), i, "exact_ref",
                 "pending", "settled", 10_000 + i))
    con.commit()


AS_OF = "2024-12-31"
HOT = ["2024-12-20", "2024-12-21"]          # inside the 45-day hot window
OLD = ["2023-06-01", "2023-06-02"]          # inside archive/cold
ANCIENT = ["2015-01-05"]                    # past the 7-year legal floor


# ------------------------------------------------------------ tiering
def test_hot_dates_are_left_alone(tmp_path):
    con = _con()
    _seed(con, HOT + OLD)
    res = run_archival(con, ArchiveStore(tmp_path), AS_OF)
    assert set(res.archived_dates) == set(OLD)
    assert con.execute("SELECT COUNT(*) c FROM txn WHERE auth_date IN (?,?)",
                       tuple(HOT)).fetchone()["c"] == 10


def test_archived_rows_leave_the_hot_store(tmp_path):
    con = _con()
    _seed(con, OLD)
    run_archival(con, ArchiveStore(tmp_path), AS_OF)
    assert con.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 0


def test_provenance_travels_with_the_row(tmp_path):
    """Archiving the amount and losing "which file said so, on which line, under
    which rule" keeps the half a representment does not need."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF)
    rows = store.retrieve(OLD[0])
    assert rows and rows[0]["events"]
    assert rows[0]["events"][0]["rule"] == "exact_ref"
    assert rows[0]["events"][0]["file_id"].startswith("STL")


def test_match_events_do_not_survive_their_transaction(tmp_path):
    """Orphaned provenance rows are worse than none: they make a ref look
    present in the hot store when the transaction is gone."""
    con = _con()
    _seed(con, OLD)
    run_archival(con, ArchiveStore(tmp_path), AS_OF)
    assert con.execute("SELECT COUNT(*) c FROM match_event").fetchone()["c"] == 0


# --------------------------------------------------------- idempotency
def test_rerunning_the_job_is_a_no_op(tmp_path):
    """It runs daily and will be re-run after failures. A second file or a
    duplicated row set is the failure this prevents."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    first = run_archival(con, store, AS_OF)
    second = run_archival(con, store, AS_OF)

    assert first.archived_rows == 10
    assert second.archived_rows == 0
    assert set(second.skipped_already_archived) == set(OLD)
    assert len(list(Path(tmp_path).glob("*.jsonl.gz"))) == 2


def test_a_date_archived_yesterday_is_not_rearchived_empty(tmp_path):
    """The dangerous version of non-idempotency: the hot rows are already gone,
    so a second pass would write an EMPTY archive over a good one."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF)
    run_archival(con, store, AS_OF)
    assert len(store.retrieve(OLD[0])) == 5, "the archive was overwritten empty"


# ------------------------------------------------------------ ordering
def test_the_hot_copy_survives_a_failed_archive(tmp_path):
    """Write, read back, verify, THEN delete. Any other order has a window where
    a crash loses the data."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)

    def _lying_rows(c, bdate):
        # Claims more rows than will come back out.
        return [{"ref": "x", "auth_date": bdate}] * 5 + [{"phantom": True}]

    real_retrieve = store.retrieve
    store.retrieve = lambda d: real_retrieve(d)[:1]      # archive reads short

    with pytest.raises(ArchiveVerificationError, match="refusing to delete"):
        run_archival(con, store, AS_OF, rows_for_date=_lying_rows)

    store.retrieve = real_retrieve
    assert con.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 10, (
        "hot rows were deleted against an archive that did not verify")


# --------------------------------------------------------------- purge
def test_a_purgeable_date_is_never_archived_on_the_way_past_the_floor(tmp_path):
    """Archiving data you are already permitted to delete buys nothing, so a
    date that is past the legal floor is left where it is and marked eligible.

    Which is why purge has to reach into hot storage and not only into the
    archive: a purgeable date may never have been archived at all.
    """
    con = _con()
    _seed(con, ANCIENT)
    store = ArchiveStore(tmp_path)
    res = run_archival(con, store, AS_OF)

    assert res.archived_dates == []
    assert res.purge_eligible == ANCIENT
    assert store.retrieve(ANCIENT[0]) == []
    assert con.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 5


def test_purging_is_off_by_default(tmp_path):
    """Archiving is reversible and purging is not. They are two verbs and they
    take two flags."""
    con = _con()
    _seed(con, ANCIENT)
    store = ArchiveStore(tmp_path)

    res = run_archival(con, store, AS_OF)
    assert res.purge_eligible == ANCIENT
    assert res.purged_dates == []
    assert con.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 5, (
        "purged without being asked")


def test_purging_requires_the_flag_and_records_the_deletion(tmp_path):
    con = _con()
    _seed(con, ANCIENT)
    store = ArchiveStore(tmp_path)
    res = run_archival(con, store, AS_OF, purge=True)

    assert res.purged_dates == ANCIENT
    assert con.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 0
    hist = store.purge_history()
    assert len(hist) == 1 and hist[0]["business_date"] == ANCIENT[0]
    assert hist[0]["rows"] == 5
    assert "never archived" in hist[0]["reason"], (
        "a hot-only purge must say so -- otherwise the log implies an archive "
        "file was deleted and an auditor goes looking for one")


def test_purging_reaches_an_archived_date_too(tmp_path):
    """The other half of the same bug. A date archived years ago and now past
    the floor has to be reachable by the purge, and it is only reachable because
    the job's date list includes the archive directory."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF)                    # OLD goes to the archive

    # Now stand far enough in the future that OLD is past the legal floor.
    much_later = "2032-01-01"
    res = run_archival(con, store, much_later, purge=True)
    assert set(res.purged_dates) == set(OLD)
    assert store.retrieve(OLD[0]) == []
    assert {e["business_date"] for e in store.purge_history()} == set(OLD)


def test_a_purge_of_a_hot_only_date_does_not_leave_orphaned_provenance(tmp_path):
    con = _con()
    _seed(con, ANCIENT)
    run_archival(con, ArchiveStore(tmp_path), AS_OF, purge=True)
    assert con.execute("SELECT COUNT(*) c FROM match_event").fetchone()["c"] == 0


def test_a_date_inside_the_legal_floor_is_never_purge_eligible(tmp_path):
    """The floor is a minimum time to KEEP, not a deadline to delete on."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    res = run_archival(con, store, AS_OF, purge=True)
    assert res.purge_eligible == []
    assert res.purged_dates == []


# ------------------------------------------------------------- dry run
def test_a_dry_run_changes_nothing(tmp_path):
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    res = run_archival(con, store, AS_OF, dry_run=True)

    assert set(res.archived_dates) == set(OLD)
    assert res.archived_rows == 10
    assert con.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 10
    assert list(Path(tmp_path).glob("*.jsonl.gz")) == []


def test_a_dry_run_never_purges_even_when_asked(tmp_path):
    con = _con()
    _seed(con, ANCIENT)
    store = ArchiveStore(tmp_path)
    res = run_archival(con, store, AS_OF, purge=True, dry_run=True)
    assert res.purged_dates == []
    assert res.purge_eligible == ANCIENT, "the plan should still say what it would do"
    assert con.execute("SELECT COUNT(*) c FROM txn").fetchone()["c"] == 5
    assert store.purge_history() == []


# ---------------------------------------------------------- retrieval
def test_a_dispute_for_archived_data_can_still_be_evidenced(tmp_path):
    """The whole reason the archive exists. A chargeback arrives for a
    transaction outside the hot window, and the answer has to be evidence rather
    than an apology."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF)

    ref = "{}-002".format(OLD[0])
    found = evidence_for(con, store, ref, search_dates=OLD)
    assert found is not None
    assert found["tier"] == "archive"
    assert found["found_in"] == OLD[0]
    assert found["events"][0]["rule"] == "exact_ref"


def test_the_lookup_says_which_tier_answered(tmp_path):
    """"Found hot" and "found in the archive, took 400ms" are the same answer to
    the dispute and completely different answers to a capacity question."""
    con = _con()
    _seed(con, HOT + OLD)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF)

    assert evidence_for(con, store, "{}-001".format(HOT[0]),
                        search_dates=OLD)["tier"] == "hot"
    assert evidence_for(con, store, "{}-001".format(OLD[0]),
                        search_dates=OLD)["tier"] == "archive"


def test_an_unknown_reference_returns_none_rather_than_an_empty_record(tmp_path):
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF)
    assert evidence_for(con, store, "no-such-ref", search_dates=OLD) is None


def test_a_purged_reference_is_not_found_and_the_purge_is_on_record(tmp_path):
    """The distinction that matters to an auditor: 'gone' with a dated reason is
    a defensible answer, and 'we have no idea' is not."""
    con = _con()
    _seed(con, ANCIENT)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF, purge=True)

    ref = "{}-000".format(ANCIENT[0])
    assert evidence_for(con, store, ref, search_dates=ANCIENT) is None
    assert store.purge_history()[0]["reason"]


# ----------------------------------------------------------- coverage
def test_every_date_is_accounted_for_across_the_tiers(tmp_path):
    """The question an auditor asks is not 'what is your policy' but 'where is
    the data for 14 March'."""
    con = _con()
    _seed(con, HOT + OLD + ANCIENT)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF, purge=True)

    cov = coverage_report(con, store, AS_OF)
    assert cov["hot_dates"] == 2
    assert cov["archived_dates"] == 2
    assert cov["purged_dates"] == 1
    assert cov["total_accounted"] == 5
    assert cov["duplicated"] is False


def test_a_date_present_in_two_tiers_is_reported_as_duplicated(tmp_path):
    """Not a hole but the opposite failure, and it means a delete did not
    happen: the data would be found twice and could disagree with itself."""
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    store.archive(OLD[0], [{"ref": "manual", "auth_date": OLD[0]}])

    cov = coverage_report(con, store, AS_OF)
    assert cov["duplicated"] is True
    assert cov["in_both_tiers"] == [OLD[0]]


# ---------------------------------------------------------- compression
def test_the_archive_is_actually_smaller_than_what_it_replaced(tmp_path):
    """Reported rather than assumed. An 'archive' that costs the same as hot
    storage has bought nothing but latency."""
    con = _con()
    _seed(con, OLD, per_day=400)
    res = run_archival(con, ArchiveStore(tmp_path), AS_OF)
    assert res.compression_ratio > 3.0, (
        "gzipped JSONL should compress repetitive settlement rows heavily; "
        "ratio was {:.2f}".format(res.compression_ratio))


def test_the_archive_file_is_genuinely_gzipped(tmp_path):
    con = _con()
    _seed(con, OLD)
    store = ArchiveStore(tmp_path)
    run_archival(con, store, AS_OF)
    raw = (Path(tmp_path) / "{}.jsonl.gz".format(OLD[0])).read_bytes()
    assert raw[:2] == b"\x1f\x8b"
    assert gzip.decompress(raw).count(b"\n") == 5
