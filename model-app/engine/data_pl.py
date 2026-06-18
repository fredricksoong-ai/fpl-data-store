"""Load Premier League club-match history into the engine's canonical frame.

The DC/Elo fit functions expect columns:
    date, home_team, away_team, home_score, away_score, tournament, neutral
(plus optional odds columns for the bookmaker baseline). This module produces
exactly that from two sources:

  1. football-data.co.uk  — the PRODUCTION source. Multi-season CSVs back to 1993,
     and they bundle closing bookmaker odds (B365H/D/A) → free market RPS baseline.
     Run `fetch_pl_data.py` locally to download the season CSVs into data/raw/pl/.
     (Note: football-data.co.uk is not reachable from the sandbox proxy; pull it in
     your own environment, or via the WebFetch tool.)

  2. FPL API /fixtures/  — the CURRENT-season source, always reachable. One season of
     results, no odds. Used for the immediate in-sandbox backtest.

Both paths return the same canonical schema, so the model never knows the difference.
Team names are normalised to the FPL convention (Man Utd, Spurs, Nott'm Forest, ...).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

# football-data.co.uk season-code -> URL. E0 = England Premier League.
FD_BASE = "https://www.football-data.co.uk/mmz4281"
# Last six completed seasons by default. Add older codes (e.g. "1819") for more history.
DEFAULT_SEASONS = ["1920", "2021", "2122", "2223", "2324", "2425"]

# football-data.co.uk name -> FPL canonical name. Only the ones that differ need listing;
# anything not in the map passes through unchanged.
FD_TO_FPL = {
    "Man United": "Man Utd",
    "Tottenham": "Spurs",
    "Newcastle": "Newcastle",
    "Nott'm Forest": "Nott'm Forest",
    "Sheffield United": "Sheffield Utd",
    "West Brom": "West Brom",
    "Wolves": "Wolves",
    "West Ham": "West Ham",
    "Man City": "Man City",
}


def _norm(name: str) -> str:
    return FD_TO_FPL.get(str(name).strip(), str(name).strip())


# ---- source 1: football-data.co.uk -----------------------------------------
def load_fd_csv(path_or_url: str | Path) -> pd.DataFrame:
    """One PL season CSV -> canonical frame (with odds where present).

    Works for both the datasets/football-datasets mirror (UTF-8, ISO dates) and
    football-data.co.uk direct (latin-1 + BOM, dd/mm/yyyy dates).
    """
    try:
        df = pd.read_csv(path_or_url, encoding="utf-8-sig")
    except (UnicodeDecodeError, ValueError):
        df = pd.read_csv(path_or_url, encoding="latin-1")
    need = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"PL CSV missing columns: {missing}")
    # Date format differs by source and is ambiguous (12/08/2023 is valid both ways),
    # so pick by separator: '/' = football-data dd/mm/yyyy, '-' = mirror ISO yyyy-mm-dd.
    sample = df["Date"].dropna().astype(str).iloc[0] if len(df) else ""
    dayfirst = "/" in sample
    date = pd.to_datetime(df["Date"], dayfirst=dayfirst, errors="coerce")
    out = pd.DataFrame(
        {
            "date": date,
            "home_team": df["HomeTeam"].map(_norm),
            "away_team": df["AwayTeam"].map(_norm),
            "home_score": df["FTHG"],
            "away_score": df["FTAG"],
            "tournament": "Premier League",
            "neutral": False,
        }
    )
    # Closing bookmaker odds for the market baseline, if the columns exist.
    for src, dst in [("B365H", "odds_h"), ("B365D", "odds_d"), ("B365A", "odds_a")]:
        if src in df.columns:
            out[dst] = pd.to_numeric(df[src], errors="coerce")
    out = out.dropna(subset=["date", "home_score", "away_score"]).copy()
    out["home_score"] = out["home_score"].astype(int)
    out["away_score"] = out["away_score"].astype(int)
    return out.sort_values("date").reset_index(drop=True)


def load_seasons_dir(raw_dir: str | Path) -> pd.DataFrame:
    """Concatenate every E0_*.csv already downloaded into data/raw/pl/."""
    raw_dir = Path(raw_dir)
    files = sorted(raw_dir.glob("E0_*.csv"))
    if not files:
        raise FileNotFoundError(f"No E0_*.csv in {raw_dir}. Run fetch_pl_data.py first.")
    frames = [load_fd_csv(f) for f in files]
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


# ---- source 2: FPL API (current season, always reachable) ------------------
def load_from_fpl(fixtures: list[dict], teams: dict) -> pd.DataFrame:
    """FPL /fixtures/ + bootstrap teams -> canonical frame (finished matches, no odds).

    teams: {team_id: team_name}. fixtures: the raw /fixtures/ list.
    """
    teams = {int(k): v for k, v in teams.items()}
    rows = []
    for f in fixtures:
        if not f.get("finished") or f.get("team_h_score") is None:
            continue
        rows.append(
            {
                "date": pd.to_datetime(f["kickoff_time"]),
                "home_team": _norm(teams[f["team_h"]]),
                "away_team": _norm(teams[f["team_a"]]),
                "home_score": int(f["team_h_score"]),
                "away_score": int(f["team_a_score"]),
                "tournament": "Premier League",
                "neutral": False,
                "event": f.get("event"),
            }
        )
    df = pd.DataFrame(rows)
    # FPL timestamps are tz-aware; drop tz so they compare with naive football-data dates.
    df["date"] = df["date"].dt.tz_localize(None)
    return df.sort_values("date").reset_index(drop=True)


def load_fpl_json(path: str | Path) -> pd.DataFrame:
    """Convenience: load from a saved {'teams':..., 'fixtures':...} JSON blob."""
    blob = json.loads(Path(path).read_text())
    return load_from_fpl(blob["fixtures"], blob["teams"])
