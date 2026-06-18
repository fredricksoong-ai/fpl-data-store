"""Turn engine expected-goals (λ) into per-player expected FPL points.

This is the bridge that makes the World Cup match engine useful for FPL. The engine
gives, per fixture, a team's expected goals for (λ_for) and against (λ_against). FPL
points, though, are per *player* — so we distribute each team's expected goals and
assists across its players using the per-90 attacking rates the FPL bootstrap already
publishes (expected_goals_per_90, expected_assists_per_90), weighted by how likely each
player is to be on the pitch.

    team λ_for      ──split by xG90 share──►  player expected goals  ──×position pts──┐
    team λ_for·AF   ──split by xA90 share──►  player expected assists ──×3────────────┤
    λ_against       ──Poisson P(0)─────────►  clean-sheet prob ──×CS pts (if 60')─────┤──► EP
    λ_against       ──Poisson floor(k/2)───►  goals-conceded penalty (GK/DEF)─────────┤
    saves_per_90    ──/3───────────────────►  save points (GK)──────────────────────-┘
    + appearance points from start/▢60 probability

Scope of this first version — included: appearance, goals, assists, clean sheets,
goals-conceded, saves. Deferred (documented TODOs, don't materially change ranking):
bonus (BPS), the new defensive-contribution points, cards, penalties saved/missed.

Minutes model (the flagged decision): a deliberately simple read off FPL data —
start share = starts/games_played, scaled by availability (status + chance_of_playing).
Good enough to rank; can be upgraded to a proper lineup/rotation model later.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import poisson

# FPL scoring constants
GOAL_PTS = {1: 6, 2: 6, 3: 5, 4: 4}     # by element_type: GKP, DEF, MID, FWD
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}        # clean-sheet points (need 60+ mins)
ASSIST_PTS = 3
APP_60_PTS, APP_SUB_PTS = 2, 1
ASSIST_FACTOR = 0.70                      # league ~assists per goal; splits team λ into assists
POS_NAME = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
UNAVAILABLE = {"i", "s", "u", "n", "o"}   # injured/suspended/unavailable/not-in-squad/on-loan


# --- player table ------------------------------------------------------------
def build_players(bootstrap: dict, games_played: int | None = None) -> pd.DataFrame:
    """Flatten the bootstrap into a per-player table with rates and minutes priors.

    games_played defaults to the number of finished gameweeks (for start share).
    """
    if games_played is None:
        games_played = sum(1 for e in bootstrap.get("events", []) if e.get("finished")) or 38
    from .data_pl import _norm
    teams = {t["id"]: _norm(t["name"]) for t in bootstrap["teams"]}

    rows = []
    for e in bootstrap["elements"]:
        avail = 0.0 if e.get("status") in UNAVAILABLE else (
            (e.get("chance_of_playing_next_round") if e.get("chance_of_playing_next_round") is not None else 100) / 100.0
        )
        starts = float(e.get("starts") or 0)
        mins = float(e.get("minutes") or 0)
        p_start = min(1.0, starts / games_played) if games_played else 0.0
        # sub appearances ≈ matches featured beyond starts
        sub_games = max(0.0, mins / 90.0 - starts)
        p_sub = min(1.0, sub_games / games_played) if games_played else 0.0
        p60 = p_start * avail            # a start ≈ likely to reach 60'
        p_sub_eff = p_sub * avail
        rows.append({
            "id": e["id"], "name": e["web_name"], "pos": int(e["element_type"]),
            "team": teams[e["team"]], "price": (e.get("now_cost") or 0) / 10.0,
            "xg90": float(e.get("expected_goals_per_90") or 0),
            "xa90": float(e.get("expected_assists_per_90") or 0),
            "saves90": float(e.get("saves_per_90") or 0),
            "p60": p60, "p_sub": p_sub_eff, "avail": avail, "status": e.get("status"),
            "total_points": e.get("total_points"),
        })
    return pd.DataFrame(rows)


# --- per-fixture expected points --------------------------------------------
def _conceded_penalty(lam_against: float, kmax: int = 12) -> float:
    """Expected goals-conceded points for a GK/DEF: -1 per 2 conceded (exact over Poisson)."""
    k = np.arange(kmax + 1)
    return float(-np.sum(poisson.pmf(k, lam_against) * (k // 2)))


def team_expected_points(players: pd.DataFrame, lam_for: float, lam_against: float) -> pd.DataFrame:
    """Expected points for every player of ONE team in ONE fixture.

    players: the rows of build_players() for a single team.
    lam_for: that team's expected goals; lam_against: expected goals conceded.
    """
    p = players.copy()
    # on-pitch attacking weights
    gw = (p["xg90"] * p["p60"])
    aw = (p["xa90"] * p["p60"])
    gw_tot, aw_tot = gw.sum(), aw.sum()
    p["exp_goals"] = lam_for * (gw / gw_tot) if gw_tot > 0 else 0.0
    p["exp_assists"] = (lam_for * ASSIST_FACTOR) * (aw / aw_tot) if aw_tot > 0 else 0.0

    cs_prob = float(np.exp(-lam_against))           # Poisson P(0 conceded)
    conc_pen = _conceded_penalty(lam_against)        # negative, GK/DEF only

    ep_app = APP_60_PTS * p["p60"] + APP_SUB_PTS * p["p_sub"]
    ep_goal = p["exp_goals"] * p["pos"].map(GOAL_PTS)
    ep_assist = p["exp_assists"] * ASSIST_PTS
    ep_cs = p["pos"].map(CS_PTS) * cs_prob * p["p60"]
    ep_conc = np.where(p["pos"].isin([1, 2]), conc_pen * p["p60"], 0.0)
    ep_saves = np.where(p["pos"] == 1, p["saves90"] * p["p60"] / 3.0, 0.0)

    p["cs_prob"] = round(cs_prob, 3)
    p["xpts"] = ep_app + ep_goal + ep_assist + ep_cs + ep_conc + ep_saves
    return p


def fixture_points(model, players: pd.DataFrame, home_team: str, away_team: str) -> pd.DataFrame:
    """Expected points for both teams in a fixture, using the engine's expected goals."""
    lam_h, lam_a = model.expected_goals(home_team, away_team, neutral=False)
    home = team_expected_points(players[players["team"] == home_team], lam_h, lam_a)
    away = team_expected_points(players[players["team"] == away_team], lam_a, lam_h)
    home["opp"], away["opp"] = away_team, home_team
    home["venue"], away["venue"] = "H", "A"
    return pd.concat([home, away], ignore_index=True)


