#!/usr/bin/env python3
"""Build squad.json — the model's optimal £100 squad for the upcoming run.

Solves the real FPL squad problem: pick 15 (2 GK, 5 DEF, 5 MID, 3 FWD) under a £100
budget with <=3 per club, choosing a valid starting XI, to maximise the XI's projected
points over the FDR-aware horizon (players' `xph` from build_recommend — model xPts
summed over the next ~5 GWs, so fixture difficulty is already priced in). The bench is
therefore cheap enabler fodder rather than wasted budget.

Reads players.json (needs `xph`, price, pos, code). Uses scipy's integer solver (milp),
falling back to a greedy + local-search heuristic if unavailable. Emits rows in the same
15-field shape the squad pitch renders.

Output: {generated, event, budget, spend, xi_xph, formation, rows:[[type,name,...,xph,fdr], ...]}
"""
from __future__ import annotations
import json, os, urllib.request, datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("SQUAD_OUT", "squad.json"))
BUDGET = float(os.environ.get("SQUAD_BUDGET", "100.0"))
API = "https://fantasy.premierleague.com/api"
PT = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}
QUOTA = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
XI_MIN = {"GK": 1, "DEF": 3, "MID": 2, "FWD": 1}
XI_MAX = {"GK": 1, "DEF": 5, "MID": 5, "FWD": 3}


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def fixtures_info():
    """Return (next_gw, opp_map_for_next_gw, chip_recommendations).
    Chip timing keys off double/blank gameweeks; until the PL announces them the
    advice is graceful 'Hold' guidance that auto-upgrades to 'Target GWx' later."""
    from collections import Counter
    chips_default = [
        {"chip": "WC", "label": "Wildcard", "advice": "Hold", "note": "Save for the first big fixture swing — burning it early wastes flexibility."},
        {"chip": "FH", "label": "Free Hit", "advice": "Hold", "note": "Best in a blank gameweek — none scheduled yet."},
        {"chip": "BB", "label": "Bench Boost", "advice": "Hold", "note": "Best in a double gameweek — none scheduled yet."},
        {"chip": "TC", "label": "Triple Captain", "advice": "Hold", "note": "Best on a premium in a double gameweek — none scheduled yet."},
    ]
    try:
        boot = get("/bootstrap-static/"); short = {t["id"]: t["short_name"] for t in boot["teams"]}
        nteams = len(boot["teams"])
        sched = [f for f in get("/fixtures/") if f.get("event")]
        un = sorted({f["event"] for f in sched if not f.get("finished")})
        gw = un[0] if un else 0
        opp = {}
        for f in sched:
            if f["event"] == gw:
                opp[short[f["team_h"]]] = short[f["team_a"]]; opp[short[f["team_a"]]] = short[f["team_h"]]
        dgw, bgw = [], []
        for ev in un:
            evfix = [f for f in sched if f["event"] == ev and not f.get("finished")]
            if not evfix:
                continue
            cnt = Counter()
            for f in evfix:
                cnt[f["team_h"]] += 1; cnt[f["team_a"]] += 1
            if any(v >= 2 for v in cnt.values()):
                dgw.append(ev)
            if len(cnt) < nteams:
                bgw.append(ev)
        ndg, nbg = (dgw[0] if dgw else None), (bgw[0] if bgw else None)
        chips = [
            {"chip": "WC", "label": "Wildcard", "advice": "Hold", "note": "Save for the first big fixture swing — burning it early wastes flexibility."},
            {"chip": "FH", "label": "Free Hit", "advice": f"Target GW{nbg}" if nbg else "Hold", "note": "Best in a blank gameweek." + ("" if nbg else " None scheduled yet.")},
            {"chip": "BB", "label": "Bench Boost", "advice": f"Target GW{ndg}" if ndg else "Hold", "note": "Best in a double gameweek." + ("" if ndg else " None scheduled yet.")},
            {"chip": "TC", "label": "Triple Captain", "advice": f"Target GW{ndg}" if ndg else "Hold", "note": "Best on a premium in a double gameweek." + ("" if ndg else " None scheduled yet.")},
        ]
        return gw, opp, chips
    except Exception:
        return 0, {}, chips_default


