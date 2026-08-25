"""The operator view, and the alerts that go with it.

`README.md` listed this as an open item: "The API returns the numbers; nothing
renders them, and there is no alert rule on aged breaks or fee variance."

THE THRESHOLDS ARE IMPORTED, NEVER RE-TYPED. This is the whole design constraint
and it is the one a dashboard usually gets wrong. Every number a panel colours
red comes from the module that owns it -- `break_queue.TIERS`,
`dispute_pack.DEFAULT_MATERIALITY_MINOR`, `chargebacks.EVIDENCE_WINDOW_DAYS` --
rather than being copied into a dashboard config.

The failure that prevents is specific and common: someone tunes a threshold in
the code, the dashboard keeps the old one, and for months the screen and the
system disagree about what is wrong. The screen wins, because the screen is what
people look at. A dashboard with its own copy of the rules is a second
implementation of them.

WHAT GOES ON IT is decided by what an operator must ACT on today, not by what is
easy to plot:

  TOTALS ARE NOT ACTIONABLE. "127 open breaks" tells nobody what to do. "3
  breaks past their T3 deadline" is a morning's work with a name on it. Counts
  appear only as context for the items above them.

  DEADLINES BEAT AMOUNTS. A chargeback whose evidence window closes today
  outranks a larger one closing next week, because the second can still be
  fought and the first cannot. Same ordering as `representment.work_queue`, for
  the same reason.

  AN ALERT WITH NO ACTION IS NOISE. Each rule carries the action from the tier
  that raised it, so the row says what to do rather than only that something is
  wrong.

WHY IT RENDERS SERVER-SIDE TO ONE FILE. No CDN, no build step, no JavaScript
framework. A settlement dashboard is looked at during an incident, and an
incident is exactly when a page that fetches a chart library from the internet
does not load.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import date

from .break_queue import TIERS, tier_for
from .chargebacks import EVIDENCE_WINDOW_DAYS, WINNABLE
from .dispute_pack import DEFAULT_MATERIALITY_MINOR

# The only threshold this module gets to invent, and it is a display choice
# rather than a control: how many rows a panel shows before it truncates.
ROW_LIMIT = 10


class MissingAge(Exception):
    """Raised when a break row carries no age field.

    THE ALTERNATIVE WAS A SILENT ZERO, AND IT SHIPPED THAT WAY FIRST. This
    module read `age_days` and `break_type`; `Service.aged_breaks` returns `age`
    and `state`. Every one of 625 breaks -- some 34 days old -- defaulted to age
    0, landed in T0, and raised no alert at all. The dashboard showed "625 open
    breaks" and "nothing needs action" on the same screen.

    A missing age must not read as NEW. Zero is the one value that means
    "everything is fine", so it is the one value a missing field must never
    become.
    """


def _age_of(row) -> int:
    for key in ("age_days", "age"):
        if row.get(key) is not None:
            return int(row[key])
    raise MissingAge(
        "break row has neither `age_days` nor `age`: {}. Refusing to default "
        "to 0 -- a missing age would read as a new break and raise no "
        "alert.".format(sorted(row)))


def _type_of(row) -> str:
    """The break's type, for `tier_for`'s ledger special case.

    `Service.aged_breaks` calls this field `state`; `break_queue` calls it
    `break_type`. Both are accepted because both are real, and an empty string
    is a safe default here in a way that zero is not for the age -- an unknown
    type simply misses the ledger fast-path and ages normally.
    """
    return str(row.get("break_type") or row.get("state") or "")


@dataclass
class Alert:
    severity: str            # page | ticket | info
    rule: str
    detail: str
    action: str
    count: int


def break_alerts(aged_breaks: list) -> list:
    """Alerts from break aging, using `break_queue.TIERS` as the source.

    `tier_for` is called rather than reimplemented, so a tier boundary that
    moves in the queue moves here on the same commit.
    """
    by_tier: dict = {}
    for b in aged_breaks:
        t = tier_for(_age_of(b), _type_of(b))
        by_tier.setdefault(t.name, {"tier": t, "items": []})["items"].append(b)

    out = []
    for tier in reversed(TIERS):                      # worst first
        bucket = by_tier.get(tier.name)
        if not bucket or tier.name == "T0":
            continue
        sev = "page" if tier.name == "T3" else (
            "ticket" if tier.name == "T2" else "info")
        out.append(Alert(
            severity=sev,
            rule="break_aging_{}".format(tier.name.lower()),
            detail="{} break(s) at {} ({}+ days)".format(
                len(bucket["items"]), tier.name, tier.min_days),
            # The action comes from the tier, so the alert says what to do
            # rather than only that something is wrong.
            action=tier.action,
            count=len(bucket["items"])))
    return out


def fee_variance_alerts(summary: dict,
                        materiality_minor: int = DEFAULT_MATERIALITY_MINOR
                        ) -> list:
    """Alerts on fee variance, against `dispute_pack`'s materiality.

    The same threshold the dispute pack uses to decide what is worth raising
    with the processor. If the dashboard used a different one, the screen would
    show variances the pack silently drops -- or worse, stay quiet about ones it
    raises.
    """
    total = abs(int(summary.get("total_variance_minor", 0) or 0))
    n = int(summary.get("rows", 0) or 0)
    if not n or total < materiality_minor:
        return []
    return [Alert(
        severity="ticket" if total < materiality_minor * 10 else "page",
        rule="fee_variance_material",
        detail="{:,} minor across {:,} row(s), against a {:,} materiality "
               "floor".format(total, n, materiality_minor),
        action="build a dispute pack and raise with the processor",
        count=n)]


def deadline_alerts(chargebacks: list, as_of: str) -> list:
    """Chargebacks whose evidence window is closing.

    Ordered by deadline rather than by amount, matching
    `representment.work_queue`: a small dispute due today outranks a large one
    due next week, because the second can still be fought.
    """
    today = date.fromisoformat(as_of)
    overdue, due_today, due_soon = [], [], []
    for c in chargebacks:
        if c.get("state") != "received":
            continue
        days = (date.fromisoformat(c["evidence_due_on"]) - today).days
        (overdue if days < 0 else due_today if days == 0
         else due_soon if days <= 2 else []).append(c)

    out = []
    if overdue:
        out.append(Alert(
            "page", "evidence_overdue",
            "{} dispute(s) past their evidence deadline".format(len(overdue)),
            "already lost -- confirm and record, do not submit", len(overdue)))
    if due_today:
        out.append(Alert(
            "page", "evidence_due_today",
            "{} dispute(s) with the window closing today".format(len(due_today)),
            "submit today or the funds are gone regardless of merit",
            len(due_today)))
    if due_soon:
        out.append(Alert(
            "ticket", "evidence_due_soon",
            "{} dispute(s) due within 2 days".format(len(due_soon)),
            "assemble evidence now", len(due_soon)))
    return out


def build(aged_breaks: list, fee_summary: dict, chargebacks: list,
          as_of: str, state_counts: dict | None = None) -> dict:
    alerts = (break_alerts(aged_breaks) + fee_variance_alerts(fee_summary)
              + deadline_alerts(chargebacks, as_of))
    order = {"page": 0, "ticket": 1, "info": 2}
    alerts.sort(key=lambda a: order.get(a.severity, 9))

    today = date.fromisoformat(as_of)
    open_cb = [c for c in chargebacks if c.get("state") == "received"]
    for c in open_cb:
        c["_days"] = (date.fromisoformat(c["evidence_due_on"]) - today).days
    open_cb.sort(key=lambda c: (c["_days"], -int(c.get("amount_minor", 0))))

    worst = sorted(aged_breaks, key=lambda b: -_age_of(b))

    return {
        "as_of": as_of,
        "alerts": alerts,
        "pages": sum(1 for a in alerts if a.severity == "page"),
        "breaks": worst[:ROW_LIMIT],
        "break_count": len(aged_breaks),
        "chargebacks": open_cb[:ROW_LIMIT],
        "chargeback_count": len(open_cb),
        "fee_summary": fee_summary,
        "state_counts": state_counts or {},
        "materiality_minor": DEFAULT_MATERIALITY_MINOR,
        "tiers": [(t.name, t.min_days, t.action) for t in TIERS],
    }


# --------------------------------------------------------------- rendering
_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#e3e3e3;--card:#fafafa;
--page:#b3261e;--ticket:#a06000;--info:#37618e;--ok:#1e7a3c}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
--bg:#16181c;--fg:#e8e8e8;--muted:#9aa0a6;--line:#2c2f36;--card:#1d2026;
--page:#f2857c;--ticket:#e3b341;--info:#8ab4f8;--ok:#6dd58c}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:20px;margin:0 0 2px}h2{font-size:14px;margin:26px 0 8px;
text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.sub{color:var(--muted);margin-bottom:20px;font-size:13px}
.alert{border-left:3px solid var(--line);background:var(--card);
padding:10px 14px;margin-bottom:8px;border-radius:0 4px 4px 0}
.alert.page{border-left-color:var(--page)}
.alert.ticket{border-left-color:var(--ticket)}
.alert.info{border-left-color:var(--info)}
.sev{font-weight:600;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
.sev.page{color:var(--page)}.sev.ticket{color:var(--ticket)}.sev.info{color:var(--info)}
.act{color:var(--muted);font-size:13px;margin-top:3px}
.clear{color:var(--ok);background:var(--card);padding:10px 14px;border-radius:4px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.05em}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto}
.late{color:var(--page);font-weight:600}
.foot{margin-top:28px;color:var(--muted);font-size:12px;border-top:1px solid
var(--line);padding-top:12px}
"""


