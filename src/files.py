"""Settlement file format, generation, and defensive parsing.

Format (fixed-width, header/detail/trailer -- the shape processors actually
send):

  H  file_id(12) business_date(8) processor(10)
  D  ref(16) settled_date(8) gross_minor(15) fee_minor(12) ccy(3) type(10)
  T  record_count(10) gross_total(20) fee_total(15)

The trailer is the contract. If the parsed detail records disagree with it, the
file is corrupt and the correct action is to reject the WHOLE file, alert, and
process nothing -- partial ingestion of a corrupt settlement file is how books
diverge silently for a month.

Planted realities in the generated files:
  T+1/T+2 settlement lag        chargebacks referencing month-old transactions
  fees deducted per schedule    partial settlements
  FX conversion on non-USD      occasional malformed lines
  duplicate file delivery       (same file_id sent twice)
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

FEE_SCHEDULE_BPS = {"USD": 29, "EUR": 34, "GBP": 34}
FEE_FIXED_MINOR = 30


@dataclass
class InternalTxn:
    ref: str
    auth_date: str
    amount_minor: int
    currency: str


@dataclass
class SettlementRow:
    ref: str
    settled_date: str
    gross_minor: int
    fee_minor: int
    currency: str
    row_type: str          # settlement | partial | chargeback
    file_id: str
    line_no: int


class FileRejected(Exception):
    """Raised before any row is applied. Never downgraded to a warning."""


def expected_fee(amount_minor: int, currency: str) -> int:
    return FEE_FIXED_MINOR + abs(amount_minor) * FEE_SCHEDULE_BPS[currency] // 10_000


# ------------------------------------------------------------------ generate
def generate(n_days: int = 30, per_day: int = 400, seed: int = 31):
    rng = random.Random(seed)
    DATA.mkdir(exist_ok=True)
    start = date(2026, 4, 1)
    internal: list[InternalTxn] = []
    files: list[Path] = []
    settled_refs: list[tuple[str, str, int, str]] = []

    for d in range(n_days):
        bdate = start + timedelta(days=d)
        for i in range(per_day):
            ccy = rng.choices(["USD", "EUR", "GBP"], [0.8, 0.12, 0.08])[0]
            internal.append(InternalTxn(
                "AUTH{:03d}{:05d}".format(d, i), bdate.isoformat(),
                rng.randint(500, 900_000), ccy))

    by_ref = {t.ref: t for t in internal}

    for d in range(n_days):
        bdate = start + timedelta(days=d)
        rows: list[SettlementRow] = []
        file_id = "STL{}".format(bdate.strftime("%Y%m%d"))

        # Settlements for auths from T-1 and T-2.
        for lag in (1, 2):
            src = start + timedelta(days=d - lag)
            if src < start:
                continue
            for t in [x for x in internal if x.auth_date == src.isoformat()]:
                if rng.random() < (0.65 if lag == 1 else 0.30):
                    partial = rng.random() < 0.05
                    gross = t.amount_minor if not partial else int(t.amount_minor * 0.6)
                    fee = expected_fee(gross, t.currency)
                    # 2% of rows carry a fee that does not match the schedule
                    if rng.random() < 0.02:
                        fee += rng.randint(5, 400)
                    rows.append(SettlementRow(
                        t.ref, bdate.isoformat(), gross, fee, t.currency,
                        "partial" if partial else "settlement", file_id, 0))
                    settled_refs.append((t.ref, bdate.isoformat(), gross, t.currency))

        # Chargebacks referencing transactions up to 30 days old.
        for _ in range(rng.randint(0, 4)):
            if not settled_refs:
                break
            ref, sdate, gross, ccy = rng.choice(settled_refs)
            rows.append(SettlementRow(ref, bdate.isoformat(), -gross, 0, ccy,
                                      "chargeback", file_id, 0))

        path = DATA / "{}.txt".format(file_id)
        write_file(path, file_id, bdate.isoformat(), rows,
                   malformed=(rng.random() < 0.15))
        files.append(path)

    # Duplicate delivery of one file, byte-identical.
    dup_src = files[len(files) // 2]
    dup = DATA / (dup_src.stem + "_REDELIVERY.txt")
    dup.write_text(dup_src.read_text(encoding="utf-8"), encoding="utf-8")
    files.append(dup)

    return internal, files, dup_src.stem


def write_file(path: Path, file_id: str, bdate: str, rows: list[SettlementRow],
               malformed: bool = False) -> None:
    count = len(rows)
    gross_total = sum(r.gross_minor for r in rows)
    fee_total = sum(r.fee_minor for r in rows)
    lines = ["H{:<12}{:<8}{:<10}".format(file_id, bdate.replace("-", ""), "PROCESSOR")]
    for i, r in enumerate(rows):
        lines.append("D{:<16}{:<8}{:>15}{:>12}{:<3}{:<10}".format(
            r.ref, r.settled_date.replace("-", ""), r.gross_minor, r.fee_minor,
            r.currency, r.row_type))
    if malformed and rows:
        # A junk line. It must be quarantined, and because it is unparseable the
        # trailer will no longer tie -- so the whole file is rejected. That is
        # the intended behaviour, not a bug in the parser.
        lines.insert(len(lines) // 2, "D{:<16}{:<8}{:>15}".format("BADLINE", "XXXXXXXX", "NOTANUMBER"))
    lines.append("T{:>10}{:>20}{:>15}".format(count, gross_total, fee_total))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- parse
def parse(path: Path) -> tuple[str, list[SettlementRow]]:
    rows: list[SettlementRow] = []
    rejected: list[tuple[int, str]] = []
    file_id = None
    declared = None

    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        code = raw[0]
        if code == "H":
            file_id = raw[1:13].strip()
        elif code == "T":
            declared = (int(raw[1:11]), int(raw[11:31]), int(raw[31:46]))
        elif code == "D":
            try:
                ref = raw[1:17].strip()
                sdate = raw[17:25].strip()
                gross = int(raw[25:40].strip())
                fee = int(raw[40:52].strip())
                ccy = raw[52:55].strip()
                rtype = raw[55:65].strip()
                iso = "{}-{}-{}".format(sdate[:4], sdate[4:6], sdate[6:8])
                rows.append(SettlementRow(ref, iso, gross, fee, ccy, rtype,
                                          file_id or path.stem, line_no))
            except Exception as exc:
                rejected.append((line_no, str(exc)))
        else:
            rejected.append((line_no, "unknown record code " + repr(code)))

    if declared is None:
        raise FileRejected("{}: no trailer record. Nothing processed.".format(path.name))

    parsed = (len(rows), sum(r.gross_minor for r in rows), sum(r.fee_minor for r in rows))
    if parsed != declared or rejected:
        raise FileRejected(
            "{}: CONTROL TOTAL MISMATCH -- declared (count={}, gross={}, fee={}), "
            "parsed (count={}, gross={}, fee={}), {} malformed line(s). "
            "File rejected in full; nothing processed.".format(
                path.name, *declared, *parsed, len(rejected)))
    return file_id or path.stem, rows
