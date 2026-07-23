#!/usr/bin/env python3
"""Build site/titleodds.json — model title / top-4 / relegation odds + projected table.

Self-contained for CI. Fits the Dixon-Coles + Elo ensemble on PL history (data/pl/
E0_*.csv) PLUS any finished matches of the live FPL season, then simulates the rest
of the season:

  • Pre-season  — FPL has no fixtures yet → use the static schedule (data/Fixtures-2026-27.csv),
                  every match unplayed, sim all 38 GWs from zero.
  • In-season   — FPL /fixtures/ carries the live schedule + results → bank actual points
                  for finished matches and Monte-Carlo only the remaining fixtures.

Writes site/titleodds.json (or $FPL_OUT_DIR/titleodds.json). Needs pandas, numpy, scipy.

    python build_titleodds.py
"""
from __future__ import annotations
import csv, glob, json, os, sys, urllib.request, datetime as dt
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
from engine import dixon_coles as dc, elo as E
from engine.data_pl import _norm, load_fd_csv

API = "https://fantasy.premierleague.com/api"
SITE = Path(os.environ["FPL_OUT_DIR"]).resolve() if os.environ.get("FPL_OUT_DIR") else HERE / "site"
SIMS = int(os.environ.get("SIMS", "20000"))

CODE2FD = {"ARS": "Arsenal", "AVL": "Aston Villa", "BOU": "Bournemouth", "BRE": "Brentford",
           "BHA": "Brighton", "CHE": "Chelsea", "COV": "Coventry", "CRY": "Crystal Palace",
           "EVE": "Everton", "FUL": "Fulham", "HUL": "Hull", "IPS": "Ipswich", "LEE": "Leeds",
           "LIV": "Liverpool", "MCI": "Man City", "MUN": "Man United", "NEW": "Newcastle",
           "NFO": "Nott'm Forest", "SUN": "Sunderland", "TOT": "Tottenham"}


def get(path, tries=5):
    req = urllib.request.Request(API + path, headers={"User-Agent": "Mozilla/5.0"})
    for _i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as _e:
            # retry transient failures (launch-week 503s, timeouts, 429s) but fail fast on real client errors
            if _i == tries - 1 or getattr(_e, "code", None) in (400, 401, 403, 404):
                raise
            import time; time.sleep(2 * (_i + 1))


def load_schedule():
    """Return (codes, played, remaining). Each match: dict(home,away[,gh,ga]).
    Prefer the live FPL schedule; fall back to the static CSV when FPL has no season loaded."""
    boot = get("/bootstrap-static/")
    short = {t["id"]: t["short_name"] for t in boot["teams"]}
    fixtures = get("/fixtures/")
    sched = [f for f in fixtures if f.get("event")]
    live = any(not f.get("finished") for f in sched)
    if live:
        played, remaining = [], []
        for f in sched:
            h, a = short[f["team_h"]], short[f["team_a"]]
            if f.get("finished") and f.get("team_h_score") is not None:
                played.append({"home": h, "away": a, "gh": int(f["team_h_score"]), "ga": int(f["team_a_score"])})
            else:
                remaining.append({"home": h, "away": a})
        codes = sorted({m["home"] for m in played + remaining} | {m["away"] for m in played + remaining})
        return codes, played, remaining, "live"
    rows = list(csv.DictReader(open(HERE / "data" / "Fixtures-2026-27.csv")))
    remaining = [{"home": r["home"], "away": r["away"]} for r in rows]
    return list(CODE2FD), [], remaining, "static"


def main() -> int:
    files = sorted(glob.glob(str(HERE / "data" / "pl" / "E0_*.csv")))
    hist = pd.concat([load_fd_csv(f) for f in files], ignore_index=True)

    codes, played, remaining, mode = load_schedule()
    key = {c: _norm(CODE2FD.get(c, c)) for c in codes}

    # fold finished live matches into training so the model learns current-season form
    if played:
        live_rows = [{"date": pd.Timestamp.utcnow().tz_localize(None), "home_team": key[m["home"]],
                      "away_team": key[m["away"]], "home_score": m["gh"], "away_score": m["ga"],
                      "tournament": "Premier League", "neutral": False} for m in played]
        hist = pd.concat([hist, pd.DataFrame(live_rows)], ignore_index=True)
    hist = hist.sort_values("date").reset_index(drop=True)
    model = dc.fit(hist, xi=0.0019)
    elom = E.fit(hist)

    idx = {c: i for i, c in enumerate(codes)}
    # base points from finished matches
    base = np.zeros(len(codes), dtype=np.int32)
    for m in played:
        if m["gh"] > m["ga"]: base[idx[m["home"]]] += 3
        elif m["gh"] < m["ga"]: base[idx[m["away"]]] += 3
        else: base[idx[m["home"]]] += 1; base[idx[m["away"]]] += 1

    # probabilities for remaining fixtures
    ph, pd_, hi, ai = [], [], [], []
    for m in remaining:
        h, a = key[m["home"]], key[m["away"]]
        pen = E.ensemble_probs(model.outcome_probs(h, a, neutral=False),
                               elom.outcome_probs(h, a, neutral=False), w=0.45)
        ph.append(pen["home"]); pd_.append(pen["draw"]); hi.append(idx[m["home"]]); ai.append(idx[m["away"]])
    ph, pd_ = np.array(ph), np.array(pd_)
    hi, ai = np.array(hi, dtype=int), np.array(ai, dtype=int)

    rng = np.random.default_rng(7)
    pts = np.tile(base, (SIMS, 1)).astype(np.int16)
    for f in range(len(remaining)):
        u = rng.random(SIMS); hw = u < ph[f]; dr = (u >= ph[f]) & (u < ph[f] + pd_[f]); aw = ~(hw | dr)
        pts[hw, hi[f]] += 3; pts[aw, ai[f]] += 3; pts[dr, hi[f]] += 1; pts[dr, ai[f]] += 1
    order = np.argsort(-(pts + rng.random(pts.shape) * 0.01), axis=1)
    rank = np.empty_like(order)
    for s in range(SIMS): rank[s, order[s]] = np.arange(len(codes))
    title = (rank == 0).mean(0); top4 = (rank < 4).mean(0); releg = (rank >= 17).mean(0); mean_pts = pts.mean(0)

    teams = []
    for c in codes:
        i = idx[c]; k = key[c]
        teams.append({"code": c, "team": k, "elo": round(elom.ratings.get(k, 1500)),
                      "pts": round(float(mean_pts[i]), 1), "gd": 0,
                      "title": round(float(title[i]) * 100, 1), "top4": round(float(top4[i]) * 100, 1),
                      "releg": round(float(releg[i]) * 100, 1)})
    teams.sort(key=lambda t: -t["pts"])
    for i, t in enumerate(teams, 1): t["rank"] = i

    out = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
           "season": "2026-27", "sims": SIMS, "mode": mode, "played": len(played),
           "trained_through": str(hist.date.max().date()),
           "model": "Dixon–Coles + Elo ensemble (w_DC=0.45)", "teams": teams}
    SITE.mkdir(exist_ok=True)
    (SITE / "titleodds.json").write_text(json.dumps(out, ensure_ascii=False))
    print(f"wrote {SITE}/titleodds.json: {mode} mode, {len(played)} played, leader {teams[0]['code']} {teams[0]['title']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
