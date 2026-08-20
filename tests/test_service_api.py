"""HTTP API and retention policy."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import serve
from src.retention import ArchiveStore, RetentionPolicy, plan_retention


@pytest.fixture(scope="module")
def client():
    with TestClient(serve.app) as c:
        yield c


# ----------------------------------------------------------------- the API
def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_daily_report_carries_the_close_numbers(client):
    b = client.get("/report/daily").json()
    assert set(b["lifecycle"]) & {"settled", "pending"}
    assert 0 <= b["match_rate"] <= 1
    assert "total_display" in b["fee_variance"]
    assert b["unexplained_variance_at_close"] == 0


def test_corrupt_file_is_422_not_500(client):
    """A file that fails its control totals is semantically invalid, not a
    server error -- the distinction decides whether the processor retries."""
    from src.files import DATA
    bad = DATA / "BADFILE.txt"
    bad.write_text("H{:<12}{:<8}{:<10}\nT{:>10}{:>20}{:>15}\n".format(
        "BADFILE", "20260401", "PROC", 99, 12345, 6), encoding="utf-8")
    r = client.post("/files/ingest", json={"file_name": "BADFILE.txt"})
    assert r.status_code == 422
    assert "CONTROL TOTAL" in r.json()["detail"]


def test_unknown_file_is_404(client):
    assert client.post("/files/ingest",
                       json={"file_name": "nope.txt"}).status_code == 404


def test_breaks_can_be_filtered_by_tier(client):
    everything = client.get("/breaks").json()
    if everything["count"]:
        tier = everything["items"][0]["tier"]
        filtered = client.get("/breaks", params={"tier": tier}).json()
        assert all(i["tier"] == tier for i in filtered["items"])


def test_unknown_transaction_is_404(client):
    assert client.get("/transactions/NOPE-123").status_code == 404


def test_retention_plan_validates_itself_against_dispute_ages(client):
    b = client.get("/retention/plan").json()
    assert b["policy"]["archive_days"] > b["policy"]["hot_days"]
    assert "covers_observed_disputes" in b["validation"]


# ------------------------------------------------------------- retention
def test_tiers_are_assigned_by_age():
    p = RetentionPolicy()
    assert p.tier_for("2026-05-01", "2026-05-10") == "hot"
    assert p.tier_for("2026-01-01", "2026-05-10") == "archive"
    assert p.tier_for("2020-01-01", "2026-05-10") == "cold"
    assert p.tier_for("2010-01-01", "2026-05-10") == "purgeable"


def test_policy_flags_a_window_shorter_than_observed_disputes():
    """A retention window shorter than the dispute tail is not a storage
    decision, it is a decision to lose winnable disputes."""
    short = RetentionPolicy(archive_days=90)
    v = short.validate_against(400)
    assert v["covers_observed_disputes"] is False
    assert "SHORTER" in v["verdict"]

    ok = RetentionPolicy().validate_against(400)
    assert ok["covers_observed_disputes"] is True


def test_archived_transaction_is_retrievable(tmp_path):
    """'We still have it, it just takes an hour' and 'it is gone' are different
    answers to a dispute."""
    store = ArchiveStore(tmp_path)
    store.archive("2026-01-15", [{"ref": "AUTH1", "amount_minor": 5000},
                                 {"ref": "AUTH2", "amount_minor": 7000}])
    found = store.find_transaction("AUTH2", ["2026-01-15"])
    assert found and found["amount_minor"] == 7000
    assert found["tier"] == "archive"
    assert store.find_transaction("MISSING", ["2026-01-15"]) is None


def test_purge_is_recorded(tmp_path):
    """Deleting without recording the deletion turns a defensible policy into an
    unexplainable hole."""
    store = ArchiveStore(tmp_path)
    store.archive("2019-01-01", [{"ref": "OLD1"}, {"ref": "OLD2"}])
    assert store.purge("2019-01-01", reason="past legal floor") is True
    assert store.retrieve("2019-01-01") == []

    history = store.purge_history()
    assert len(history) == 1
    assert history[0]["rows"] == 2
    assert history[0]["reason"] == "past legal floor"
    assert history[0]["purged_at"]


def test_purging_a_missing_date_is_a_noop(tmp_path):
    store = ArchiveStore(tmp_path)
    assert store.purge("1999-01-01", reason="x") is False
    assert store.purge_history() == []


def test_plan_partitions_every_date_exactly_once():
    dates = ["2026-05-01", "2026-01-01", "2020-01-01", "2010-01-01"]
    plan = plan_retention(dates, "2026-05-10")
    assert sum(len(v) for v in plan.values()) == len(dates)
