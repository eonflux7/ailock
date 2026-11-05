"""Deadlock game coordinator via your own Steam login (steam/gc.mjs, Node).

Gives what deadlock-api only has partially: your full match history, and replay salts for any match
without deadlock-api's 3/hour lookup limit. Log in once with `ailock steam-login` (QR code).
"""
import json
import os
import subprocess

from .. import paths
from . import valve

STEAM_DIR = paths.ROOT / "steam"
TOKEN = paths.DATA / "steam" / "refresh_token"


class InGame(Exception):
    """The account is playing; starting a GC session would kick the game."""


def logged_in() -> bool:
    return TOKEN.exists()


class RateLimited(Exception):
    """The GC refused lookups for now; its quota resets on its own (retry later)."""


def _run(*args: str, timeout: float | None = 900, partial_ok: bool = False) -> str:
    r = subprocess.run(["node", str(STEAM_DIR / "gc.mjs"), *args], capture_output=True, text=True,
                       timeout=timeout, env={**os.environ, "AILOCK_DATA": str(paths.DATA)})
    if r.returncode == 3:
        raise InGame(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "account is in a game")
    if r.returncode != 0 and not (partial_ok and r.stdout.strip()):
        raise RuntimeError(f"gc.mjs {args[0]} failed: {r.stderr.strip()[-500:]}")
    return r.stdout


def match_history(account_id: int | None = None, max_pages: int | None = None) -> list[dict]:
    """Every match the GC lists for the account (yours by default), newest first."""
    args = ["history"] + ([str(account_id)] if account_id else []) + \
        (["--max-pages", str(max_pages)] if max_pages else [])
    return json.loads(_run(*args) or "[]")


def salts(match_ids: list[int]) -> tuple[dict[int, dict], set[int]]:
    """(salts in the same shape deadlock-api returns keyed by match id, ids the GC rate-limited).

    Ids in neither had no salts at the GC. Lookups that finished before a failure are kept.
    """
    out, limited, answered = {}, set(), set()
    for line in _run("salts", *map(str, match_ids), timeout=None, partial_ok=True).splitlines():
        r = json.loads(line)
        answered.add(r["match_id"])
        if r["result"] == "k_eResult_RateLimited":
            limited.add(r["match_id"])
        if r["result"] != "k_eResult_Success" or not r["cluster_id"]:
            continue
        mid, cluster = r["match_id"], r["cluster_id"]
        out[mid] = {
            "match_id": mid, "cluster_id": cluster, "metadata_salt": r["metadata_salt"],
            "replay_salt": r["replay_salt"],
            "metadata_url": valve.replay_url(mid, cluster, r["metadata_salt"], "meta.bz2")
            if r["metadata_salt"] else None,
            "demo_url": valve.replay_url(mid, cluster, r["replay_salt"], "dem.bz2") if r["replay_salt"] else None,
            "source": "steam_gc",
        }
    # ids the GC never answered (session dropped) count as rate limited too
    limited |= set(match_ids) - answered
    return out, limited
