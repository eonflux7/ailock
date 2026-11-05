"""Population benchmarks from deadlock-api analytics + build browser.

Fetched per hero over a time window (default: last 30 days) and stored as
parquet under data/benchmarks/, tagged with the window so reports can say
exactly what they compared against.
"""
import hashlib
import json
import logging
import time

import httpx

from . import paths
from .sources.deadlock_api import DeadlockApi, cached
from .storage import write_rows

log = logging.getLogger(__name__)
BENCH = paths.DATA / "benchmarks"
DAY = 86400


def _window(days: int, min_badge: int | None, max_badge: int | None) -> dict:
    # round to the day so repeated runs reuse the cache
    now = int(time.time()) // DAY * DAY
    return {"min_unix_timestamp": now - days * DAY, "min_average_badge": min_badge, "max_average_badge": max_badge}


def _key(name: str, params: dict) -> str:
    return f"{name}-{hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:10]}"


def _fetch(api: DeadlockApi, name: str, params: dict) -> list:
    try:
        return cached(BENCH / "raw" / f"{_key(name, params)}.json", lambda: api.analytics(name, **params),
                      max_age_s=DAY)
    except httpx.HTTPStatusError as e:
        # analytics endpoints occasionally fail server-side; keep what we have and move on
        log.warning("%s %s failed: %s", name, params, e.response.text[:200])
        return []


def sync_hero(api: DeadlockApi, hero_id: int, days: int = 30, min_badge: int | None = None,
              max_badge: int | None = None, builds: int = 30) -> None:
    w = _window(days, min_badge, max_badge)
    tag = {"hero_id": hero_id, "window_days": days, "window_start": w["min_unix_timestamp"],
           "min_badge": min_badge, "max_badge": max_badge}

    items = _fetch(api, "item-stats", {"hero_id": hero_id, **w})
    write_rows(BENCH / "item_stats" / f"{hero_id}.parquet", [
        {**tag, **{k: v for k, v in r.items() if k != "bucket"},
         "win_rate": r["wins"] / r["matches"] if r["matches"] else None}
        for r in items
    ])

    # resolution 0 = absolute game seconds (the default buckets by % of match length)
    curve = _fetch(api, "player-performance-curve", {"hero_ids": str(hero_id), "resolution": 0, **w})
    if curve:
        write_rows(BENCH / "performance_curve" / f"{hero_id}.parquet", [{**tag, **r} for r in curve])

    # skill order: which ability gets maxed first, and full upgrade orders with average timings
    max_first = _fetch(api, "ability-max-first-stats", {"hero_id": hero_id, **w})
    write_rows(BENCH / "ability_max_first" / f"{hero_id}.parquet", [
        {**tag, "ability_id": r["ability_id"], "wins": r["wins"], "matches": r["matches"],
         "win_rate": r["wins"] / r["matches"] if r["matches"] else None} for r in max_first
    ])
    orders = _fetch(api, "ability-order-stats", {"hero_id": hero_id, "min_matches": 50, **w})
    write_rows(BENCH / "ability_orders" / f"{hero_id}.parquet", [
        {**tag, "order_id": oi, "position": pi, "ability_id": a, "avg_time_s": t, "wins": o["wins"],
         "matches": o["matches"]}
        for oi, o in enumerate(orders)
        for pi, (a, t) in enumerate(zip(o["abilities"], o["avg_upgrade_times_s"]))
    ])

    top = cached(BENCH / "raw" / f"builds-{hero_id}.json",
                 lambda: api.builds(hero_id, limit=builds, sort_by="favorites", only_latest=True,
                                    min_unix_timestamp=w["min_unix_timestamp"] - 60 * DAY),
                 max_age_s=DAY)
    write_rows(BENCH / "build_items" / f"{hero_id}.parquet", [
        {"hero_id": hero_id, "hero_build_id": b["hero_build"]["hero_build_id"], "build_name": b["hero_build"]["name"],
         "favorites": b.get("num_favorites"), "last_updated": b["hero_build"]["last_updated_timestamp"],
         "category_index": ci, "category": cat.get("name"), "position": mi, "item_id": mod["ability_id"],
         "annotation": mod.get("annotation")}
        for b in top
        for ci, cat in enumerate((b["hero_build"].get("details") or {}).get("mod_categories") or [])
        for mi, mod in enumerate(cat.get("mods") or [])
    ])


def sync_global(api: DeadlockApi, days: int = 30, min_badge: int | None = None, max_badge: int | None = None):
    w = _window(days, min_badge, max_badge)
    tag = {"window_days": days, "window_start": w["min_unix_timestamp"], "min_badge": min_badge,
           "max_badge": max_badge}
    write_rows(BENCH / "hero_stats.parquet", [{**tag, **r} for r in _fetch(api, "hero-stats", w)])
    counters = _fetch(api, "hero-counter-stats", w)
    write_rows(BENCH / "hero_counters.parquet", [
        {**tag, **r, "win_rate": r["wins"] / r["matches_played"] if r["matches_played"] else None} for r in counters
    ])


def sync(hero_ids: set[int], **window) -> None:
    api = DeadlockApi()
    sync_global(api, **window)
    for h in sorted(hero_ids):
        sync_hero(api, h, **window)
