"""Channel selection: turn what a state *actually reports* into a record's channel set.

Replaces the hand-written `_corn_channels()` / `_wheat_channels()` pairs. Those hard-coded one
commodity per state and, as a side effect, made nine states unbuildable ("they need a new
crop-stage channel set, not just a config entry") even though every one of them reports a large
weekly series -- Arizona upland cotton for 1,660 weeks, Florida peanuts for 966, Nevada pasture
for 944, and the six New England states apples/potatoes/sweet-corn/pasture for ~870. Selecting
channels from the series index instead means a state needs no per-commodity code at all, and a
state that narrates several crops contributes all of them as channels of one record (which is the
package's existing rule: multi-commodity enriches channels, it does not multiply record count).

Two groups per record:

  universal backbone -- crop-agnostic, surveyed every week of the season by nearly every state:
      FIELDWORK days-suitable, TOPSOIL/SUBSOIL moisture (8), PASTURELAND condition (5).
      PASTURELAND is the single largest series family in the whole file (215,813 STATE/WEEKLY
      observations, present for all 48 states) and the previous build used none of it.
  commodity channels -- every commodity the state reports with enough weekly history, each
      contributing its PROGRESS growth stages plus its 5-way CONDITION rating.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------- unit labels

_COMMODITY_ALIASES = {
    "WHEAT, WINTER": "winter_wheat",
    "WHEAT, SPRING, (EXCL DURUM)": "spring_wheat",
    "WHEAT, SPRING, DURUM": "durum_wheat",
    "COTTON, UPLAND": "upland_cotton",
    "CORN, GRAIN": "corn_grain",
    "CORN, SILAGE": "corn_silage",
    "SORGHUM, GRAIN": "sorghum_grain",
    "SORGHUM, SILAGE": "sorghum_silage",
    "HAY, ALFALFA": "alfalfa_hay",
    "HAY, (EXCL ALFALFA)": "other_hay",
    "HAY, HARVESTED QUALITY": "hay_quality",
    "BEANS, DRY EDIBLE, (EXCL CHICKPEAS)": "dry_beans",
    "BEANS, DRY EDIBLE, INCL CHICKPEAS": "dry_beans_incl_chickpeas",
    "PEAS, DRY EDIBLE": "dry_peas",
    "BLUEBERRIES, TAME": "blueberries",
    "ONIONS, DRY": "dry_onions",
    "VEGETABLE TOTALS, IN THE OPEN": "vegetables_open",
    "SOIL, TOPSOIL": "topsoil",
    "SOIL, SUBSOIL": "subsoil",
}


def _slug(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def unit_label(short_desc: str) -> str | None:
    """`SHORT_DESC` -> snake_case channel label, or None if it isn't a channel we model.

    Examples:
      CORN - PROGRESS, MEASURED IN PCT PLANTED          -> corn_pct_planted
      WHEAT, WINTER - CONDITION, MEASURED IN PCT GOOD   -> winter_wheat_condition_pct_good
      SOIL, TOPSOIL - MOISTURE, MEASURED IN PCT ADEQUATE-> topsoil_moisture_pct_adequate
      FIELDWORK - DAYS SUITABLE, MEASURED IN DAYS / WEEK-> days_suitable_per_week
    """
    if " - " not in short_desc:
        return None
    commodity, rest = short_desc.split(" - ", 1)
    if short_desc == "FIELDWORK - DAYS SUITABLE, MEASURED IN DAYS / WEEK":
        return "days_suitable_per_week"
    m = re.match(r"(PROGRESS|CONDITION|MOISTURE),\s*MEASURED IN PCT (.+)$", rest)
    if not m:
        return None
    kind, cls = m.group(1), m.group(2)
    base = _COMMODITY_ALIASES.get(commodity, _slug(commodity))
    cls_slug = _slug(cls)
    if kind == "PROGRESS":
        return f"{base}_pct_{cls_slug}"
    if kind == "CONDITION":
        return f"{base}_condition_pct_{cls_slug}"
    return f"{base}_moisture_pct_{cls_slug}"


# ---------------------------------------------------------------- selection

DAYS_SUITABLE = "FIELDWORK - DAYS SUITABLE, MEASURED IN DAYS / WEEK"
_MOISTURE_CLASSES = ("VERY SHORT", "SHORT", "ADEQUATE", "SURPLUS")
_CONDITION_CLASSES = ("VERY POOR", "POOR", "FAIR", "GOOD", "EXCELLENT")

UNIVERSAL: list[str] = (
    [DAYS_SUITABLE]
    + [f"SOIL, {layer} - MOISTURE, MEASURED IN PCT {c}"
       for layer in ("TOPSOIL", "SUBSOIL") for c in _MOISTURE_CLASSES]
    + [f"PASTURELAND - CONDITION, MEASURED IN PCT {c}" for c in _CONDITION_CLASSES]
)

# Commodities excluded from the *stage* selection: aggregates and date-valued oddities that
# aren't a weekly percentage trajectory of a real crop.
_SKIP_COMMODITIES = {"PASTURELAND"}


@dataclass(frozen=True)
class Channel:
    short_desc: str
    unit: str


def select_channels(state_index: dict[str, dict], *, min_weeks: int = 150,
                    max_commodities: int = 8) -> tuple[list[Channel], list[str]]:
    """Pick channels for one state.

    `state_index` maps SHORT_DESC -> {date: value} for that state only.

    Returns `(channels, commodities)`. The universal backbone comes first (so a record's leading
    channels are always the dense ones), then commodities ordered by how much weekly history they
    have. `min_weeks` keeps out crops a state reports only a handful of times -- those add a
    near-empty channel to every record of that state without adding real signal.
    """
    chans: list[Channel] = []
    for sd in UNIVERSAL:
        if sd in state_index:
            u = unit_label(sd)
            if u:
                chans.append(Channel(sd, u))

    # commodity -> weeks of PROGRESS/CONDITION history
    weeks: dict[str, set] = {}
    for sd, per_date in state_index.items():
        if " - " not in sd:
            continue
        commodity, rest = sd.split(" - ", 1)
        if commodity in _SKIP_COMMODITIES or commodity.startswith("SOIL,"):
            continue
        if not rest.startswith(("PROGRESS,", "CONDITION,")):
            continue
        if unit_label(sd) is None:
            continue
        weeks.setdefault(commodity, set()).update(per_date)

    ranked = sorted(((c, w) for c, w in weeks.items() if len(w) >= min_weeks),
                    key=lambda cw: -len(cw[1]))[:max_commodities]
    commodities = [c for c, _ in ranked]

    for commodity in commodities:
        # PROGRESS stages, ordered by when the stage actually peaks in the season so the channel
        # list reads as the crop's real cascade (planted -> emerged -> ... -> harvested) rather
        # than alphabetically.
        prog = [sd for sd in state_index
                if sd.startswith(f"{commodity} - PROGRESS,") and unit_label(sd)]
        prog.sort(key=lambda sd: _median_doy(state_index[sd]))
        for sd in prog:
            chans.append(Channel(sd, unit_label(sd)))  # type: ignore[arg-type]
        for cls in _CONDITION_CLASSES:
            sd = f"{commodity} - CONDITION, MEASURED IN PCT {cls}"
            if sd in state_index:
                chans.append(Channel(sd, unit_label(sd)))  # type: ignore[arg-type]

    # A duplicate unit label inside one record is a strict-validator warning; keep first wins.
    seen, uniq = set(), []
    for c in chans:
        if c.unit in seen:
            continue
        seen.add(c.unit)
        uniq.append(c)
    return uniq, commodities


def _median_doy(per_date: dict) -> float:
    """Median day-of-year weighted toward when the stage is actually being reported."""
    doys = sorted(d.timetuple().tm_yday for d in per_date)
    return doys[len(doys) // 2] if doys else 999.0
