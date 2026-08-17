#!/usr/bin/env python3
"""Arena — in-season transfer optimizer for the deterministic Prediction-Model agent.

optimize.py builds the OPENING squad; this decides the best legal MOVE from an existing squad
each subsequent gameweek. It is the *chalk* agent: pure expected value, no strategy. The Opus AI
reasons on top of the same machinery.

Formulation (scipy.optimize.milp), reusing the opening-squad constraints but pinned near the
current squad. Binary x_i (in the new 15) and y_i (in XI) over a pool that always contains the
current squad plus buy candidates:
  maximize  sum(xph_i * y_i)  -  4 * max(0, t - free_transfers)
  s.t.  the usual FPL squad/XI/club constraints (see optimize.py), AND
        money:  sum(cost_i * x_i) <= bank + sum(sell_i for owned)   # owned priced at SELL value
        moves:  sum(x_i for owned) = 15 - t                          # exactly t sold

The hit term is piecewise, so we solve once per t in 0..MAX_TRANSFERS and keep the best net.

FPL selling price: sell = purchase + floor((now - purchase)/2) on profit, else now (computed in
tenths so the floor is exact). Budget post-GW1 is bank + squad selling value, never a flat £100m.

Usage: python transfer.py [players.json] [state.json] [max_transfers]
"""
from __future__ import annotations
import json, sys
import numpy as np
from scipy.optimize import milp, LinearConstraint, Bounds

POS = ["GK", "DEF", "MID", "FWD"]
SQUAD = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}
HIT = 4.0
MAX_TRANSFERS = 2


def xph_of(p):
    v = p.get("xph")
    return float(v) if v is not None else float(p.get("ep", 0) or 0)


def xp1_of(p):
    return float(p.get("xp1", 0) or 0)


def sell_price(purchase, now):
    """FPL selling price in £m: keep purchase + floor(half the rise); losses sell at current."""
    pp, nw = round(purchase * 10), round(now * 10)
    rise = nw - pp
    tenths = pp + rise // 2 if rise > 0 else nw
    return tenths / 10.0


def _solve_for_t(pool, owned_mask, cost, val, price_now, budget, t):
    """Solve the ILP with exactly t of the owned players sold. Returns (xi_xph, x, y) or None."""
    n = len(pool)
    N = 2 * n
    posmask = {k: np.array([1.0 if p["pos"] == k else 0.0 for p in pool]) for k in POS}
    clubs = sorted({p["code"] for p in pool})
    c = np.concatenate([np.zeros(n), -val])
    def row(xp, yp): return np.concatenate([xp, yp])
    cons = [LinearConstraint(row(np.ones(n), np.zeros(n)), 15, 15)]
    for k in POS:
        cons.append(LinearConstraint(row(posmask[k], np.zeros(n)), SQUAD[k], SQUAD[k]))
    cons.append(LinearConstraint(row(cost, np.zeros(n)), -np.inf, budget))          # money
    for cl in clubs:
        m = np.array([1.0 if p["code"] == cl else 0.0 for p in pool])
        cons.append(LinearConstraint(row(m, np.zeros(n)), -np.inf, 3))
    cons.append(LinearConstraint(row(np.zeros(n), np.ones(n)), 11, 11))
    for k in POS:
        cons.append(LinearConstraint(row(np.zeros(n), posmask[k]), XI_MIN[k], XI_MAX[k]))
    A = np.zeros((n, N))
    for i in range(n):
        A[i, i] = -1.0; A[i, n + i] = 1.0
    cons.append(LinearConstraint(A, -np.inf, 0))                                     # y_i <= x_i
    cons.append(LinearConstraint(row(owned_mask, np.zeros(n)), 15 - t, 15 - t))      # exactly t sold
    res = milp(c, constraints=cons, integrality=np.ones(N), bounds=Bounds(0, 1))
    if not res.success:
        return None
    x = res.x[:n] > 0.5; y = res.x[n:] > 0.5
    return float(sum(val[i] for i in range(n) if y[i])), x, y


