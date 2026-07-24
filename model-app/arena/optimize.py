#!/usr/bin/env python3
"""Arena — opening-squad optimizer for the deterministic Prediction-Model agent.

Picks a legal £100m FPL squad that maximizes the starting XI's projected points, then
selects the XI (valid formation), captain and bench order. This is the *chalk* agent —
pure expected value, no strategy. The Opus AI agent will reason on top of the same pool.

ILP (scipy.optimize.milp): binary x_i (in 15) and y_i (in XI) per player.
  maximize  sum(xph_i * y_i)                       # the starting XI's 5-GW projected points
  s.t.  sum x = 15 ; GK/DEF/MID/FWD squad = 2/5/5/3
        sum(price_i x_i) <= budget ; <=3 per club
        sum y = 11 ; y_i <= x_i
        XI formation: GK=1, DEF in [3,5], MID in [2,5], FWD in [1,3]

Usage: python optimize.py [players.json] [budget]   (defaults: ./players.json, 100.0)
"""
from __future__ import annotations
import json, sys
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds

POS = ["GK", "DEF", "MID", "FWD"]
SQUAD = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}


def xph_of(p):
    v = p.get("xph")
    return float(v) if v is not None else float(p.get("ep", 0) or 0)


def optimize_squad(players, budget=100.0, obj_key="xph", force_in=()):
    # usable pool: has a price + position, and isn't hard-unavailable ('u')
    pool = [p for p in players if p.get("price") and p.get("pos") in POS and p.get("st") != "u"]
    n = len(pool)
    price = np.array([float(p["price"]) for p in pool])
    val = np.array([xph_of(p) if obj_key == "xph" else float(p.get("xp1", 0) or 0) for p in pool])
    posmask = {k: np.array([1.0 if p["pos"] == k else 0.0 for p in pool]) for k in POS}
    clubs = sorted({p["code"] for p in pool})

    # variables: [x_0..x_{n-1}, y_0..y_{n-1}]
    N = 2 * n
    c = np.concatenate([np.zeros(n), -val])              # minimize -> maximize XI value
    cons = []
    def row(xpart, ypart): return np.concatenate([xpart, ypart])
    # squad size + per-position squad counts
    cons.append(LinearConstraint(row(np.ones(n), np.zeros(n)), 15, 15))
    for k in POS:
        cons.append(LinearConstraint(row(posmask[k], np.zeros(n)), SQUAD[k], SQUAD[k]))
    # budget
    cons.append(LinearConstraint(row(price, np.zeros(n)), -np.inf, budget))
    # <=3 per club
    for cl in clubs:
        m = np.array([1.0 if p["code"] == cl else 0.0 for p in pool])
        cons.append(LinearConstraint(row(m, np.zeros(n)), -np.inf, 3))
    # XI size + formation
    cons.append(LinearConstraint(row(np.zeros(n), np.ones(n)), 11, 11))
    for k in POS:
        cons.append(LinearConstraint(row(np.zeros(n), posmask[k]), XI_MIN[k], XI_MAX[k]))
    # y_i <= x_i  ->  y_i - x_i <= 0 (one row per player)
    A = np.zeros((n, N))
    for i in range(n):
        A[i, i] = -1.0      # -x_i
        A[i, n + i] = 1.0   # +y_i
    cons.append(LinearConstraint(A, -np.inf, 0))
    # strategic must-owns: force x_i = 1 for chosen player ids
    fids = set(force_in or [])
    for i, p in enumerate(pool):
        if p["id"] in fids:
            r = np.zeros(N); r[i] = 1.0
            cons.append(LinearConstraint(r, 1, 1))

    res = milp(c, constraints=cons, integrality=np.ones(N), bounds=Bounds(0, 1))
    if not res.success:
        raise RuntimeError(f"ILP failed: {res.message}")
    x = res.x[:n] > 0.5
    y = res.x[n:] > 0.5
    squad = [pool[i] for i in range(n) if x[i]]
    xi = [pool[i] for i in range(n) if y[i]]
    bench = [p for p in squad if p not in xi]
    # captain = highest single-GW projection in the XI; vice = next
    xi_sorted = sorted(xi, key=lambda p: -(float(p.get("xp1", 0) or 0)))
    captain, vice = xi_sorted[0], xi_sorted[1]
    # bench order: GK first, then by xp1 descending
    bench_sorted = sorted(bench, key=lambda p: (0 if p["pos"] == "GK" else 1, -(float(p.get("xp1", 0) or 0))))
    form = "-".join(str(sum(1 for p in xi if p["pos"] == k)) for k in ["DEF", "MID", "FWD"])
    return {"squad": squad, "xi": xi, "bench": bench_sorted, "captain": captain, "vice": vice,
            "formation": form, "spend": round(float(price[x].sum()), 1),
            "xi_xph": round(sum(xph_of(p) for p in xi), 1)}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "players.json"
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0
    players = json.load(open(path))["players"]
    r = optimize_squad(players, budget)
    print(f"Formation {r['formation']} | spend £{r['spend']}m | XI 5-GW xPH {r['xi_xph']}\n")
    print("STARTING XI:")
    for k in POS:
        for p in [q for q in r["xi"] if q["pos"] == k]:
            tag = " (C)" if p is r["captain"] else " (V)" if p is r["vice"] else ""
            print(f"  {p['pos']} {p['name']:<16}{p['code']}  £{p['price']}  xph {xph_of(p):.1f}{tag}")
    print("BENCH:")
    for p in r["bench"]:
        print(f"  {p['pos']} {p['name']:<16}{p['code']}  £{p['price']}")
    return r


if __name__ == "__main__":
    main()
