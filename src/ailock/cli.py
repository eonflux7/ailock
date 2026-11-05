"""ailock command line.

  ailock sync <steam id | vanity | profile url> [--limit N] [--demos]
  ailock match <match_id> [--demos]
  ailock knowledge [--all]
  ailock bench [--days 30] [--hero ID ...]
  ailock report <match_id> <player> [--json]
  ailock live <hero> -a <allies> -e <enemies> [-p partner -o lane opponents] [--json] [--no-open]
  ailock live-page [plan.json] [--no-open]
  ailock live-serve [--port 8765]
  ailock db
  ailock sql "<query>"
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

from . import benchmarks, db, ingest, lane, live, live_page, live_server, paths, report
from .sources import steam_gc
from .sources.deadlock_api import DeadlockApi

STEAM64_BASE = 76561197960265728


def resolve_account(ref: str) -> int:
    """Accept a 32-bit account id, SteamID64, vanity name or profile URL."""
    ref = ref.strip().rstrip("/")
    if m := re.search(r"steamcommunity\.com/(id|profiles)/([^/?#]+)", ref):
        ref = m.group(2)
    if ref.isdigit():
        n = int(ref)
        return n - STEAM64_BASE if n > STEAM64_BASE else n
    xml = httpx.get(f"https://steamcommunity.com/id/{ref}/?xml=1", timeout=30, follow_redirects=True).text
    m = re.search(r"<steamID64>(\d+)</steamID64>", xml)
    if not m:
        sys.exit(f"could not resolve steam vanity name {ref!r}")
    return int(m.group(1)) - STEAM64_BASE


def _report(results) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        mark = {"ingested": "+", "cached": "=", "no_metadata": "?", "rate_limited": "~", "error": "!"}[r.status]
        print(f" {mark} {r.match_id} {r.status}{' +demo' if r.demo else ''} {r.detail}", flush=True)
    print(" ".join(f"{k}={v}" for k, v in counts.items()))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ailock")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="ingest a player's match history")
    s.add_argument("player")
    s.add_argument("--limit", type=int)
    s.add_argument("--demos", action="store_true", help="also download demos Valve still has")
    s.add_argument("--no-knowledge", action="store_true")
    m = sub.add_parser("match", help="ingest specific matches")
    m.add_argument("match_ids", nargs="+", type=int)
    m.add_argument("--demos", action="store_true")
    k = sub.add_parser("knowledge", help="build versioned game data for ingested matches")
    k.add_argument("--all", action="store_true", help="every tracked client version (slow)")
    k.add_argument("--offline", action="store_true", help="don't fetch GameTracking updates")
    b = sub.add_parser("bench", help="sync population benchmarks (default: heroes in ingested matches)")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--hero", type=int, nargs="*")
    b.add_argument("--min-badge", type=int)
    b.add_argument("--max-badge", type=int)
    r = sub.add_parser("report", help="coaching facts for one player in one match")
    r.add_argument("match_id", type=int)
    r.add_argument("player")
    r.add_argument("--json", action="store_true")
    rp = sub.add_parser("replay", help="download replays on demand (fills positions for demo-only matches)")
    rp.add_argument("match_ids", type=int, nargs="+")
    sub.add_parser("steam-login", help="log in to Steam once (QR code) for full match history and replay salts")
    dm = sub.add_parser("demo", help="ingest local replay files (.dem) from their embedded match details")
    dm.add_argument("paths", nargs="+")
    ln = sub.add_parser("lane", help="lane phase facts: partner/opponents, economy, trades, positioning, deaths")
    ln.add_argument("match_id", type=int)
    ln.add_argument("player")
    ln.add_argument("--until", type=int, default=600, help="end of lane phase in game seconds (default 600)")
    ln.add_argument("--json", action="store_true")
    lv = sub.add_parser("live", help="pre-game brief: build, item corrections and skills vs a roster")
    lv.add_argument("hero")
    lv.add_argument("-a", "--allies", nargs="*", default=[], help="teammates (names or ids, spaces or commas)")
    lv.add_argument("-e", "--enemies", nargs="*", default=[])
    lv.add_argument("-p", "--partner", help="lane partner")
    lv.add_argument("-o", "--opponents", nargs="*", default=[], help="lane opponents")
    lv.add_argument("--days", type=int, default=30)
    lv.add_argument("--min-badge", type=int)
    lv.add_argument("--max-badge", type=int)
    lv.add_argument("--json", action="store_true")
    lv.add_argument("--no-open", action="store_true", help="serve the live page but don't open a browser")
    lp = sub.add_parser("live-page", help="render the last live brief + an agent plan as HTML and show it")
    lp.add_argument("plan", nargs="?", default=str(live_page.LIVE_DIR / "plan.json"))
    lp.add_argument("--no-open", action="store_true")
    ls = sub.add_parser("live-serve", help="serve the live brief on localhost with in-place reload and status")
    ls.add_argument("--host", default="127.0.0.1")
    ls.add_argument("--port", type=int, default=live_server.PORT)
    sub.add_parser("db", help="rebuild DuckDB views")
    q = sub.add_parser("sql", help="run a query against the DuckDB catalog")
    q.add_argument("query")
    a = ap.parse_args(argv)

    if a.cmd == "sync":
        account = resolve_account(a.player)
        print(f"account_id {account}")
        _report(ingest.sync_player(account, limit=a.limit, demos=a.demos))
        if not a.no_knowledge:
            print("knowledge versions:", ingest.sync_knowledge())
        db.build().close()
    elif a.cmd == "match":
        api = DeadlockApi()
        results = [ingest.fetch_match(api, mid, demos=a.demos) for mid in a.match_ids]
        missing = [r.match_id for r in results if r.status in ("no_metadata", "rate_limited")]
        if missing and steam_gc.logged_in():
            gc, limited = steam_gc.salts(missing)
            results = [ingest.fetch_match(api, r.match_id, demos=a.demos, salts=gc[r.match_id]) if r.match_id in gc
                       else ingest.MatchResult(r.match_id, "rate_limited", detail="Steam GC rate limited; retry later")
                       if r.match_id in limited else r for r in results]
        _report(results)
        db.build().close()
    elif a.cmd == "replay":
        api = DeadlockApi()
        _report(ingest.fetch_replay(api, mid) for mid in a.match_ids)
        db.build().close()
    elif a.cmd == "steam-login":
        subprocess.run(["node", str(steam_gc.STEAM_DIR / "gc.mjs"), "login"], check=True,
                       env={**os.environ, "AILOCK_DATA": str(paths.DATA)})
    elif a.cmd == "knowledge":
        print("knowledge versions:", ingest.sync_knowledge(update_repo=not a.offline, all_versions=a.all))
        db.build().close()
    elif a.cmd == "bench":
        heroes = set(a.hero or [r[0] for r in db.connect().sql("select distinct hero_id from players").fetchall()])
        benchmarks.sync(heroes, days=a.days, min_badge=a.min_badge, max_badge=a.max_badge)
        print(f"benchmarks synced for {len(heroes)} heroes")
    elif a.cmd == "report":
        facts = report.match_facts(a.match_id, resolve_account(a.player))
        print(json.dumps(facts, default=str, indent=1) if a.json else report.render_markdown(facts))
    elif a.cmd == "demo":
        _report(ingest.ingest_demo(p) for p in a.paths)
        print("knowledge versions:", ingest.sync_knowledge(update_repo=False))
        db.build().close()
    elif a.cmd == "lane":
        facts = lane.lane_facts(a.match_id, resolve_account(a.player), a.until)
        print(json.dumps(facts, default=str, indent=1) if a.json else lane.render_markdown(facts))
    elif a.cmd == "live":
        def split(refs):
            return [x for r in refs for x in r.split(",") if x.strip()]
        live_page.new_run()
        live_page.set_status("facts", f"Gathering facts for {a.hero}")
        url = live_server.show(open_it=not a.no_open)  # open now, so the page fills in while we work
        try:
            facts = live.brief(a.hero, split(a.allies), split(a.enemies), a.partner, split(a.opponents),
                               days=a.days, min_badge=a.min_badge, max_badge=a.max_badge)
        except ValueError as e:
            live_page.set_status("error", str(e).splitlines()[0])
            sys.exit(str(e))
        live_page.save_facts(facts)
        live_page.write({})  # rosters and popular builds while the agent decides
        live_page.set_status("deciding", "Agent is building the plan")
        print(json.dumps(facts, default=str, indent=1) if a.json else live.render_text(facts))
        print(f"\nlive page: {url}")
    elif a.cmd == "live-page":
        _, warnings = live_page.render(Path(a.plan))
        print(live_server.show(open_it=not a.no_open))
        for w in warnings:
            print("warning:", w)
    elif a.cmd == "live-serve":
        live_server.serve(a.host, a.port)
    elif a.cmd == "db":
        db.build().close()
    elif a.cmd == "sql":
        db.connect().sql(a.query).show(max_width=200, max_rows=200)


if __name__ == "__main__":
    main()
