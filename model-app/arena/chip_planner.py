#!/usr/bin/env python3
"""Arena — deterministic chip planner for the Prediction-Model agent (the AI reasons on top).

The 2026/27 rules give TWO of each chip, split at GW19: {WC, FH, TC, BB} usable in each half.
The load-bearing insight: Double/Blank gameweeks are created by cup + European rescheduling, so
they cluster in the SECOND half. A FIRST-half chip can therefore never wait for a double — it must
be spent on its best single week before GW19, or it expires unused. This module encodes exactly that.

recommend(state, gw, fixtures, bench_xp1, cap_xp1) -> (chip|None, reason)

BB/TC are decided here deterministically. WC/FH are restructuring / blank-navigation chips whose
timing needs squad-shape and full-schedule judgement — left to the AI agent (and a later module),
so this planner never auto-fires them.
"""
from __future__ import annotations
from collections import Counter

HALF_END = {1: 19, 2: 38}
BB_STRONG = 22.0    # bench 1-GW xp1 sum that justifies a Bench Boost in a single (non-double) week
TC_STRONG = 9.0     # captain 1-GW xp1 that justifies a Triple Captain in a single week
DGW_SLACK = 0.8     # in a double gameweek the bar relaxes (bench/captain effectively play twice)


def half_of(gw: int) -> int:
    return 1 if gw <= HALF_END[1] else 2


def gw_multiplicity(fixtures, gw) -> Counter:
    c = Counter()
    for f in fixtures:
        if f.get("event") == gw and not f.get("finished"):
            c[f.get("team_h")] += 1
            c[f.get("team_a")] += 1
    return c


def is_dgw(fixtures, gw) -> bool:
    return any(v >= 2 for v in gw_multiplicity(fixtures, gw).values())


def available(state, chip) -> bool:
    return chip in state.get("chips", {}).get(str(half_of(state.get("gw", 1))), [])


def recommend(state, gw, fixtures, bench_xp1, cap_xp1):
    """Return (chip, reason) for THIS gameweek, or (None, reason) to hold."""
    half = half_of(gw)
    gws_left = HALF_END[half] - gw
    chips = state.get("chips", {}).get(str(half), [])
    dgw = is_dgw(fixtures, gw)

    # 1. Double gameweek (mostly H2): the doubled bench / captain clears a relaxed bar.
    if dgw:
        if "BB" in chips and bench_xp1 >= BB_STRONG * DGW_SLACK:
            return "BB", f"Double gameweek — bench projects {bench_xp1:.0f}; boost it."
        if "TC" in chips and cap_xp1 >= TC_STRONG * DGW_SLACK:
            return "TC", f"Double gameweek — triple the captain ({cap_xp1:.1f})."

    # 2. Strong single week — a first-half chip can't wait for a double that never comes.
    if "BB" in chips and bench_xp1 >= BB_STRONG:
        return "BB", f"Bench projects {bench_xp1:.0f} — strong single week, no doubles this half."
    if "TC" in chips and cap_xp1 >= TC_STRONG:
        return "TC", f"Captain projects {cap_xp1:.1f} — high single-week ceiling."

    # 3. Expiry forcing — never let a half-chip die unused; play the better of BB/TC in the last week.
    if gws_left <= 1:
        if "BB" in chips:
            return "BB", "Half ending — Bench Boost would expire unused; take the best bench available."
        if "TC" in chips:
            return "TC", "Half ending — Triple Captain would expire unused; triple your best captain."

    return None, "Hold — no week clears the bar yet (and H1 chips can't wait for a double)."


if __name__ == "__main__":
    # quick self-test scenarios
    st = {"gw": 1, "chips": {"1": ["WC", "FH", "TC", "BB"], "2": ["WC", "FH", "TC", "BB"]}}
    cases = [
        ("GW1 fodder bench (your case)", 1, [], 9.0, 4.9),
        ("GW1 strong BB bench",          1, [], 25.0, 4.9),
        ("GW10 huge captain",            10, [], 12.0, 10.5),
        ("GW19 unused BB, weak bench",   19, [], 11.0, 5.0),
        ("H2 double gameweek",           26, [{"event": 26, "team_h": 1, "team_a": 2},
                                              {"event": 26, "team_h": 1, "team_a": 3}], 19.0, 6.0),
    ]
    for label, gw, fx, b, c in cases:
        st["gw"] = gw
        chip, why = recommend(st, gw, fx, b, c)
        print(f"{label:<34} -> {chip or 'HOLD':<5} | {why}")
