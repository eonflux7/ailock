"""Client for the community deadlock-api (https://api.deadlock-api.com).

Used for things only the Game Coordinator knows: match lists and replay salts,
plus build-browser builds and aggregate analytics. Match metadata itself is
fetched from Valve's CDN (see valve.py) since it is the same data and faster.
"""
import json
import time
from pathlib import Path
from typing import Any

import httpx

BASE = "https://api.deadlock-api.com"


class RateLimited(Exception):
    """A quota that resets on a long period (e.g. salts lookups: 3/hour per IP); retrying now only wastes it."""

    def __init__(self, path: str, wait_s: int):
        super().__init__(f"{path}: rate limited, next request allowed in {wait_s // 60} min")
        self.wait_s = wait_s


class DeadlockApi:
    def __init__(self, timeout: float = 300.0, retries: int = 3):
        self.retries = retries
        self.http = httpx.Client(base_url=BASE, timeout=timeout, headers={"Accept-Encoding": "gzip"},
                                 follow_redirects=True)

    def get(self, path: str, retries: int | None = None, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(retries or self.retries):
            r = self.http.get(path, params=params)
            if r.status_code == 429:
                try:
                    wait = int(r.json()["error"]["next_request_in"])
                except (ValueError, KeyError, TypeError):
                    wait = 0
                if wait > 60:
                    raise RateLimited(path, wait)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt * 5)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def match_history(self, account_id: int) -> list[dict]:
        return self.get(f"/v1/players/{account_id}/match-history")

    def salts(self, match_id: int) -> dict:
        # matches deadlock-api hasn't seen cost a Steam lookup with a tiny hourly quota: don't retry
        return self.get(f"/v1/matches/{match_id}/salts", retries=1)

    def builds(self, hero_id: int, *, limit: int = 50, sort_by: str = "favorites", only_latest: bool = True,
               min_unix_timestamp: int | None = None) -> list[dict]:
        return self.get("/v1/builds", hero_id=hero_id, limit=limit, sort_by=sort_by, only_latest=only_latest,
                        min_unix_timestamp=min_unix_timestamp)

    def analytics(self, name: str, **params: Any) -> Any:
        return self.get(f"/v1/analytics/{name}", **params)


def cached(path: Path, fetch, max_age_s: float | None = None) -> Any:
    """Return JSON from `path` if fresh, else call fetch() and store it."""
    if path.exists() and (max_age_s is None or time.time() - path.stat().st_mtime < max_age_s):
        return json.loads(path.read_text())
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data
