"""Pull matches for players: history -> salts -> Valve metadata (+ demo) -> tables."""
import json
import logging
from dataclasses import dataclass

import httpx

from . import paths, storage
from .knowledge import gametracking as gt
from .knowledge.tables import write_snapshot_tables, write_versions
from .metadata import flatten
from .sources import steam_gc, valve
from .sources.deadlock_api import DeadlockApi, RateLimited, cached

log = logging.getLogger(__name__)


@dataclass
class MatchResult:
    match_id: int
    status: str  # ingested | cached | no_metadata | rate_limited | error
    demo: bool = False
    detail: str = ""


def fetch_match(api: DeadlockApi, match_id: int, demos: bool = False, force: bool = False,
                salts: dict | None = None) -> MatchResult:
    """`salts` (e.g. from the Steam GC) skips the deadlock-api lookup."""
    d = paths.match_dir(match_id)
    if salts:
        d.mkdir(parents=True, exist_ok=True)
        (d / "salts.json").write_text(json.dumps(salts))
    meta_path = d / "meta.zst"
    demo_path = d / "demo.dem.bz2"
    if storage.has_match(match_id) and not force and (not demos or demo_path.exists()):
        return MatchResult(match_id, "cached", demo_path.exists())
    try:
        try:
            salts = cached(d / "salts.json", lambda: api.salts(match_id))
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404):
                return MatchResult(match_id, "no_metadata", detail="salts unavailable (match too old?)")
            raise
        if not meta_path.exists():
            if not salts.get("metadata_url"):
                return MatchResult(match_id, "no_metadata", detail="no metadata salt")
            blob = valve.download(salts["metadata_url"])
            d.mkdir(parents=True, exist_ok=True)
            meta_path.write_bytes(blob)
        storage.write_match_tables(match_id, flatten(valve.parse_metadata(meta_path.read_bytes())))
        if demos and not demo_path.exists():
            if not salts.get("demo_url") and salts.get("replay_salt") is None:
                # salts may have been cached before Valve published the replay; refresh once
                salts = cached(d / "salts.json", lambda: api.salts(match_id), max_age_s=0)
            if salts.get("demo_url"):
                try:
                    demo_path.write_bytes(valve.download(salts["demo_url"]))
                except httpx.HTTPStatusError as e:
                    if e.response.status_code != 404:
                        raise
                    return MatchResult(match_id, "ingested", detail="demo expired on Valve CDN")
        return MatchResult(match_id, "ingested", demo_path.exists())
    except RateLimited as e:
        return MatchResult(match_id, "rate_limited", detail=str(e))
    except (httpx.HTTPError, ValueError) as e:
        return MatchResult(match_id, "error", detail=f"{type(e).__name__}: {e}")


def ingest_demo(path, copy: bool = True) -> MatchResult:
    """Ingest a local replay (e.g. downloaded in the Deadlock client) from its embedded post-match details.

    Those carry everything in Valve's .meta file except match_paths (positions); `paths` stays empty until
    positions are extracted from the demo itself.
    """
    import shutil
    import subprocess
    from pathlib import Path

    from . import demo
    from .proto import cm

    path = Path(path)
    blob = demo.post_match_details(path)
    if blob is None:
        return MatchResult(0, "no_metadata", detail=f"{path.name}: no post-match details in demo")
    contents = cm.CMsgMatchMetaDataContents()
    contents.ParseFromString(blob)
    match_id = contents.match_info.match_id
    d = paths.match_dir(match_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "details_from_demo.pb").write_bytes(blob)
    if copy and not (d / "demo.dem").exists():
        shutil.copyfile(path, d / "demo.dem")
    tables = flatten(contents)
    detail = "from local demo"
    if not tables["paths"]:
        # the post-match broadcast has no match_paths: rebuild them from the demo at 1 Hz, in the same shape
        try:
            slots = {p["account_id"]: p["player_slot"] for p in tables["players"]}
            tables["paths"] = [
                {"match_id": match_id, "player_slot": slots[r["account_id"]], "game_time_s": r["game_time_s"],
                 "x": r["x"], "y": r["y"],
                 "health": round(100 * r["health"] / r["max_health"]) if r["max_health"] else None,
                 # combat/move state isn't in the demo extract; a string keeps the column type stable
                 "combat_type": "Unknown", "move_type": "Unknown"}
                for r in demo.positions(path) if r["alive"] and r["account_id"] in slots
            ]
            detail += ", positions from demo"
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            detail += f", no positions ({e})"
    storage.write_match_tables(match_id, tables)
    return MatchResult(match_id, "ingested", demo=True, detail=detail)