def solve_milp(P):
    import numpy as np
    from scipy.optimize import milp, LinearConstraint, Bounds
    n = len(P); xph = np.array([p["xph"] for p in P]); price = np.array([p["price"] for p in P])
    pos = [p["pos"] for p in P]; club = [p["code"] for p in P]; clubs = sorted(set(club))
    Z = np.zeros(n); O = np.ones(n)
    rows, lo, hi = [], [], []
    def add(cs, ct, a, b): rows.append(np.concatenate([cs, ct])); lo.append(a); hi.append(b)
    add(O, Z, 15, 15)
    for ps, q in QUOTA.items():
        mask = np.array([1.0 if pos[i] == ps else 0 for i in range(n)]); add(mask, Z, q, q)
    add(price, Z, 0, BUDGET)
    for c in clubs:
        mask = np.array([1.0 if club[i] == c else 0 for i in range(n)]); add(mask, Z, 0, 3)
    add(Z, O, 11, 11)
    for ps in PT:
        mask = np.array([1.0 if pos[i] == ps else 0 for i in range(n)]); add(Z, mask, XI_MIN[ps], XI_MAX[ps])
    for i in range(n):  # t_i <= s_i
        cs = np.zeros(n); ct = np.zeros(n); cs[i] = -1; ct[i] = 1; add(cs, ct, -np.inf, 0)
    A = np.array(rows)
    res = milp(c=np.concatenate([Z, -xph]), constraints=LinearConstraint(A, lo, hi),
               integrality=np.ones(2 * n), bounds=Bounds(0, 1))
    if not res.success:
        raise RuntimeError("milp infeasible")
    x = res.x; s = [i for i in range(n) if x[i] > 0.5]; xi = {i for i in range(n) if x[n + i] > 0.5}
    return s, xi


def solve_greedy(P):
    # fallback: fill XI by best xph within budget+quota+club, then cheapest valid bench
    import itertools
    byv = sorted(range(len(P)), key=lambda i: -P[i]["xph"])
    # simple: take best XI shape 3-4-3 then cheapest bench; not optimal but valid
    pick, spend, clubn = [], 0.0, {}
    need_xi = {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}
    def ok(i):
        return clubn.get(P[i]["code"], 0) < 3 and spend + P[i]["price"] <= BUDGET
    xi = set()
    for ps in ["GK", "DEF", "MID", "FWD"]:
        for i in byv:
            if len([j for j in xi if P[j]["pos"] == ps]) >= need_xi[ps]:
                break
            if P[i]["pos"] == ps and i not in xi and ok(i):
                xi.add(i); spend += P[i]["price"]; clubn[P[i]["code"]] = clubn.get(P[i]["code"], 0) + 1
    s = set(xi)
    bench_need = {"GK": QUOTA["GK"] - 1, "DEF": QUOTA["DEF"] - need_xi["DEF"], "MID": QUOTA["MID"] - need_xi["MID"], "FWD": QUOTA["FWD"] - need_xi["FWD"]}
    cheap = sorted(range(len(P)), key=lambda i: P[i]["price"])
    for ps, cnt in bench_need.items():
        got = 0
        for i in cheap:
            if got >= cnt: break
            if P[i]["pos"] == ps and i not in s and clubn.get(P[i]["code"], 0) < 3 and spend + P[i]["price"] <= BUDGET:
                s.add(i); spend += P[i]["price"]; clubn[P[i]["code"]] = clubn.get(P[i]["code"], 0) + 1; got += 1
    return sorted(s), xi


def main() -> int:
    pj = json.loads((HERE / "data" / "players.json").read_text()) if (HERE / "data" / "players.json").exists() \
        else json.loads((OUT.parent / "players.json").read_text())
    pool = [p for p in pj["players"] if p.get("xph") is not None and p.get("min", 0) >= 300 and p.get("price")]
    # de-dupe to the strongest per id, keep needed fields
    P = [{"id": p["id"], "name": p["name"], "code": p["code"], "pos": p["pos"],
          "price": float(p["price"]), "xph": float(p["xph"]), "sel": p.get("sel", 0)} for p in pool]
    if len(P) < 15:
        OUT.write_text(json.dumps({"error": "not enough players", "rows": []}))
        print("not enough players to build a squad"); return 0

    try:
        s, xi = solve_milp(P)
    except Exception as e:
        print(f"  milp unavailable ({e}); using greedy fallback"); s, xi = solve_greedy(P)

    gw, opp, _chips = fixtures_info()
    members = [P[i] for i in s]
    # captain / vice = top two XI by xph
    xi_sorted = sorted([i for i in s if i in xi], key=lambda i: -P[i]["xph"])
    cap, vice = (xi_sorted[0] if xi_sorted else None), (xi_sorted[1] if len(xi_sorted) > 1 else None)
    rows = []
    for i in s:
        p = P[i]; bench = 0 if i in xi else 1
        rows.append([PT[p["pos"]], p["name"], 0, p["sel"], 0, 1 if i == cap else 0, 1 if i == vice else 0,
                     bench, opp.get(p["code"], ""), "", 0, 0, 0, round(p["xph"], 1), 0])
    spend = round(sum(P[i]["price"] for i in s), 1)
    xi_xph = round(sum(P[i]["xph"] for i in s if i in xi), 1)
    cnt = {ps: sum(1 for i in s if i in xi and P[i]["pos"] == ps) for ps in PT}
    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "event": gw,
           "budget": BUDGET, "spend": spend, "xi_xph": xi_xph,
           "formation": f"{cnt['DEF']}-{cnt['MID']}-{cnt['FWD']}", "rows": rows,
           "ids": [P[i]["id"] for i in s]}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: spend £{spend}m, XI {out['formation']}, XI xPts {xi_xph}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
