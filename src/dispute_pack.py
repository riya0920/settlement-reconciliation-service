"""The monthly fee-variance dispute pack, with a materiality threshold.

`fee_variance_summary()` flags every transaction whose deducted fee differs from
the schedule. On a month of real volume that is thousands of rows, and a list of
thousands of rows is not a dispute -- it is a spreadsheet nobody sends.

WHAT TURNS A VARIANCE LIST INTO A DISPUTE.

  AGGREGATION      the processor does not care that transaction 4471 was 3 cents
                   light. It cares that one fee tier has been mis-applied 8,000
                   times for a month, which is one argument with one root cause
                   and one number attached.

  MATERIALITY      a threshold, stated, with the reasoning next to it. Below it
                   the variance costs less to absorb than to argue about --
                   which is a real commercial judgement and not a rounding-down
                   of the firm's own claim. The threshold belongs to finance,
                   so it is a parameter.

  A ROOT CAUSE     "fees are wrong" is not actionable. "Every EUR transaction
                   between the 3rd and the 11th was charged the GBP rate" is,
                   because it names what to fix and bounds what to re-bill.

  THE OTHER SIDE   variances in OUR favour go in the same pack. A dispute pack
                   that reports only the amounts owed to us is a negotiating
                   position dressed as a reconciliation, and the first thing the
                   processor will do is find the ones we kept quiet about.

THE ONE THING THIS CANNOT DO. Decide whether the schedule itself is right. Every
variance here is measured against a fee schedule this repository declares; if
the contract says something different, the whole pack is measuring the wrong
thing accurately.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

# Below this, the cost of arguing exceeds the amount. Finance owns this number,
# not engineering -- it is here as a default and a parameter, never a constant
# buried in a comparison.
DEFAULT_MATERIALITY_MINOR = 5_000        # $50


@dataclass
class VarianceGroup:
    root_cause: str
    currency: str
    count: int
    total_minor: int
    direction: str                        # owed_to_us | owed_to_them
    examples: list = field(default_factory=list)

    @property
    def material(self) -> bool:
        return abs(self.total_minor) >= DEFAULT_MATERIALITY_MINOR


def group_variances(rows, materiality_minor: int = DEFAULT_MATERIALITY_MINOR
                    ) -> list[VarianceGroup]:
    """Collapse per-transaction variances into arguable groups.

    Grouping key is (currency, sign, rounded rate delta). The rate delta is what
    identifies a MIS-APPLIED TIER -- a hundred transactions all short by the
    same basis points is one mistake, while a hundred short by different amounts
    is a hundred mistakes and probably not a tier problem at all.
    """
    buckets = defaultdict(list)
    for r in rows:
        delta = int(r.get("variance_minor", 0))
        if delta == 0:
            continue
        gross = max(int(r.get("gross_minor", 0)), 1)
        # Basis points of the gross, rounded to the nearest 5bp. Exact equality
        # would split one tier error into dozens of groups on rounding alone.
        bps = round(delta / gross * 10_000 / 5) * 5
        key = (r.get("currency", "USD"), "owed_to_us" if delta < 0 else "owed_to_them", bps)
        buckets[key].append(r)

    groups = []
    for (ccy, direction, bps), items in buckets.items():
        total = sum(int(i.get("variance_minor", 0)) for i in items)
        groups.append(VarianceGroup(
            root_cause="fee off by ~{}bp of gross".format(bps),
            currency=ccy, count=len(items), total_minor=total,
            direction=direction,
            examples=[i.get("ref") for i in items[:3]]))
    return sorted(groups, key=lambda g: -abs(g.total_minor))


def build_pack(rows, period: str,
               materiality_minor: int = DEFAULT_MATERIALITY_MINOR) -> dict:
    groups = group_variances(rows, materiality_minor)
    material = [g for g in groups if abs(g.total_minor) >= materiality_minor]
    immaterial = [g for g in groups if abs(g.total_minor) < materiality_minor]

    owed_to_us = sum(-g.total_minor for g in material if g.direction == "owed_to_us")
    owed_to_them = sum(g.total_minor for g in material
                       if g.direction == "owed_to_them")

    return {
        "period": period,
        "materiality_minor": materiality_minor,
        "groups_total": len(groups),
        "groups_material": len(material),
        "groups_immaterial": len(immaterial),
        "material": material,
        "immaterial": immaterial,
        "variances_total": sum(g.count for g in groups),
        "owed_to_us_minor": owed_to_us,
        "owed_to_them_minor": owed_to_them,
        "net_claim_minor": owed_to_us - owed_to_them,
        "immaterial_absorbed_minor": sum(abs(g.total_minor) for g in immaterial),
    }


def render(pack: dict) -> str:
    L = ["FEE VARIANCE DISPUTE PACK -- {}".format(pack["period"]),
         "=" * 78,
         "materiality threshold : {:,} minor units".format(pack["materiality_minor"]),
         "variances in period   : {:,}".format(pack["variances_total"]),
         "grouped into          : {} root causes ({} material, {} below threshold)"
         .format(pack["groups_total"], pack["groups_material"],
                 pack["groups_immaterial"]),
         ""]

    if not pack["material"]:
        L.append("No group clears the materiality threshold. That IS the finding:")
        L.append("the variances are real and individually too small to argue,")
        L.append("so the pack recommends absorbing {:,} rather than opening a"
                 .format(pack["immaterial_absorbed_minor"]))
        L.append("dispute that costs more than it recovers.")
        return "\n".join(L)

    L.append("{:<34}{:>10}{:>16}{:>16}".format(
        "root cause", "count", "amount", "direction"))
    L.append("-" * 78)
    for g in pack["material"]:
        L.append("{:<34}{:>10,}{:>16,}{:>16}".format(
            g.root_cause[:33], g.count, g.total_minor, g.direction))
        L.append("   e.g. {}".format(", ".join(str(e) for e in g.examples)))
    L.append("-" * 78)
    L.append("{:<34}{:>42,}".format("owed to us", pack["owed_to_us_minor"]))
    L.append("{:<34}{:>42,}".format("owed to them", pack["owed_to_them_minor"]))
    L.append("{:<34}{:>42,}".format("NET CLAIM", pack["net_claim_minor"]))
    L.append("")
    L.append("Variances in the processor's favour are in this pack too. A pack")
    L.append("that reports only what we are owed is a negotiating position")
    L.append("dressed as a reconciliation, and the first thing the other side")
    L.append("does is find the ones we left out.")
    L.append("")
    L.append("Below the threshold: {} groups totalling {:,}, recommended for"
             .format(pack["groups_immaterial"], pack["immaterial_absorbed_minor"]))
    L.append("absorption -- arguing them costs more than they are worth.")
    return "\n".join(L)
