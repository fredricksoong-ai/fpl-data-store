#!/usr/bin/env python3
"""Build site/fpl_predictions.json (+ site/data.js) — the model contract for the view.

Self-contained: imports the vendored ./engine package, writes its output next to the
view in ./site. Fits the DC+Elo engine on Premier League results up to the target GW,
then projects per-player expected points, captaincy, a transfer shortlist and chip EV.

    python build_predictions.py            # auto-pick the upcoming GW
    python build_predictions.py 21         # a specific GW (handy for testing layout)
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE))
from engine import dixon_coles as dc, elo as E, data_pl, fpl_points as fp
from engine.data_pl import _norm

API = "https://fantasy.premierleague.com/api"
ENTRY = 822500
ARG_EVENT = int(sys.argv[1]) if len(sys.argv) > 1 else None
SITE = HERE / "site"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main() -> int:
    boot = get(f"{API}/bootstrap-static/")
    teams = {t["id"]: t["name"] for t in boot["teams"]}
    fixtures = get(f"{API}/fixtures/")

    finished = sorted({f["event"] for f in fixtures if f.get("finished") and f.get("event")})
    nxt = next((e["id"] for e in boot["events"] if e.get("is_next")), None)
    event = ARG_EVENT or nxt or (finished[-1] if finished else 1)
    if not any(f.get("event") == event for f in fixtures) and finished:
        event = finished[-1]
    print(f"target gameweek: GW{event}")

    hist = data_pl.load_from_fpl(fixtures, teams)
    train = hist[hist["event"] < event]
    model = dc.fit(train, xi=0.0019)
    elom = E.fit(train)

    fx_rows, fx_pred = [], []
    for f in (f for f in fixtures if f.get("event") == event):
        h, a = _norm(teams[f["team_h"]]), _norm(teams[f["team_a"]])
        if h not in model.attack or a not in model.attack:
            continue
        lam_h, lam_a = model.expected_goals(h, a, neutral=False)
        pen = E.ensemble_probs(model.outcome_probs(h, a, neutral=False),
                               elom.outcome_probs(h, a, neutral=False), w=0.45)
        fx_rows.append({"home_team": h, "away_team": a})
        fx_pred.append({"home": h, "away": a, "xg_home": round(lam_h, 2), "xg_away": round(lam_a, 2),
                        "p_home": round(pen["home"], 3), "p_draw": round(pen["draw"], 3), "p_away": round(pen["away"], 3)})
    gw_fx = pd.DataFrame(fx_rows)

    players = fp.build_players(boot)
    proj = fp.gameweek_points(model, players, gw_fx)
    proj_by_id = {int(r.id): r for r in proj.itertuples()}

    try:
        picks = get(f"{API}/entry/{ENTRY}/event/{event}/picks/")["picks"]
    except Exception:
        picks = []
        print(f"(no picks for GW{event} yet — squad/transfers/chips empty)")

    def player_row(pid):
        r = proj_by_id.get(pid)
        el = next((e for e in boot["elements"] if e["id"] == pid), None)
        return {"id": pid, "name": el["web_name"] if el else str(pid),
                "team": _norm(teams[el["team"]]) if el else "",
                "pos": fp.POS_NAME.get(el["element_type"]) if el else "",
                "price": (el["now_cost"] / 10.0) if el else None,
                "xpts": round(float(r.xpts), 2) if r is not None else 0.0}

    xi = [{**player_row(p["element"]), "is_captain": p["is_captain"], "is_vice": p["is_vice_captain"],
           "mult": p["multiplier"]} for p in picks if p["position"] <= 11]
    bench = [player_row(p["element"]) for p in picks if p["position"] > 11]
    cap = next((p for p in picks if p["is_captain"]), None)

    squad_ids = {p["element"] for p in picks}
    best_by_pos: dict = {}
    for r in proj.itertuples():
        best_by_pos.setdefault(r.pos, []).append(r)
    transfers = []
    for s in xi + bench:
        pool = [r for r in best_by_pos.get(s["pos"], []) if int(r.id) not in squad_ids]
        if not pool:
            continue
        top = max(pool, key=lambda r: r.xpts)
        gain = round(float(top.xpts) - s["xpts"], 2)
        if gain > 0.5:
            transfers.append({"out": s, "in": {"id": int(top.id), "name": top.name, "team": top.team,
                              "pos": top.pos, "price": round(float(top.price), 1), "xpts": round(float(top.xpts), 2)},
                              "gain": gain})
    transfers = sorted(transfers, key=lambda t: -t["gain"])[:5]

    bench_xpts = round(sum(p["xpts"] for p in bench), 1)
    best_cap = proj.head(1).iloc[0] if len(proj) else None
    chips = {"triple_captain": {"note": "extra points vs normal (×2) captain",
                                "best_player": best_cap["name"] if best_cap is not None else None,
                                "ev_extra": round(float(best_cap["xpts"]), 1) if best_cap is not None else 0},
             "bench_boost": {"note": "bench points you'd bank this GW", "ev_extra": bench_xpts}}

    out = {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "season": "live", "event": event,
        "model": "Dixon–Coles + Elo ensemble (w_DC=0.45), home advantage on",
        "team": {"id": ENTRY, "name": "wirtzplay"},
        "fixtures": fx_pred,
        "projection": [{"id": int(r.id), "name": r.name, "team": r.team, "pos": r.pos,
                        "price": round(float(r.price), 1), "xpts": round(float(r.xpts), 2),
                        "exp_goals": round(float(r.exp_goals), 2), "exp_assists": round(float(r.exp_assists), 2),
                        "cs_prob": round(float(r.cs_prob), 2), "n_fix": int(r.n_fix)}
                       for r in proj.head(50).itertuples()],
        "squad": {"xi": xi, "bench": bench, "xi_xpts": round(sum(p["xpts"] for p in xi), 1),
                  "bench_xpts": bench_xpts, "captain_id": cap["element"] if cap else None,
                  "cap_xpts": max((p["xpts"] for p in xi), default=0)},
        "captaincy": [{"name": c["name"], "team": c["team"], "pos": c["pos"],
                       "xpts": c["xpts"], "captain_ev": c["captain_ev"]}
                      for c in fp.captaincy(proj).to_dict("records")],
        "transfers": transfers, "chips": chips,
    }
    SITE.mkdir(exist_ok=True)
    (SITE / "fpl_predictions.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    (SITE / "data.js").write_text("window.FPL_DATA = " + json.dumps(out, ensure_ascii=False) + ";")
    print(f"wrote site/fpl_predictions.json (GW{event}, {len(out['projection'])} projected, {len(fx_pred)} fixtures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