def gameweek_points(model, players: pd.DataFrame, fixtures_df: pd.DataFrame) -> pd.DataFrame:
    """Per-player expected points across a gameweek's fixtures.

    fixtures_df needs columns home_team, away_team (e.g. from fixtures_fpl.upcoming_fixtures).
    Doubles (a team playing twice) sum naturally — group by player id at the end.
    """
    known = set(getattr(model, "teams", []) or [])
    parts = []
    for r in fixtures_df.itertuples():
        if known and (r.home_team not in known or r.away_team not in known):
            continue
        parts.append(fixture_points(model, players, r.home_team, r.away_team))
    if not parts:
        return pd.DataFrame()
    allp = pd.concat(parts, ignore_index=True)
    agg = (allp.groupby(["id", "name", "pos", "team", "price"], as_index=False)
                .agg(xpts=("xpts", "sum"), exp_goals=("exp_goals", "sum"),
                     exp_assists=("exp_assists", "sum"), cs_prob=("cs_prob", "max"),
                     n_fix=("xpts", "size")))
    agg["pos"] = agg["pos"].map(POS_NAME)
    return agg.sort_values("xpts", ascending=False).reset_index(drop=True)


def captaincy(gw_table: pd.DataFrame, top: int = 10) -> pd.DataFrame:
    """Captaincy ranking = expected points × 2 (the armband doubles the haul)."""
    out = gw_table.head(top * 2).copy()
    out["captain_ev"] = (out["xpts"] * 2).round(2)
    return out[["name", "team", "pos", "xpts", "captain_ev", "cs_prob", "n_fix"]].head(top).reset_index(drop=True)
