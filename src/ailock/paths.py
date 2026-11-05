"""Filesystem layout. Everything lives under AILOCK_DATA (default: ./data).

data/
  raw/matches/<match_id>/meta.zst      Valve metadata blob, untouched
  raw/matches/<match_id>/salts.json    replay CDN salts from deadlock-api
  raw/matches/<match_id>/demo.dem.bz2  demo, when Valve still has it
  raw/players/<account_id>.json        match history from deadlock-api
  tables/<table>/<match_id>.parquet    normalized match tables
  knowledge/<client_version>/...       versioned game data (heroes, items, abilities)
  ailock.duckdb                        views over all of the above
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# a worktree under .claude/worktrees/ shares the main checkout's data (it is gitignored, so worktrees start empty)
MAIN = ROOT.parents[2] if ROOT.parent.name == "worktrees" and ROOT.parents[1].name == ".claude" else ROOT
DATA = Path(os.environ.get("AILOCK_DATA", MAIN / "data"))
RAW = DATA / "raw"
TABLES = DATA / "tables"
KNOWLEDGE = DATA / "knowledge"
DB = DATA / "ailock.duckdb"
GAMETRACKING = Path(os.environ.get("AILOCK_GAMETRACKING", ROOT / "vendor/GameTracking-Deadlock"))


def match_dir(match_id: int) -> Path:
    return RAW / "matches" / str(match_id)
