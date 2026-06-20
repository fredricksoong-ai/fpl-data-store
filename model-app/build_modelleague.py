#!/usr/bin/env python3
"""Build modelleague.json — the Model League: each model's accuracy over played GWs.

Mirrors WC26's League. For every finished gameweek we fit DC+Elo on results *before*
that GW (no hindsight), predict its fixtures, and score each model against the actual
result: 1 pt for the correct result, 3 for the exact score (non-additive), plus RPS.
Market is scored from the frozen per-fixture odds in market_history.json (so it only
accrues for fixtures we had priced — exactly like WC26's market ledger). "You" is not
here — the app scores your saved picks client-side against the games list below.

Output: {generated, event, models:[{key,label,color,games,result_hits,exact_hits,pts,rps,
         series:[cumulative pts by gw]}], gws:[...], games:[{gw,home,away,gh,ga}]}
Needs pandas, numpy, scipy.
"""
from __future__ import annotations
import glob, json, os, sys, urllib.request, datetime as dt
from pathlib import Path
import pandas as pd, numpy as np

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
from engine import dixon_coles as dc, elo as E
from engine.data_pl import _norm, load_fd_csv

API = "https://fantasy.premierleague.com/api"
OUT = Path(os.environ.get("ML_OUT", "modelleague.json"))
MAXGW = int(os.environ["ML_MAXGW"]) if os.environ.get("ML_MAXGW") else None
RP, BONUS = 1, 2  # result=1; exact=3 total -> bonus 2 over the result

CODE2FD = {"ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
           "BHA": "Brighton", "CHE": "Chelsea", "COV": "Coventry", "CRY": "Crystal Palace",
           "EVE": "Everton", "FUL": "Fulham", "HUL": "Hull", "IPS": "Ipswich", "LEE": "Leeds",
           "LIV": "Liverpool", "MCI": "Man City", "MUN": "Man United", "NEW": "Newcastle",
           "NFO": "Nott'm Forest", "SUN": "Sunderland", "TOT": "Tottenham"}
LABELS = {"dc": "Dixon–Coles", "elo": "Elo", "ens": "Ensemble", "market": "Market"}
COLORS = {"dc": "#facc15", "elo": "#60a5fa", "ens": "#a78bfa", "market": "#e5e7eb"}


def get(path):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def rps(probs, outcome):  # ordered [home, draw, away]; outcome in {0,1,2}
    a = [0, 0, 0]; a[outcome] = 1
    c_p = c_a = 0.0; s = 0.0
    for i in range(2):
        c_p += probs[i]; c_a += a[i]; s += (c_p - c_a) ** 2
    return 0.5 * s


def outcome_of(gh, ga):
    return 0 if gh > ga else 2 if gh < ga else 1