def optimize_transfers(players, state, max_transfers=MAX_TRANSFERS):
    owned = {s["id"]: s["purchase_price"] for s in state["squad"]}
    bank = float(state.get("bank", 0.0))
    free = int(state.get("free_transfers", 1))
    byid = {p["id"]: p for p in players}

    # pool = current squad (always, so they can be kept or sold) + available buy candidates
    pool = []
    for pid, pp in owned.items():
        p = byid.get(pid)
        if p:
            pool.append(p)
    seen = set(owned)
    for p in players:
        if p["id"] in seen:
            continue
        if p.get("price") and p.get("pos") in POS and p.get("st") not in ("u", "i", "s"):
            pool.append(p)
    owned_mask = np.array([1.0 if p["id"] in owned else 0.0 for p in pool])
    # cost vector: owned valued at SELL price, others at current price; budget = bank + total sell value
    sell = {pid: sell_price(pp, byid[pid]["price"]) for pid, pp in owned.items() if pid in byid}
    cost = np.array([sell[p["id"]] if p["id"] in owned else float(p["price"]) for p in pool])
    val = np.array([xph_of(p) for p in pool])
    price_now = np.array([float(p["price"]) for p in pool])
    budget = bank + sum(sell.values())

    best = None
    for t in range(0, max_transfers + 1):
        sol = _solve_for_t(pool, owned_mask, cost, val, price_now, budget, t)
        if sol is None:
            continue
        xi_xph, x, y = sol
        net = xi_xph - HIT * max(0, t - free)
        if best is None or net > best["net"] + 1e-9:
            best = {"t": t, "net": round(net, 2), "xi_xph": round(xi_xph, 2), "x": x, "y": y}

    x, y = best["x"], best["y"]
    new_squad = [pool[i] for i in range(len(pool)) if x[i]]
    xi = [pool[i] for i in range(len(pool)) if y[i]]
    bench = [p for p in new_squad if p not in xi]
    sold = [byid[pid] for pid in owned if pid not in {p["id"] for p in new_squad}]
    bought = [p for p in new_squad if p["id"] not in owned]
    xi_sorted = sorted(xi, key=lambda p: -xp1_of(p))
    captain, vice = xi_sorted[0], xi_sorted[1]
    bench_sorted = sorted(bench, key=lambda p: (0 if p["pos"] == "GK" else 1, -xp1_of(p)))
    spend_new = sum(cost[i] for i in range(len(pool)) if x[i])
    form = "-".join(str(sum(1 for p in xi if p["pos"] == k)) for k in ["DEF", "MID", "FWD"])
    return {
        "transfers": best["t"], "hit": HIT * max(0, best["t"] - free), "net_xph": best["net"],
        "xi_xph": best["xi_xph"], "bank_after": round(budget - spend_new, 1),
        "squad": new_squad, "xi": xi, "bench": bench_sorted, "captain": captain, "vice": vice,
        "formation": form, "sold": sold, "bought": bought,
    }


def main():
    players = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "players.json"))["players"]
    state = json.load(open(sys.argv[2] if len(sys.argv) > 2 else "state.json"))
    mt = int(sys.argv[3]) if len(sys.argv) > 3 else MAX_TRANSFERS
    r = optimize_transfers(players, state, mt)
    print(f"{r['transfers']} transfer(s), hit -{int(r['hit'])} | net horizon xPH {r['net_xph']} "
          f"(XI {r['xi_xph']}) | bank after £{r['bank_after']}m | {r['formation']}")
    if r["sold"]:
        print("OUT:", ", ".join(f"{p['name']} ({p['code']})" for p in r["sold"]))
        print(" IN:", ", ".join(f"{p['name']} ({p['code']})" for p in r["bought"]))
    else:
        print("No transfer — roll it.")
    print(f"(C) {r['captain']['name']}  (V) {r['vice']['name']}")
    return r


if __name__ == "__main__":
    main()