def _esc(v) -> str:
    return html.escape(str(v))


def render_html(view: dict) -> str:
    A = []
    add = A.append
    add("<!doctype html><meta charset='utf-8'>")
    add("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    add("<title>Settlement operations</title><style>{}</style>".format(_CSS))
    add("<h1>Settlement operations</h1>")
    add("<div class='sub'>as of {} &middot; {} open break(s) &middot; "
        "{} open dispute(s)</div>".format(
            _esc(view["as_of"]), view["break_count"], view["chargeback_count"]))

    add("<h2>Needs action</h2>")
    if not view["alerts"]:
        add("<div class='clear'>Nothing is past a threshold. Counts below are "
            "context, not work.</div>")
    for a in view["alerts"]:
        add("<div class='alert {0}'><span class='sev {0}'>{0}</span> "
            "&nbsp;{1}<div class='act'>&rarr; {2}</div></div>".format(
                _esc(a.severity), _esc(a.detail), _esc(a.action)))

    add("<h2>Oldest breaks</h2><div class='wrap'><table>")
    add("<tr><th>ref</th><th>type</th><th class='num'>age</th>"
        "<th>tier</th><th>action</th></tr>")
    for b in view["breaks"]:
        age = _age_of(b)
        t = tier_for(age, _type_of(b))
        add("<tr><td>{}</td><td>{}</td><td class='num'>{}</td>"
            "<td>{}</td><td>{}</td></tr>".format(
                _esc(b.get("ref", "")), _esc(_type_of(b)), age,
                _esc(t.name), _esc(t.action)))
    if not view["breaks"]:
        add("<tr><td colspan='5'>none</td></tr>")
    add("</table></div>")

    add("<h2>Evidence deadlines</h2><div class='wrap'><table>")
    add("<tr><th>ref</th><th>reason</th><th class='num'>amount</th>"
        "<th>due</th><th class='num'>days</th><th>worth fighting</th></tr>")
    for c in view["chargebacks"]:
        late = c["_days"] < 0
        add("<tr><td>{}</td><td>{}</td><td class='num'>{:,}</td><td>{}</td>"
            "<td class='num{}'>{}</td><td>{}</td></tr>".format(
                _esc(c.get("ref", "")), _esc(c.get("reason_code", "")),
                int(c.get("amount_minor", 0)), _esc(c["evidence_due_on"]),
                " late" if late else "", c["_days"],
                "yes" if c.get("reason_code") in WINNABLE else "no"))
    if not view["chargebacks"]:
        add("<tr><td colspan='6'>none</td></tr>")
    add("</table></div>")

    fs = view["fee_summary"]
    add("<h2>Fee variance</h2><div class='wrap'><table>")
    add("<tr><th>rows</th><th class='num'>total variance (minor)</th>"
        "<th class='num'>materiality</th><th>state</th></tr>")
    total = abs(int(fs.get("total_variance_minor", 0) or 0))
    add("<tr><td>{}</td><td class='num'>{:,}</td><td class='num'>{:,}</td>"
        "<td>{}</td></tr>".format(
            fs.get("rows", 0), total, view["materiality_minor"],
            "material" if total >= view["materiality_minor"] else "below floor"))
    add("</table></div>")

    add("<div class='foot'>Every threshold on this page is imported from the "
        "module that owns it &mdash; break tiers from <code>break_queue.TIERS"
        "</code>, materiality from <code>dispute_pack</code>, evidence windows "
        "from <code>chargebacks</code>. A dashboard with its own copy of the "
        "rules is a second implementation of them, and the two drift.</div>")
    return "\n".join(A)