def main() -> int:
    hist = pd.concat([load_fd_csv(f) for f in sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))],
                     ignore_index=True).sort_values("date").reset_index(drop=True)
    csv_max = hist.date.max()

    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    fixtures = get("/fixtures/")
    fin = [f for f in fixtures if f.get("finished") and f.get("team_h_score") is not None and f.get("event")]

    # add live-season finished results that post-date the CSVs (avoids double-counting 2025/26)
    live_rows = []
    for f in fin:
        d = pd.to_datetime(f["kickoff_time"]).tz_localize(None) if f.get("kickoff_time") else None
        if d is not None and d > csv_max:
            live_rows.append({"date": d, "home_team": _norm(CODE2FD.get(short[f["team_h"]], short[f["team_h"]])),
                              "away_team": _norm(CODE2FD.get(short[f["team_a"]], short[f["team_a"]])),
                              "home_score": int(f["team_h_score"]), "away_score": int(f["team_a_score"]),
                              "tournament": "Premier League", "neutral": False})
    if live_rows:
        hist = pd.concat([hist, pd.DataFrame(live_rows)], ignore_index=True).sort_values("date").reset_index(drop=True)

    try:
        market_hist = json.loads((OUT.parent / "market_history.json").read_text()).get("events", {})
    except Exception:
        market_hist = {}

    by_gw = {}
    for f in fin:
        by_gw.setdefault(f["event"], []).append(f)
    gws = sorted(by_gw)
    if MAXGW:
        gws = [g for g in gws if g <= MAXGW]

    keys = ["ens", "dc", "elo", "market"]
    acc = {k: {"games": 0, "rh": 0, "eh": 0, "rps": 0.0, "byw": {}, "rw": {}, "nw": {}} for k in keys}
    games = []

    for g in gws:
        gwfix = by_gw[g]
        kos = [pd.to_datetime(f["kickoff_time"]).tz_localize(None) for f in gwfix if f.get("kickoff_time")]
        cutoff = min(kos) if kos else csv_max
        train = hist[hist.date < cutoff]
        model = dc.fit(train, xi=0.0019); elom = E.fit(train)
        for f in gwfix:
            hc, ac = short[f["team_h"]], short[f["team_a"]]
            hk, ak = _norm(CODE2FD.get(hc, hc)), _norm(CODE2FD.get(ac, ac))
            if hk not in model.attack or ak not in model.attack:
                continue
            gh, ga = int(f["team_h_score"]), int(f["team_a_score"])
            o = outcome_of(gh, ga); actual_score = f"{gh}-{ga}"
            games.append({"gw": g, "home": hc, "away": ac, "gh": gh, "ga": ga})
            dcp = model.outcome_probs(hk, ak, neutral=False)
            elop = elom.outcome_probs(hk, ak, neutral=False)
            pen = E.ensemble_probs(dcp, elop, w=0.45)
            (di, dj), _ = model.most_likely_scores(hk, ak, top=1, neutral=False)[0]
            (ei, ej), _ = elom.most_likely_scores(hk, ak, top=1, neutral=False)[0]
            preds = {
                "ens": ([pen["home"], pen["draw"], pen["away"]], f"{di}-{dj}"),
                "dc":  ([dcp["home"], dcp["draw"], dcp["away"]], f"{di}-{dj}"),
                "elo": ([elop["home"], elop["draw"], elop["away"]], f"{ei}-{ej}"),
            }
            mk = market_hist.get(f"{hc}|{ac}")
            if mk:
                preds["market"] = ([mk["ph"], mk["pd"], mk["pa"]], None)
            for k, (probs, score) in preds.items():
                pick = int(np.argmax(probs))
                rh = pick == o; eh = (score == actual_score); r = rps(probs, o)
                a = acc[k]; a["games"] += 1; a["rh"] += rh; a["eh"] += eh; a["rps"] += r
                a["byw"][g] = a["byw"].get(g, 0) + (RP if rh else 0) + (BONUS if eh else 0)
                a["rw"][g] = a["rw"].get(g, 0) + r; a["nw"][g] = a["nw"].get(g, 0) + 1

    models = []
    for k in keys:
        a = acc[k]
        if not a["games"]:
            continue
        cum = 0; series = []; rsum = 0.0; nsum = 0; series_rps = []
        for g in gws:
            cum += a["byw"].get(g, 0); series.append(cum)
            rsum += a["rw"].get(g, 0); nsum += a["nw"].get(g, 0)
            series_rps.append(round(rsum / nsum, 4) if nsum else None)
        models.append({"key": k, "label": LABELS[k], "color": COLORS[k], "games": a["games"],
                       "result_hits": a["rh"], "exact_hits": a["eh"],
                       "pts": a["rh"] * RP + a["eh"] * BONUS, "rps": round(a["rps"] / a["games"], 4),
                       "series": series, "series_rps": series_rps})
    models.sort(key=lambda m: (-m["pts"], m["rps"]))

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "event": gws[-1] if gws else 0, "scoring": {"result": RP, "exact": RP + BONUS},
           "gws": gws, "models": models, "games": games}
    OUT.write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {OUT}: {len(gws)} GWs, {len(games)} games, models {[m['key']+' '+str(m['pts']) for m in models]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
