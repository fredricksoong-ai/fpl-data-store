#!/usr/bin/env python3
"""Arena — captain / vice selection.

Position-agnostic by design: the captain is simply the highest single-GW projection (xp1) in the XI,
whatever position. A strong attacking full-back or a set-piece centre-back with clean-sheet upside is
a legitimate captain contender — we do NOT restrict the armband to forwards/midfielders.

The one guard is against a known model failure mode, not against defenders as a class: an early-season
or thin-data fit can over-project defender clean-sheet points, throwing out an implausible single-GW
number (e.g. a defender at 7.8). No defender realistically projects above ~7 for one gameweek, so if the
top pick is a GK/DEF whose xp1 clears that ceiling, we treat it as an artifact and fall back to the best
outfield attacker — but a defender projecting *within* a plausible range keeps the armband on merit.
"""
from __future__ import annotations

DEF_CAPTAIN_CEIL = 7.0   # a single-GW GK/DEF projection above this is almost always a CS-inflation artifact


def _xp1(p):
    return float(p.get("xp1") or 0)


def pick_captain(xi):
    """Return (captain, vice) from an XI. Position-agnostic, with an anomaly guard on GK/DEF."""
    ranked = sorted(xi, key=lambda p: -_xp1(p))
    if not ranked:
        return None, None
    cap = ranked[0]
    if cap.get("pos") in ("GK", "DEF") and _xp1(cap) > DEF_CAPTAIN_CEIL:
        attacker = next((p for p in ranked if p.get("pos") in ("MID", "FWD")), None)
        if attacker is not None:
            cap = attacker
    vice = next((p for p in ranked if p["id"] != cap["id"]), ranked[-1])
    return cap, vice
