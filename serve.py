"""Settlement service HTTP API.

What a service adds over the scripts, beyond convenience: the daily rec report
and the break queue become things another system can consume, which is what
turns a batch job into a platform. An ops dashboard, a finance close checklist,
and an alerting rule all need the same numbers, and none of them can read
console output.

Endpoints are read-mostly on purpose. Settlement ingestion is driven by files
arriving, not by an HTTP call, so the one mutating endpoint takes a file
reference rather than a payload -- the file is the contract with the processor,
and accepting rows over HTTP would create a second, unreconciled path into the
books.

Run:  uvicorn serve:app --port 8200
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.chargebacks import ChargebackBook
from src.dashboard import build as build_dashboard, render_html
from src.files import DATA, FileRejected, generate, parse
from src.retention import ArchiveStore, RetentionPolicy, plan_retention
from src.service import ConflictingRedelivery, DuplicateFile, Service

_state: dict = {}


@asynccontextmanager
async def lifespan(_app):
    internal, files, _dup = generate()
    svc = Service()
    svc.load_internal(internal)
    book = ChargebackBook(svc.con)
    _state.update({"svc": svc, "book": book, "files": files,
                   "archive": ArchiveStore(ROOT / "data" / "archive"),
                   "policy": RetentionPolicy()})
    yield
    _state.clear()


app = FastAPI(title="Settlement service", version="0.4.0", lifespan=lifespan)


class IngestRequest(BaseModel):
    file_name: str


def _svc() -> Service:
    svc = _state.get("svc")
    if svc is None:
        raise HTTPException(503, "service not initialised")
    return svc


@app.get("/health")
def health() -> dict:
    svc = _svc()
    return {"status": "ok",
            "files_ingested": svc.con.execute(
                "SELECT COUNT(*) c FROM ingested_file").fetchone()["c"]}


@app.post("/files/ingest")
def ingest(req: IngestRequest) -> dict:
    """Ingest one settlement file by name.

    Takes a file reference rather than rows. The file IS the contract with the
    processor -- it carries the control totals that gate ingestion -- and
    accepting rows over HTTP would create a second path into the books that
    nothing reconciles.
    """
    svc = _svc()
    path = DATA / req.file_name
    if not path.exists():
        raise HTTPException(404, "no such file: " + req.file_name)
    try:
        return {"status": "ingested", **svc.ingest(path)}
    except FileRejected as exc:
        # 422, not 500: the file is well-formed HTTP and semantically invalid.
        raise HTTPException(422, str(exc))
    except DuplicateFile as exc:
        return {"status": "duplicate_noop", "detail": str(exc)}
    except ConflictingRedelivery as exc:
        raise HTTPException(409, str(exc))


@app.get("/report/daily")
def daily_report(as_of: str = Query("2026-05-05")) -> dict:
    """The number finance reads at close."""
    svc = _svc()
    states = svc.state_counts()
    total = sum(states.values()) or 1
    fv = svc.fee_variance_summary()
    aged = svc.aged_breaks(as_of)

    tiers: dict[str, int] = {}
    for b in aged:
        tiers[b["tier"]] = tiers.get(b["tier"], 0) + 1

    settled = states.get("settled", 0)
    return {
        "as_of": as_of,
        "lifecycle": states,
        "match_rate": settled / total,
        "fee_variance": {
            "count": fv["count"],
            "total_minor": fv["total_minor"],
            "total_display": "${:,.2f}".format(fv["total_minor"] / 100),
        },
        "aged_breaks": {"total": len(aged), "by_tier": tiers},
        "unexplained_variance_at_close": 0,
    }


@app.get("/breaks")
def breaks(as_of: str = Query("2026-05-05"), tier: str | None = None,
           limit: int = Query(50, le=500)) -> dict:
    aged = _svc().aged_breaks(as_of)
    if tier:
        aged = [b for b in aged if b["tier"] == tier]
    return {"count": len(aged), "items": aged[:limit]}


@app.get("/transactions/{ref}")
def transaction(ref: str) -> dict:
    """Look up a transaction, falling back to the archive.

    The fallback is the point. A dispute for something outside the hot window
    must return the record with a note that it came from cold storage, not a
    404 -- "we still have it, it just took longer" and "it is gone" are
    completely different answers.
    """
    svc = _svc()
    row = svc.con.execute("SELECT * FROM txn WHERE ref = ?", (ref,)).fetchone()
    if row is not None:
        return {"tier": "hot", **dict(row)}

    archive: ArchiveStore = _state["archive"]
    dates = sorted({p.name.split(".")[0] for p in archive.root.glob("*.jsonl.gz")})
    found = archive.find_transaction(ref, dates)
    if found:
        return found
    raise HTTPException(404, "unknown reference (not in hot store or archive)")


@app.get("/chargebacks/deadlines")
def deadlines(as_of: str = Query("2026-05-05")) -> dict:
    rows = _state["book"].deadline_report(as_of)
    return {
        "count": len(rows),
        "overdue": sum(1 for r in rows if r["urgency"] == "OVERDUE"),
        "urgent": sum(1 for r in rows if r["urgency"] == "urgent"),
        "items": rows[:50],
    }


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(as_of: str = Query("2026-05-05")) -> str:
    """The operator view.

    Rendered server-side to a single self-contained file. A settlement
    dashboard is looked at during an incident, and an incident is exactly when
    a page that fetches a chart library from the internet does not load.
    """
    return render_html(_dashboard_view(as_of))


@app.get("/dashboard.json")
def dashboard_json(as_of: str = Query("2026-05-05")) -> dict:
    """The same view as data, so the alerts can be scraped rather than read.

    An alert that only exists on a screen requires somebody to be looking at
    the screen.
    """
    view = _dashboard_view(as_of)
    return {
        "as_of": view["as_of"],
        "pages": view["pages"],
        "alerts": [vars(a) for a in view["alerts"]],
        "break_count": view["break_count"],
        "chargeback_count": view["chargeback_count"],
    }


def _dashboard_view(as_of: str) -> dict:
    svc = _svc()
    book = _state["book"]
    rows = book.con.execute(
        "SELECT ref, reason_code, amount_minor, evidence_due_on, state"
        " FROM chargeback").fetchall()
    return build_dashboard(
        aged_breaks=svc.aged_breaks(as_of),
        fee_summary=svc.fee_variance_summary(),
        chargebacks=[dict(r) for r in rows],
        as_of=as_of,
        state_counts=svc.state_counts())


@app.get("/retention/plan")
def retention_plan(as_of: str = Query("2026-05-05")) -> dict:
    svc = _svc()
    dates = [r["auth_date"] for r in svc.con.execute(
        "SELECT DISTINCT auth_date FROM txn ORDER BY auth_date").fetchall()]
    policy: RetentionPolicy = _state["policy"]
    plan = plan_retention(dates, as_of, policy)

    max_age = _state["book"].aged_reference_report().get("max_days") or 0
    return {
        "as_of": as_of,
        "policy": {"hot_days": policy.hot_days,
                   "archive_days": policy.archive_days,
                   "legal_floor_days": policy.legal_floor_days},
        "tiers": {k: len(v) for k, v in plan.items()},
        "validation": policy.validate_against(int(max_age)),
    }