def fetch_replay(api: DeadlockApi, match_id: int) -> MatchResult:
    """Download one match's replay on demand (data/raw/matches/<id>/demo.dem, decompressed).

    Salts come from the cache, else the Steam GC, else deadlock-api. If the match isn't ingested yet or has no
    positions, the replay's embedded details and positions fill them in.
    """
    d = paths.match_dir(match_id)
    dem = d / "demo.dem"
    if not dem.exists():
        salts_path = d / "salts.json"
        salts = json.loads(salts_path.read_text()) if salts_path.exists() else {}
        if not salts.get("demo_url") and steam_gc.logged_in():
            salts = steam_gc.salts([match_id])[0].get(match_id) or salts
        if not salts.get("demo_url"):
            try:
                salts = api.salts(match_id)
            except (httpx.HTTPError, RateLimited) as e:
                return MatchResult(match_id, "error", detail=f"no replay salt: {e}")
        if not salts.get("demo_url"):
            return MatchResult(match_id, "no_metadata", detail="no replay salt (replay not published?)")
        d.mkdir(parents=True, exist_ok=True)
        salts_path.write_text(json.dumps(salts))
        try:
            blob = valve.download(salts["demo_url"])
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return MatchResult(match_id, "no_metadata", detail="replay expired on Valve CDN")
            raise
        tmp = dem.with_suffix(".tmp")
        tmp.write_bytes(valve.decompress(blob))
        tmp.replace(dem)
    has_paths = (paths.TABLES / "paths" / f"{match_id}.parquet").exists()
    if storage.has_match(match_id) and has_paths:
        return MatchResult(match_id, "ingested", demo=True, detail="replay downloaded")
    return ingest_demo(dem, copy=False)


def player_history(api: DeadlockApi, account_id: int, refresh: bool = True) -> list[dict]:
    """deadlock-api's stored history, merged with the full GC history when a Steam login is set up."""
    path = paths.RAW / "players" / f"{account_id}.json"
    if not refresh and path.exists():
        return json.loads(path.read_text())
    merged = {m["match_id"]: m for m in api.match_history(account_id)}
    if steam_gc.logged_in():
        try:
            for m in steam_gc.match_history(account_id):
                merged.setdefault(m["match_id"], m)
        except (steam_gc.InGame, RuntimeError) as e:
            log.warning("Steam GC history skipped: %s", e)
    history = sorted(merged.values(), key=lambda m: m["start_time"], reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history))
    return history


def sync_player(account_id: int, limit: int | None = None, demos: bool = False, refresh: bool = True):
    api = DeadlockApi()
    history = player_history(api, account_id, refresh)
    retry = []
    for m in history[:limit]:
        r = fetch_match(api, m["match_id"], demos=demos)
        if r.status in ("no_metadata", "rate_limited") and steam_gc.logged_in():
            retry.append(m["match_id"])  # deadlock-api had no salts: ask the GC directly, in one session
        else:
            yield r
    if retry:
        try:
            gc_salts, limited = steam_gc.salts(retry)
        except (steam_gc.InGame, RuntimeError) as e:
            log.warning("Steam GC salts skipped: %s", e)
            gc_salts, limited = {}, set(retry)
        for mid in retry:
            if mid in gc_salts:
                yield fetch_match(api, mid, demos=demos, salts=gc_salts[mid])
            elif mid in limited:
                yield MatchResult(mid, "rate_limited", detail="Steam GC rate limited; rerun sync later")
            else:
                yield MatchResult(mid, "no_metadata", detail="no salts from deadlock-api or Steam GC")


def sync_knowledge(update_repo: bool = True, all_versions: bool = False) -> list[int]:
    """Build knowledge snapshots for every client version referenced by ingested matches."""
    import duckdb

    gt.ensure_repo(update=update_repo)
    vs = gt.versions()
    write_versions(vs)
    if all_versions:
        wanted = {v.client_version for v in vs}
    else:
        files = str(paths.TABLES / "matches" / "*.parquet")
        starts = [r[0] for r in duckdb.sql(f"select start_time from read_parquet('{files}')").fetchall()] \
            if list((paths.TABLES / "matches").glob("*.parquet")) else []
        wanted = {gt.version_at(t, vs).client_version for t in starts} | {vs[-1].client_version}
    by_cv = {v.client_version: v for v in vs}
    built = []
    for cv in sorted(wanted):
        fresh = not gt.snapshot_current(paths.KNOWLEDGE / str(cv))
        snap = gt.build_snapshot(by_cv[cv])
        if fresh or not (snap / "abilities.parquet").exists():
            write_snapshot_tables(cv)
        built.append(cv)
    (paths.KNOWLEDGE / "index.json").write_text(json.dumps({"versions": built}))
    return built
