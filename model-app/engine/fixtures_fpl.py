"""Load upcoming Premier League fixtures from the FPL API and predict on them.

Mirrors src/fixtures.py (the World Cup loader) but for club football:
  * fixtures come from the FPL API /fixtures/ (key-free, always reachable),
  * team names are normalised to the same FPL convention data_pl uses, so the
    names line up with a model trained on either football-data history or FPL results,
  * predictions use neutral=False (club football has real home advantage).

The next-season fixture list is published by the FPL API around mid-July; until then
this loads the current/just-finished season, which is what the in-sandbox backtest uses.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pandas as pd

API = "https://fantasy.premierleague.com/api"


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def fetch_bootstrap_and_fixtures() -> tuple[dict, list[dict]]:
    boot = _get(f"{API}/bootstrap-static/")
    teams = {t["id"]: t["name"] for t in boot["teams"]}
    fixtures = _get(f"{API}/fixtures/")
    return teams, fixtures


def upcoming_fixtures(teams: dict, fixtures: list[dict], event: int | None = None) -> pd.DataFrame:
    """Unplayed fixtures as a frame: date, event, home_team, away_team (FPL names).

    event=None returns all unplayed fixtures; pass a GW number to filter to one.
    """
    from .data_pl import _norm  # same normalisation as the history loader

    rows = []
    for f in fixtures:
        if f.get("finished") or f.get("team_h_score") is not None:
            continue
        if event is not None and f.get("event") != event:
            continue
        rows.append(
            {
                "date": pd.to_datetime(f["kickoff_time"]) if f.get("kickoff_time") else pd.NaT,
                "event": f.get("event"),
                "home_team": _norm(teams[f["team_h"]]),
                "away_team": _norm(teams[f["team_a"]]),
            }
        )
    return pd.DataFrame(rows).sort_values(["event", "date"]).reset_index(drop=True)


def predict_fixtures(model, fixtures_df: pd.DataFrame, neutral: bool = False) -> pd.DataFrame:
    """Attach 1X2 probs (and expected goals if the model has them) to each fixture.

    `model` is any engine model exposing outcome_probs(h, a, neutral=...) — a
    DixonColesModel, EloModel, or an ensemble wrapper. Unknown teams (e.g. a newly
    promoted side with no history) are skipped with a printed warning.
    """
    known = set(getattr(model, "teams", []) or [])
    out = []
    unmatched = set()
    for r in fixtures_df.itertuples():
        if known and (r.home_team not in known or r.away_team not in known):
            unmatched.update({r.home_team, r.away_team} - known)
            continue
        p = model.outcome_probs(r.home_team, r.away_team, neutral=neutral)
        row = {"date": r.date, "event": r.event, "home_team": r.home_team,
               "away_team": r.away_team, "p_home": p["home"], "p_draw": p["draw"], "p_away": p["away"]}
        if hasattr(model, "expected_goals"):
            lh, la = model.expected_goals(r.home_team, r.away_team, neutral=neutral)
            row["xg_home"], row["xg_away"] = round(lh, 2), round(la, 2)
        out.append(row)
    if unmatched:
        print(f"WARN unmatched fixture team names (no history): {sorted(unmatched)}")
    return pd.DataFrame(out)
