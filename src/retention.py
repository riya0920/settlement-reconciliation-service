"""Retention and archival, driven by how far back disputes actually reach.

The spec's question -- "a chargeback arrives for a transaction your retention
policy archived, now what?" -- has a wrong answer and a right one.

Wrong: set retention from a storage budget, discover the gap when a dispute
arrives, and lose it for want of evidence. A dispute you cannot evidence is a
dispute you lose, so a retention policy that is shorter than the dispute window
is a decision to pay chargebacks you could have won.

Right: derive retention from the observed dispute-age distribution, keep the
tail with margin, and make archived data RETRIEVABLE rather than deleted --
because "we still have it, it just takes an hour" is a completely different
conversation from "it is gone".

Three tiers, which is what real systems settle on:

  HOT      recent, indexed, queried by the daily reconciliation. Expensive.
  ARCHIVE  older than the hot window, compressed, still retrievable on demand.
           Cheap per byte, slow per query, and that trade is the entire point.
  PURGED   past the legal retention floor. Actually gone, and the fact of the
           purge is itself recorded -- an auditor asks what happened to it, and
           "we have no idea" is a worse answer than "purged on this date under
           this policy".

The purge log is the part people skip. Deleting data without recording that you
deleted it turns a defensible policy into an unexplainable hole.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# Card scheme dispute windows run to 120 days for most reason codes and longer
# for some (services not rendered can reach 540). Hot retention has to cover the
# common case; the archive has to cover the tail.
HOT_DAYS = 45
ARCHIVE_DAYS = 545          # 540 + margin
LEGAL_FLOOR_DAYS = 2555     # ~7 years, the usual financial-records requirement


@dataclass
class RetentionPolicy:
    hot_days: int = HOT_DAYS
    archive_days: int = ARCHIVE_DAYS
    legal_floor_days: int = LEGAL_FLOOR_DAYS

    def tier_for(self, record_date: str, as_of: str) -> str:
        age = (date.fromisoformat(as_of) - date.fromisoformat(record_date)).days
        if age <= self.hot_days:
            return "hot"
        if age <= self.archive_days:
            return "archive"
        if age <= self.legal_floor_days:
            return "cold"        # past the dispute window, inside legal retention
        return "purgeable"

    def validate_against(self, max_observed_dispute_age_days: int) -> dict:
        """Check the policy against what disputes actually do.

        A retention window shorter than the observed dispute tail is not a
        storage decision, it is a decision to lose winnable disputes.
        """
        covered = max_observed_dispute_age_days <= self.archive_days
        return {
            "max_observed_dispute_age_days": max_observed_dispute_age_days,
            "archive_days": self.archive_days,
            "covers_observed_disputes": covered,
            "margin_days": self.archive_days - max_observed_dispute_age_days,
            "verdict": ("archive window covers the observed dispute tail"
                        if covered else
                        "ARCHIVE WINDOW IS SHORTER THAN OBSERVED DISPUTES -- "
                        "evidence will be missing when it is needed"),
        }


class ArchiveStore:
    """Gzipped JSONL per business date. Slow to query, cheap to keep, and above
    all RETRIEVABLE -- which is the only property that matters when a dispute
    arrives for something outside the hot window."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.purge_log = self.root / "purge_log.jsonl"

    def _path(self, business_date: str) -> Path:
        return self.root / "{}.jsonl.gz".format(business_date)

    def archive(self, business_date: str, rows: list[dict]) -> Path:
        p = self._path(business_date)
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        return p

    def retrieve(self, business_date: str) -> list[dict]:
        p = self._path(business_date)
        if not p.exists():
            return []
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def find_transaction(self, ref: str, search_dates: list[str]) -> dict | None:
        """Retrieval by reference across archived dates.

        Linear and slow on purpose -- the archive is not indexed, because
        indexing it would cost most of what archiving saved. An hour to answer a
        dispute is acceptable; losing the dispute is not.
        """
        for d in search_dates:
            for row in self.retrieve(d):
                if row.get("ref") == ref:
                    return {**row, "found_in": d, "tier": "archive"}
        return None

    def purge(self, business_date: str, reason: str, actor: str = "retention-job") -> bool:
        """Delete, and RECORD the deletion.

        Deleting without recording turns a defensible policy into an
        unexplainable hole. An auditor asking what happened to a record needs a
        better answer than "we have no idea".
        """
        p = self._path(business_date)
        if not p.exists():
            return False
        rows = len(self.retrieve(business_date))
        size = p.stat().st_size
        p.unlink()
        with self.purge_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "business_date": business_date, "rows": rows,
                "bytes": size, "reason": reason, "actor": actor,
                "purged_at": date.today().isoformat(),
            }) + "\n")
        return True

    def purge_history(self) -> list[dict]:
        if not self.purge_log.exists():
            return []
        return [json.loads(l) for l in
                self.purge_log.read_text(encoding="utf-8").splitlines() if l.strip()]


def plan_retention(business_dates: list[str], as_of: str,
                   policy: RetentionPolicy | None = None) -> dict:
    policy = policy or RetentionPolicy()
    plan: dict[str, list[str]] = {"hot": [], "archive": [], "cold": [],
                                  "purgeable": []}
    for d in business_dates:
        plan[policy.tier_for(d, as_of)].append(d)
    return plan
