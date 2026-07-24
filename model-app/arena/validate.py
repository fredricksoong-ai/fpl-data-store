#!/usr/bin/env python3
"""Arena — legality validator. The guardrail that wraps EVERY agent decision (especially
the Opus AI's), so no agent can ever field an illegal team. Returns a list of violations;
empty list == legal.

A decision is: {squad:[15 ids], xi:[11 ids], bench:[4 ids ordered], captain:id, vice:id}.
`pool` is {id: player} where player has pos, price, code. `budget` is the cash available
(100.0 for the opening squad; for later GWs pass squad_value + bank).
"""
from __future__ import annotations

SQUAD = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}


def validate(decision, pool, budget=100.0):
    v = []
    squad = decision.get("squad", [])
    xi = decision.get("xi", [])
    bench = decision.get("bench", [])
    cap, vice = decision.get("captain"), decision.get("vice")

    missing = [i for i in squad if i not in pool]
    if missing:
        return [f"unknown player id(s): {missing}"]           # can't check further

    if len(squad) != 15: v.append(f"squad has {len(squad)}, need 15")
    if len(set(squad)) != len(squad): v.append("duplicate players in squad")

    by_pos = {}
    for i in squad:
        by_pos[pool[i]["pos"]] = by_pos.get(pool[i]["pos"], 0) + 1
    for k, need in SQUAD.items():
        if by_pos.get(k, 0) != need:
            v.append(f"squad {k}: {by_pos.get(k,0)}, need {need}")

    spend = sum(float(pool[i]["price"]) for i in squad)
    if spend > budget + 1e-6:
        v.append(f"over budget: £{spend:.1f}m > £{budget:.1f}m")

    clubs = {}
    for i in squad:
        clubs[pool[i]["code"]] = clubs.get(pool[i]["code"], 0) + 1
    over = {c: n for c, n in clubs.items() if n > 3}
    if over: v.append(f">3 per club: {over}")

    if len(xi) != 11: v.append(f"XI has {len(xi)}, need 11")
    if any(i not in squad for i in xi): v.append("XI contains players not in squad")
    xi_pos = {}
    for i in xi:
        if i in pool: xi_pos[pool[i]["pos"]] = xi_pos.get(pool[i]["pos"], 0) + 1
    for k in SQUAD:
        c = xi_pos.get(k, 0)
        if not (XI_MIN[k] <= c <= XI_MAX[k]):
            v.append(f"XI {k}: {c} outside [{XI_MIN[k]},{XI_MAX[k]}]")

    if bench and sorted(bench + xi) != sorted(squad):
        v.append("bench + XI must equal the 15-man squad")
    if bench and len(bench) != 4:
        v.append(f"bench has {len(bench)}, need 4")

    if cap not in xi: v.append("captain not in XI")
    if vice not in xi: v.append("vice not in XI")
    if cap is not None and cap == vice: v.append("captain and vice are the same")
    return v


if __name__ == "__main__":
    import json, sys
    from optimize import optimize_squad
    players = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "players.json"))["players"]
    pool = {p["id"]: p for p in players}
    r = optimize_squad(players)
    dec = {"squad": [p["id"] for p in r["squad"]], "xi": [p["id"] for p in r["xi"]],
           "bench": [p["id"] for p in r["bench"]], "captain": r["captain"]["id"], "vice": r["vice"]["id"]}
    viol = validate(dec, pool)
    print("VALID ✓" if not viol else "VIOLATIONS: " + "; ".join(viol))
