"""Pre-game brief: my hero + both rosters -> build, item corrections, skill order and matchup facts.

Built for speed while a match loads: every deadlock-api call runs in parallel and is cached for the day
(data/benchmarks/raw/live/), and game knowledge is read straight from the newest build's parquet with an
in-memory DuckDB (no catalog lock). Facts are plain dicts; render_text() keeps them short.

Roster-adjusted item deltas: for each enemy, an item's win rate in games against that enemy relative to
the hero's win rate against that enemy, minus the same gap over all games, averaged over the enemies.
Positive = the item does better than usual into this roster. Buyer skill and late-buy bias show up in
both terms and mostly cancel.
"""
import datetime
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import duckdb

from . import paths
from .benchmarks import BENCH, DAY, _key, _window
from .report import COUNTER_ITEMS, _mmss, _rows
from .sources.deadlock_api import DeadlockApi, cached

LIVE = BENCH / "raw" / "live"
SIGNATURES = ("Signature_1", "Signature_2", "Signature_3", "Signature_4")
ALIASES = {"mo": "Mo & Krill", "krill": "Mo & Krill", "geist": "Lady Geist", "ld": "Lady Geist",
           "talon": "Grey Talon", "gt": "Grey Talon", "danny": "Deadman Danny", "nurse": "Nurse Harrow",
           "harrow": "Nurse Harrow", "rat": "Rat King", "mcg": "McGinnis", "door": "The Doorman"}
# ability properties that mean hard CC on an enemy (value = duration in seconds)
CC = re.compile(r"^(Stun|Silence|Sleep|Immobilize|Disarm|Root|Hex|Petrify|Freeze|Fear|Charm|Toss|Pull|Tether|"
                r"Knockdown|Bound|Suppress|Lift|Chain|Throw\w*)Duration$")
CC_LABEL = re.compile(r"Time-Stop|Time Stop|Stun Duration|Silence Duration|Sleep Duration|Disarm Duration|"
                      r"Root Duration|Immobilize Duration", re.I)
CC_TEXT = re.compile(r"\b(stun\w*|silenc\w*|disarm\w*|sleeps?|immobiliz\w*|roots?|time-stop|hold the target in place|"
                     r"swaps? your position|pulls?|lift an enemy|knock(s|ed)? (up|back))\b", re.I)
HEAL = re.compile(r"HealAmount|HealPerSecond|Lifesteal|LifeSteal|HealOnHit|BonusHealthRegen|HealthRegen$")
SLOT_KIND = {"WeaponMod": "weapon", "Tech": "spirit", "Armor": "vitality"}
TOP_BUILDS = 12  # most played builds to fetch and rank
MIN_N = 200  # games for an item's win rate to count
Z_ITEM, Z_PAIR = 2.0, 2.5  # significance to flag an item's roster delta / a single item-vs-enemy delta
MIN_DELTA = 0.005  # and big enough to matter (win-rate fraction)
CANDIDATES = 10  # builds shown for the agent to choose from
COUNTER_PICK = 0.10  # a roster hero "buys" an anti-heal/cleanse item at this pick rate
GLOSS_LEN = 320
NOTE_LEN = 120  # build author's per-item annotation
# 12 item slots, any mix of weapon/spirit/vitality (checked against held items in match data: players top out
# at 12 with up to 11 of one kind). An upgrade takes its component's slot and costs the difference.
SLOTS = 12
FLOW_COLUMNS = ["0-9m", "9-20m", "20-30m", "30m+"]  # item-flow-stats' fixed phases
FLOW_PICK = 0.04  # share of the players reaching a phase who bought the item in it
FLOW_MIN = 300  # buys for an item's phase win rate to count
FLOW_TOP = 18  # items listed per phase
# tooltip plumbing, not worth a glossary slot
GLOSS_SKIP = {"AbilityCooldownBetweenCharge", "AbilityCastDelay", "AbilityCharges", "AbilityPostCastDuration",
              "AbilityChannelTime", "AbilityUnitTargetLimit", "AbilityResourceCost", "ChannelMoveSpeed"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve_hero(ref: str, heroes: dict[int, str], fallback: dict[int, str] | None = None) -> int:
    """Hero id from an id, name, alias or unambiguous prefix/substring (case and punctuation ignored).
    Names not found in `heroes` are looked up in `fallback`."""
    ref = ref.strip()
    if ref.isdigit() and int(ref) in heroes:
        return int(ref)
    n = _norm(ALIASES.get(_norm(ref), ref))
    names = {h: _norm(name) for h, name in heroes.items()}
    for test in (lambda v: v == n, lambda v: v.startswith(n), lambda v: n in v):
        hits = [h for h, v in names.items() if test(v)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ValueError(f"{ref!r} is ambiguous: {', '.join(heroes[h] for h in hits)}")
    if fallback:
        return resolve_hero(ref, fallback)
    raise ValueError(f"unknown hero {ref!r}")


def knowledge() -> tuple[duckdb.DuckDBPyConnection, dict]:
    """In-memory views over the newest knowledge snapshot, plus its raw abilities JSON (for upgrades)."""
    cv = max(int(p.name) for p in paths.KNOWLEDGE.iterdir() if (p / "abilities.parquet").exists())
    con = duckdb.connect()
    for t in ("heroes", "hero_abilities", "abilities", "ability_properties"):
        con.execute(f"create view {t} as select * from read_parquet('{paths.KNOWLEDGE / str(cv) / t}.parquet')")
    released = con.execute(f"select strftime(released_at, '%Y-%m-%d') from "
                           f"read_parquet('{paths.KNOWLEDGE}/versions.parquet') where client_version = ?",
                           [cv]).fetchone()
    raw = json.loads((paths.KNOWLEDGE / str(cv) / "abilities.json").read_text())
    return con, {"client_version": cv, "released_at": released[0] if released else None, "abilities": raw}


def _wr(w, m):
    return w / m if m else None


def _wilson_lb(w: int, m: int, z: float = 1.96) -> float:
    if not m:
        return 0.0
    p = w / m
    return (p + z * z / (2 * m) - z * math.sqrt(p * (1 - p) / m + z * z / (4 * m * m))) / (1 + z * z / m)


def fetch_all(hero: int, allies: list[int], enemies: list[int], partner: int | None, opponents: list[int],
              days: int, min_badge: int | None, max_badge: int | None) -> tuple[dict, list[str]]:
    """All analytics in parallel, cached per day. Failed calls come back as None with an error noted."""
    api = DeadlockApi(timeout=60, retries=2)
    w = _window(days, min_badge, max_badge)
    calls: dict[str, tuple[str, dict]] = {
        "hero_stats": ("hero-stats", w),
        "counters": ("hero-counter-stats", w),
        "synergy": ("hero-synergy-stats", w),
        "items": ("item-stats", {"hero_id": hero, **w}),
        "roster_items": ("item-stats", {"hero_ids": ",".join(map(str, [hero, *allies, *enemies])),
                                        "bucket": "hero", **w}),
        "max_first": ("ability-max-first-stats", {"hero_id": hero, **w}),
        "orders": ("ability-order-stats", {"hero_id": hero, "min_matches": 50, **w}),
        "build_stats": (f"hero-build-stats/{hero}", w),
        "curve": ("player-performance-curve", {"hero_ids": str(hero), "resolution": 0, **w}),
        # per-phase item win rates standardized for the buyer's net worth, plus who reaches each phase
        "flow": ("item-flow-stats", {"hero_ids": str(hero), **w}),
        **{f"vs_{e}": ("item-stats", {"hero_id": hero, "enemy_hero_ids": str(e), **w}) for e in enemies},
    }
    if opponents:
        calls["lane"] = ("lane-matchup-stats", {"hero_ids": ",".join(map(str, [hero] + ([partner] if partner else []))),
                                                "enemy_hero_ids": ",".join(map(str, opponents)), **w})
        # exact 2v2 lanes are often rare: also my hero against each opponent
        calls |= {f"lane_vs_{o}": ("lane-matchup-stats", {"hero_ids": str(hero), "enemy_hero_ids": str(o), **w})
                  for o in opponents}
    errors: list[str] = []

    def run(item):
        key, (name, params) = item
        try:
            return key, cached(LIVE / f"{_key(name.replace('/', '-'), params)}.json",
                               lambda: api.analytics(name, **params), max_age_s=DAY)
        except Exception as e:  # noqa: BLE001 - a missing section beats no brief
            errors.append(f"{key}: {type(e).__name__} {str(e)[:120]}")
            return key, None

    def build(build_id: int):
        # one build id can come back for several heroes: keep this hero's
        try:
            rows = cached(LIVE / f"build-{build_id}.json",
                          lambda: api.get("/v1/builds", build_id=build_id, only_latest="true"), max_age_s=DAY)
        except Exception as e:  # noqa: BLE001
            errors.append(f"build {build_id}: {type(e).__name__} {str(e)[:120]}")
            return None
        return next((b["hero_build"] for b in rows if b["hero_build"]["hero_id"] == hero), None)

    def icons():
        # the asset index is ~8 MB: keep only id -> icon url, refreshed weekly. Shop items use their shop icon
        # (every shop item has one, some have nothing else); abilities only have image*. Unreleased heroes have
        # no images and are left out.
        def compact():
            items = {str(a["id"]): next((a[k] for k in ("shop_image_webp", "shop_image", "image_webp", "image")
                                         if a.get(k)), None) for a in api.get("/v1/assets/items")}
            heroes = {str(h["id"]): next((im[k] for k in ("icon_image_small_webp", "icon_image_small") if im.get(k)),
                                         None) for h in api.get("/v1/assets/heroes") for im in [h.get("images") or {}]}
            return {"items": {k: v for k, v in items.items() if v}, "heroes": {k: v for k, v in heroes.items() if v}}
        try:
            return "icons", cached(LIVE / "icons-v3.json", compact, max_age_s=7 * DAY)
        except Exception as e:  # noqa: BLE001
            errors.append(f"icons: {type(e).__name__} {str(e)[:120]}")
            return "icons", None

    with ThreadPoolExecutor(max_workers=12) as pool:
        icon_job = pool.submit(icons)
        data = dict(pool.map(run, calls.items()))
        # the most played builds (what people actually run), not the most favorited
        top = sorted(data.get("build_stats") or [], key=lambda r: -r["matches"])[:TOP_BUILDS]
        data["builds"] = [b for b in pool.map(build, [r["hero_build_id"] for r in top]) if b]
        data["icons"] = icon_job.result()[1]
    return data, errors


def brief(hero_ref: str, ally_refs: list[str], enemy_refs: list[str], partner_ref: str | None = None,
          opponent_refs: list[str] | None = None, days: int = 30, min_badge: int | None = None,
          max_badge: int | None = None) -> dict[str, Any]:
    con, kn = knowledge()
    # the game files keep `in_development` on some heroes after release (Violet in 6819 with 500k+ matches):
    # names match released heroes first, then those
    rows = con.execute("select hero_id, name, in_development from heroes where name is not null and not disabled"
                       ).fetchall()
    heroes = {h: name for h, name, _ in rows}
    released = {h: name for h, name, dev in rows if not dev}

    def find(ref: str) -> int:
        return resolve_hero(ref, released, heroes)

    hero = find(hero_ref)
    allies = [a for a in map(find, ally_refs) if a != hero]
    enemies = [find(r) for r in enemy_refs]
    partner = find(partner_ref) if partner_ref else None
    opponents = [find(r) for r in opponent_refs or []]
    d, errors = fetch_all(hero, allies, enemies, partner, opponents, days, min_badge, max_badge)

    items = {r["ability_id"]: r for r in _rows(con, """
        select ability_id, name, class_name, slot, tier, cost, component_items from abilities
        where type = 'Item' and shoppable and not coalesce(disabled, false)""")}
    by_class = {r["class_name"]: i for i, r in items.items()}
    for r in items.values():
        r["components"] = [by_class[c] for c in r["component_items"] or [] if c in by_class]
    into: dict[int, list[int]] = {}
    for i, r in items.items():
        for c in r["components"]:
            into.setdefault(c, []).append(i)
    counter_cat = {r["ability_id"]: r["category"] for r in _rows(con, COUNTER_ITEMS)}

    hero_rows = {r["hero_id"]: r for r in d.get("hero_stats") or []}
    by_wr = sorted(hero_rows, key=lambda h: -_wr(hero_rows[h]["wins"], hero_rows[h]["matches"]))
    hs = hero_rows.get(hero, {})
    hero_wr = _wr(hs.get("wins", 0), hs.get("matches", 0))
    counters = {(r["hero_id"], r["enemy_hero_id"]): r for r in d.get("counters") or []}
    synergy = {(r["hero_id1"], r["hero_id2"]): r for r in d.get("synergy") or []}

    facts: dict[str, Any] = {
        "version": {"client_version": kn["client_version"], "released_at": kn["released_at"]},
        "window": {"days": days, "min_badge": min_badge, "max_badge": max_badge},
        "hero": {"hero_id": hero, "name": heroes[hero], "win_rate": hero_wr, "matches": hs.get("matches"),
                 "wr_rank": (by_wr.index(hero) + 1, len(by_wr)) if hero in by_wr else None},
        "errors": errors,
    }

    # --- each roster hero: kit (CC, heals, stealth, ult), item spend split, items they usually buy
    kit = _kits(con, kn["abilities"], [*allies, *enemies])
    lean = _item_lean(d.get("roster_items") or [], items, hero_rows, counter_cat)

    def hero_line(h: int, enemy: bool) -> dict:
        row = {"hero_id": h, "name": heroes[h], **kit.get(h, {}), **lean.get(h, {})}
        if enemy:
            c = counters.get((hero, h))
            row["my_wr_vs"] = _wr(c["wins"], c["matches_played"]) if c else None
            row["their_wr"] = _wr(hero_rows[h]["wins"], hero_rows[h]["matches"]) if h in hero_rows else None
        else:
            c = synergy.get((min(hero, h), max(hero, h)))
            row["wr_with"] = _wr(c["wins"], c["matches_played"]) if c else None
        key = "my_wr_vs" if enemy else "wr_with"
        row["shift"] = row[key] - hero_wr if row[key] is not None and hero_wr is not None else None
        return row

    facts["enemies"] = [hero_line(e, True) for e in enemies]
    facts["allies"] = [hero_line(a, False) for a in allies]
    for side in ("enemies", "allies"):
        rows = [r for r in facts[side] if r.get("spend")]
        facts[f"{side}_damage_mix"] = {k: sum(r["spend"][k] for r in rows) / len(rows)
                                       for k in ("weapon", "spirit", "vitality")} if rows else None

    # --- items: baseline + roster-adjusted delta
    base = {r["item_id"]: r for r in d.get("items") or [] if r["item_id"] in items}
    # (gap, enemy, standard error); the error of the vs-enemy item win rate dominates (smallest sample)
    deltas: dict[int, list[tuple[float, int, float]]] = {}
    for e in enemies:
        c, vs = counters.get((hero, e)), d.get(f"vs_{e}")
        if not c or not vs or hero_wr is None:
            continue
        wr_vs = _wr(c["wins"], c["matches_played"])
        gaps = []
        for r in vs:
            b = base.get(r["item_id"])
            if b and r["matches"] >= MIN_N and b["matches"] >= MIN_N:
                p = _wr(r["wins"], r["matches"])
                gap = (p - wr_vs) - (_wr(b["wins"], b["matches"]) - hero_wr)
                gaps.append((r["item_id"], gap, math.sqrt(p * (1 - p) / r["matches"])))
        # hero-stats, counter-stats and item-stats don't share a baseline exactly, which shifts every item the
        # same way: center on the median item so Δ reads "vs this hero's other items into this enemy"
        mid = sorted(g for _, g, _ in gaps)[len(gaps) // 2] if gaps else 0
        for i, gap, se in gaps:
            deltas.setdefault(i, []).append((gap - mid, e, se))
    hero_matches = hs.get("matches") or max((b["matches"] for b in base.values()), default=1)

    def item_row(i: int) -> dict:
        b, ds = base.get(i, {}), deltas.get(i, [])
        best, worst = max(ds, default=None), min(ds, default=None)
        mean = sum(g for g, _, _ in ds) / len(ds) if ds else None
        se = math.sqrt(sum(s * s for _, _, s in ds)) / len(ds) if ds else None
        return {"item_id": i, "name": items[i]["name"], "kind": SLOT_KIND.get(items[i]["slot"]),
                "tier": items[i]["tier"], "cost": items[i]["cost"],
                "pick": b.get("matches", 0) / hero_matches, "wr": _wr(b.get("wins", 0), b.get("matches", 0)),
                "buy_s": b.get("avg_buy_time_s"), "sold": _wr(b.get("sold", 0), b.get("matches", 0)),
                "sell_s": b.get("avg_sell_time_s"), "roster_delta": mean,
                "roster_z": mean / se if ds and se else None,
                "best_vs": (heroes[best[1]], best[0]) if best else None,
                "worst_vs": (heroes[worst[1]], worst[0]) if worst else None,
                "counter": counter_cat.get(i)}

    # --- candidate builds: the most played builds, ranked by win-rate lower bound (English first). The agent
    # picks one against the roster using each build's profile, its items' roster deltas and the glossary.
    stats = {r["hero_build_id"]: r for r in d.get("build_stats") or []}
    ranked = sorted(d.get("builds") or [], key=lambda b: (b.get("language", 0) != 0, -_wilson_lb(
        stats[b["hero_build_id"]]["wins"], stats[b["hero_build_id"]]["matches"])))
    facts["builds"] = []
    in_builds: dict[int, list[str]] = {}  # item -> candidate tags (B1..)
    ability_names = {v["id"]: v["name"] for v in kn["abilities"].values() if v.get("id") and v.get("name")}
    for k, b in enumerate(ranked[:CANDIDATES]):
        tag, s = f"B{k + 1}", stats[b["hero_build_id"]]
        cats, ids = [], []
        # the author's own sections, in order; `optional` marks situational ones, annotations are their notes
        for cat in (b.get("details") or {}).get("mod_categories") or []:
            mods = [m for m in cat.get("mods") or [] if m["ability_id"] in items]
            if mods:
                cats.append({"category": re.sub(r"^#Citadel_HeroBuilds_", "", cat.get("name") or "-"),
                             "optional": bool(cat.get("optional")),
                             "description": _short(cat.get("description"), NOTE_LEN),
                             "items": [{**item_row(m["ability_id"]), "note": _short(m.get("annotation"), NOTE_LEN),
                                        "imbue": ability_names.get(m.get("imbue_target_ability_id"))}
                                       for m in mods]})
                ids += [m["ability_id"] for m in mods]
        for i in dict.fromkeys(ids):
            in_builds.setdefault(i, []).append(tag)
        rows = [item_row(i) for i in dict.fromkeys(ids)]
        spend = {kind: sum(r["cost"] or 0 for r in rows if r["kind"] == kind) for kind in ("weapon", "spirit", "vitality")}
        facts["builds"].append({
            "tag": tag, "hero_build_id": b["hero_build_id"], "name": b["name"], "language": b.get("language", 0),
            "win_rate": _wr(s["wins"], s["matches"]), "matches": s["matches"],
            "updated": b.get("last_updated_timestamp"),
            "spend": {kd: v / (sum(spend.values()) or 1) for kd, v in spend.items()},
            "counters": sorted({r["counter"] for r in rows if r["counter"]}), "categories": cats})

    # --- corrections against the roster (only items with real usage on this hero)
    scored = [item_row(i) for i in base if base[i]["matches"] / hero_matches >= 0.03]
    for r in scored:
        r["in_builds"] = in_builds.get(r["item_id"], [])
    sig = [r for r in scored if _significant(r)]
    facts["add"] = sorted([r for r in sig if r["roster_delta"] > 0], key=lambda r: -r["roster_delta"])[:8]
    facts["cut"] = sorted([r for r in sig if r["roster_delta"] < 0], key=lambda r: r["roster_delta"])[:6]
    facts["standouts"] = sorted([
        {"item": items[i]["name"], "enemy": heroes[e], "delta": g, "z": g / s, "in_builds": in_builds.get(i, [])}
        for i, ds in deltas.items() if base[i]["matches"] / hero_matches >= 0.03
        for g, e, s in ds if s and abs(g / s) >= Z_PAIR and abs(g) >= MIN_DELTA], key=lambda r: -abs(r["delta"]))[:8]
    # item pool by phase for the agent's own build. Raw item win rates favor late items (only long games get
    # there, and richer players buy sooner): item-flow-stats standardizes each item's win rate to the phase's
    # net worth at buy, and we compare it with the win rate of everyone who bought anything in that phase.
    flow = d.get("flow") or {}
    reached, reached_w = flow.get("summary_reached_per_column") or [], flow.get("summary_reached_wins_per_column") or []
    games = (flow.get("summary") or {}).get("matches") or 0
    facts["phases"] = []
    for c, label in enumerate(FLOW_COLUMNS):
        if c >= len(reached) or not reached[c]:
            continue
        base_wr = reached_w[c] / reached[c]
        rows = []
        for n in flow.get("nodes") or []:
            if n["column"] == c and n["item_id"] in items and n["matches"] >= FLOW_MIN \
                    and n["matches"] / reached[c] >= FLOW_PICK:
                rows.append({**item_row(n["item_id"]), "phase_pick": n["matches"] / reached[c],
                             "phase_adj": n["adjusted_win_rate"] - base_wr, "nw_at_buy": n["avg_net_worth_at_buy"]})
        facts["phases"].append({"phase": label, "reached": reached[c] / games if games else None, "win_rate": base_wr,
                                "items": sorted(rows, key=lambda r: -r["phase_pick"])[:FLOW_TOP]})
    for r in [r for p in facts["phases"] for r in p["items"]]:
        r["in_builds"] = in_builds.get(r["item_id"], [])
    facts["counter_items"] = sorted([item_row(i) for i in counter_cat if i in base],
                                    key=lambda r: (r["counter"], r["cost"] or 0))
    mix = facts["enemies_damage_mix"]
    resist = "TechResist" if mix and mix["spirit"] >= mix["weapon"] else "BulletResist"
    facts["resist_kind"] = "spirit" if resist == "TechResist" else "bullet"
    facts["resist_items"] = sorted([{**item_row(r["ability_id"]), "resist": r["value_num"]} for r in _rows(con, """
        select p.ability_id, max(p.value_num) as value_num from ability_properties p
        join abilities a using (ability_id)
        where a.type = 'Item' and a.shoppable and p.property = ? and p.value_num > 0
          and p.section in ('Innate', 'Passive') and not coalesce(p.negative, false) group by 1""", resist)
        if r["ability_id"] in base], key=lambda r: (r["cost"] or 0, -(r["resist"] or 0)))[:8]

    # what the items worth considering do, from this build's own data (niche build items are left out)
    mentioned = {r["item_id"] for p in facts["phases"] for r in p["items"]}
    mentioned |= {r["item_id"] for r in facts["counter_items"] + facts["resist_items"] + facts["add"] + facts["cut"]}
    # components of anything mentioned, so upgrade paths read end to end
    mentioned |= {c for i in list(mentioned) for c in items[i]["components"]}
    facts["glossary"] = _glossary(con, mentioned)
    for g in facts["glossary"]:
        g["from"] = [items[c]["name"] for c in items[g["item_id"]]["components"]]
        g["into"] = [items[u]["name"] for u in into.get(g["item_id"], [])]
    # for the HTML brief (not rendered as text): every shop item by name, and icon urls
    facts["catalog"] = {it["name"]: {"item_id": i, "cost": it["cost"], "kind": SLOT_KIND.get(it["slot"]),
                                     "buy_s": (base.get(i) or {}).get("avg_buy_time_s"),
                                     "components": [items[c]["name"] for c in it["components"]]}
                        for i, it in items.items() if it["name"]}
    facts["icons"] = d.get("icons") or {"items": {}, "heroes": {}}

    facts["skills"] = _skills(con, kn["abilities"], hero, d.get("max_first") or [], d.get("orders") or [])

    # --- tempo: average souls over time, how long games run and the lane matchup
    curve = {r["game_time"]: r for r in d.get("curve") or []}
    facts["nw_targets"] = {t: round(curve[min(curve, key=lambda g: abs(g - t))]["net_worth_avg"])
                           for t in (300, 600, 900, 1200, 1500, 1800, 2100, 2400, 2700)
                           if curve and t <= max(curve) + 60} if curve else None
    summary = flow.get("summary") or {}
    facts["games"] = {"avg_duration_s": summary.get("avg_duration_s"), "avg_end_souls": summary.get("avg_net_worth"),
                      "reach": {p["phase"]: p["reached"] for p in facts["phases"]}} if summary else None
    def lane_sum(rows: list[dict]) -> dict:
        n = sum(r["sample_matches"] for r in rows)
        m = sum(r["matches_played"] for r in rows)
        return {"matches": m, "win_rate": _wr(sum(r["wins"] for r in rows), m),
                "nw_diff_15": sum(r["net_worth_diff"] * r["sample_matches"] for r in rows) / n if n else None}

    facts["lane"] = {"partner": heroes.get(partner), "opponents": [heroes[o] for o in opponents],
                     **lane_sum(d.get("lane") or []),
                     "vs_each": [{"opponent": heroes[o], **lane_sum(d.get(f"lane_vs_{o}") or [])} for o in opponents],
                     } if opponents else None
    return facts


def _kits(con: duckdb.DuckDBPyConnection, raw: dict, hero_ids: list[int]) -> dict[int, dict]:
    """Per hero: quick tags (hard CC with duration, heals, stealth, big slows, ult cooldown) and every ability's
    own description and numbers from this game build, so CC the tags miss (e.g. a channel that holds the
    target, a time-stop) is still in front of the agent."""
    if not hero_ids:
        return {}
    rows = _rows(con, f"""
        select h.hero_id, a.name as ability, a.type, p.property, p.label, p.value_num
        from hero_abilities h join abilities a using (ability_id)
        join ability_properties p using (ability_id)
        where h.hero_id in ({",".join(map(str, hero_ids))}) and h.slot in {SIGNATURES}
          and p.value_num is not null and p.value_num != 0""")
    out: dict[int, dict] = {h: {"cc": [], "heals": [], "stealth": [], "slow": [], "ult": None, "kit": []}
                            for h in hero_ids}
    for r in rows:
        k = out[r["hero_id"]]
        if r["type"] == "Ultimate" and r["property"] == "AbilityCooldown":
            k["ult"] = f"{r['ability']} ({r['value_num']:g}s cd)"
        if (CC.match(r["property"]) or CC_LABEL.search(r["label"] or "")) and r["value_num"] > 0 \
                and not (r["label"] or "").startswith("Min "):
            what = r["label"] or r["property"][:-8]
            k["cc"].append(f"{r['ability']}: {what.lower()} {r['value_num']:g}s")
        elif HEAL.search(r["property"]) and r["value_num"] > 0 and r["ability"] not in k["heals"]:
            k["heals"].append(r["ability"])
        elif r["property"].startswith("Invis") and r["ability"] not in k["stealth"]:
            k["stealth"].append(r["ability"])
        elif r["property"] in ("SlowPercent", "MoveSlowPercent") and r["value_num"] >= 25 \
                and r["ability"] not in k["slow"]:
            k["slow"].append(r["ability"])
    for r in _rows(con, f"""select h.hero_id, h.slot, a.class_name from hero_abilities h join abilities a using (ability_id)
                            where h.hero_id in ({",".join(map(str, hero_ids))}) and h.slot in {SIGNATURES}
                            order by h.hero_id, h.slot"""):
        if ab := raw.get(r["class_name"]):
            out[r["hero_id"]]["kit"].append(_ability_facts(ab, SIGNATURES.index(r["slot"]) + 1))
    for k in out.values():
        # abilities whose text says they disable but carry no duration property
        tagged = {c.split(":")[0] for c in k["cc"]}
        k["cc"] += [f"{a['name']}: {m.group(0).lower()} (text)" for a in k["kit"] if a["name"] not in tagged
                    and (m := CC_TEXT.search(a["text"]))]
    return out


def _scale(p: dict) -> float | None:
    """Spirit power scaling of an ability property (value per point of spirit power), if any."""
    s = p.get("scale") or ""
    m = re.search(r"'m_flStatScale': ([-\d.e]+)", s)
    return float(m.group(1)) if m and "ETechPower" in s and float(m.group(1)) else None


def _ability_facts(ab: dict, slot: int) -> dict:
    """An ability's description (placeholders filled) and its labeled numbers with spirit scaling."""
    props = ab.get("properties") or {}
    values = {k: str(p.get("value")) for k, p in props.items()}
    nums = []
    for k, p in props.items():
        try:
            v = float(re.sub(r"m$", "", str(p.get("value"))))
        except ValueError:
            continue
        if not p.get("label") or k in GLOSS_SKIP or k == "AbilityCastDelay" or v in (0, -1):
            continue
        sc = _scale(p)
        nums.append(f"{p['label']} {p['value']}" + (f" (+{sc:g}/spirit)" if sc else ""))
    return {"slot": slot, "name": ab.get("name"), "ult": ab.get("type") == "Ultimate",
            "text": _clean_description(ab.get("description"), values), "numbers": list(dict.fromkeys(nums)),
            "upgrades": _upgrade_text(ab)}


def _short(s: str | None, n: int) -> str | None:
    s = re.sub(r"\s+", " ", s or "").strip()
    return None if not s else s if len(s) <= n else s[:n - 1] + "…"


def _item_lean(rows: list[dict], items: dict, hero_rows: dict, counter_cat: dict) -> dict[int, dict]:
    """Per roster hero: share of item souls by category, their usual core items (by souls spent) with buy
    times, and the anti-heal/cleanse items they commonly buy."""
    by_hero: dict[int, list[dict]] = {}
    for r in rows:
        if r["item_id"] in items:
            by_hero.setdefault(r["bucket"], []).append(r)
    out = {}
    for h, rs in by_hero.items():
        spent = {"weapon": 0.0, "spirit": 0.0, "vitality": 0.0}
        for r in rs:
            if kind := SLOT_KIND.get(items[r["item_id"]]["slot"]):
                spent[kind] += (items[r["item_id"]]["cost"] or 0) * r["matches"]
        tot = sum(spent.values()) or 1
        n = hero_rows.get(h, {}).get("matches") or max(r["matches"] for r in rs)
        core = sorted(rs, key=lambda r: -(items[r["item_id"]]["cost"] or 0) * r["matches"])[:6]
        out[h] = {"spend": {k: v / tot for k, v in spent.items()},
                  "core": [f"{items[r['item_id']]['name']} {r['matches'] / n:.0%}@{_mmss(r.get('avg_buy_time_s'))}"
                           for r in sorted(core, key=lambda r: r.get("avg_buy_time_s") or 0)],
                  "counters": [f"{items[r['item_id']]['name']} ({counter_cat[r['item_id']]}) {r['matches'] / n:.0%}"
                               for r in sorted(rs, key=lambda r: -r["matches"])
                               if r["item_id"] in counter_cat and r["matches"] / n >= COUNTER_PICK]}
    return out


def _clean_description(text: str, values: dict[str, str]) -> str:
    """Tooltip text with its {s:Property} placeholders filled and {g:...:'Attribute'} tags reduced to words."""
    text = re.sub(r"\{s:(\w+)\}", lambda m: values.get(m.group(1), m.group(1)), text or "")
    text = re.sub(r"\{g:[^}]*'(\w+)'\}", lambda m: re.sub(r"(?<!^)(?=[A-Z])", " ", m.group(1)), text)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>|\{[^}]*\}", "", text)).strip()


def _glossary(con: duckdb.DuckDBPyConnection, ids: set[int]) -> list[dict]:
    """One line per item: always-on stats, then the effect's numbers (on target / active), then its text."""
    if not ids:
        return []
    props = _rows(con, f"""
        select ability_id, property, value, label, section, usage, coalesce(negative, false) as negative
        from ability_properties where ability_id in ({",".join(map(str, ids))})
          and value_num is not null and value_num != 0 and value_num != -1""")
    by_item: dict[int, list[dict]] = {}
    for p in props:
        by_item.setdefault(p["ability_id"], []).append(p)
    out = []
    for a in _rows(con, f"""select ability_id, name, slot, tier, cost, description from abilities
                            where ability_id in ({",".join(map(str, ids))}) order by cost, name"""):
        ps = by_item.get(a["ability_id"], [])
        values = {p["property"]: p["value"] for p in ps}

        def fmt(p, bonus):
            v = p["value"] if not bonus or p["value"].startswith("-") else "+" + p["value"]
            return f"{p['label']} {v}" + (" (drawback)" if p["negative"] else "")

        # Innate rows are stat bonuses (+); effect rows mix bonuses with plain numbers (cooldown, duration, radius)
        innate = list(dict.fromkeys(fmt(p, True) for p in ps if p["section"] == "Innate" and p["label"]))
        effect = list(dict.fromkeys(fmt(p, False) + (" (cond)" if p["usage"] == "ConditionallyApplied" else "")
                                    for p in ps if p["section"] != "Innate" and p["label"]
                                    and p["property"] not in GLOSS_SKIP))
        active = any(p["section"] == "Active" for p in ps)
        text = _clean_description(a["description"], values)
        out.append({"item_id": a["ability_id"], "name": a["name"], "kind": SLOT_KIND.get(a["slot"]),
                    "tier": a["tier"], "cost": a["cost"], "active": active, "innate": innate, "effect": effect,
                    "text": text if len(text) <= GLOSS_LEN else text[:GLOSS_LEN - 1] + "…"})
    return out


def _upgrade_text(ab: dict) -> list[str]:
    props = ab.get("properties") or {}
    tiers = []
    for tier in ab.get("upgrades") or []:
        parts = []
        for u in tier:
            if not u.get("property"):
                continue  # some upgrades (e.g. new heroes) carry only a script effect, no property
            label = (props.get(u["property"]) or {}).get("label") or _words(u["property"])
            bonus = str(u["bonus"])
            bonus = bonus if bonus.startswith("-") else "+" + bonus
            if u.get("type") == "EAddToScale":
                parts.append(f"{label} {bonus}/spirit")  # adds to the spirit power scaling, not the base
            elif u.get("type") == "EMultiplyScale":
                parts.append(f"{label} spirit scaling x{u['bonus']}")
            else:
                parts.append(f"{label} {bonus}")
        tiers.append(", ".join(parts))
    return tiers


def _words(prop: str) -> str:
    """BulletResistReductionDuration -> Bullet Resist Reduction Duration"""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", prop)


def _skills(con, raw: dict, hero: int, max_first: list[dict], orders: list[dict]) -> dict:
    abil = _rows(con, f"""select slot, ability_id, ability_name, ability_class from hero_abilities
                          where hero_id = ? and slot in {SIGNATURES} order by slot""", hero)
    names = {a["ability_id"]: a["ability_name"] for a in abil}
    out: dict[str, Any] = {"abilities": [{**_ability_facts(raw.get(a["ability_class"]) or {}, i + 1),
                                          "name": a["ability_name"], "ability_id": a["ability_id"]}
                                         for i, a in enumerate(abil)]}
    n = sum(r["matches"] for r in max_first) or 1
    out["max_first"] = sorted([{"ability": names[r["ability_id"]], "share": r["matches"] / n,
                                "win_rate": _wr(r["wins"], r["matches"])}
                               for r in max_first if r["ability_id"] in names], key=lambda r: -r["share"])

    def summarize(o: dict) -> dict:
        seen: dict[int, int] = {}
        unlock, maxed, t3 = [], [], []
        for a, t in zip(o["abilities"], o["avg_upgrade_times_s"]):
            seen[a] = seen.get(a, 0) + 1
            if seen[a] == 1:
                unlock.append(names.get(a, str(a)))
            if seen[a] == 4:
                maxed.append(names.get(a, str(a)))
                t3.append(_mmss(t))
        return {"unlock": unlock, "max": maxed, "t3_at": t3, "matches": o["matches"],
                "win_rate": _wr(o["wins"], o["matches"]),
                "sequence": [{"ability": names.get(a, str(a)), "t": _mmss(t)}
                             for a, t in zip(o["abilities"], o["avg_upgrade_times_s"])]}

    total = sum(o["matches"] for o in orders) or 1
    common = sorted(orders, key=lambda o: -o["matches"])[:2]
    best = max([o for o in orders if o["matches"] / total >= 0.02],
               key=lambda o: _wilson_lb(o["wins"], o["matches"]), default=None)
    out["orders"] = []
    for label, o in [("most common", c) for c in common] + ([("best win rate", best)] if best else []):
        row = {"label": label, **summarize(o), "share": o["matches"] / total}
        # orders that differ only after the first three maxes read as duplicates
        if not any(r["unlock"] == row["unlock"] and r["max"][:3] == row["max"][:3] for r in out["orders"]):
            out["orders"].append(row)
    return out


# ---------- rendering: dense plain text, meant to be read by an agent (or a human) in seconds

def _pct(v, signed=False) -> str:
    if v is None:
        return "-"
    return f"{v * 100:+.1f}" if signed else f"{v * 100:.0f}%"


def _significant(r: dict) -> bool:
    return r.get("roster_z") is not None and abs(r["roster_z"]) >= Z_ITEM and abs(r["roster_delta"]) >= MIN_DELTA


def _delta(r: dict) -> str:
    if not _significant(r):
        return ""
    d = r["roster_delta"]
    vs = r["best_vs"] if d > 0 else r["worst_vs"]
    return f" Δ{_pct(d, True)}" + (f" ({vs[0]} {_pct(vs[1], True)})" if vs else "")


def _item(r: dict, extra: str = "") -> str:
    return (f"{r['name']} {r['cost']} {r['kind'] or '?'}, pick {_pct(r['pick'])} wr {_pct(r['wr'])}"
            f" @{_mmss(r['buy_s'])}{extra}{_delta(r)}")


def _sold(r: dict) -> str:
    return f" sold {_pct(r['sold'])}@{_mmss(r['sell_s'])}" if r.get("sold") and r["sold"] >= 0.2 and r.get("sell_s") else ""


def _tags(r: dict) -> str:
    return f" [{','.join(r['in_builds'])}]" if r.get("in_builds") else ""


def _mix(m) -> str:
    return "-" if not m else f"W{m['weapon']:.0%} S{m['spirit']:.0%} V{m['vitality']:.0%}"


def render_text(f: dict) -> str:
    h, v, w = f["hero"], f["version"], f["window"]
    badge = f"badge {w['min_badge'] or ''}-{w['max_badge'] or ''}" if w["min_badge"] or w["max_badge"] else "all ranks"
    rank = f" (#{h['wr_rank'][0]}/{h['wr_rank'][1]})" if h["wr_rank"] else ""
    out = [(f"# {h['name']} wr {_pct(h['win_rate'])}{rank} | build {v['client_version']} ({v['released_at']}) "
            f"| last {w['days']}d, {badge} | Δ/shift = win-rate pts"),
           f"Inventory: {SLOTS} item slots, any mix of weapon/spirit/vitality. Buying an upgrade replaces its component "
           f"in the same slot and costs the difference; a full inventory means selling (half the cost back)."]
    if f["errors"]:
        out.append("missing: " + "; ".join(f["errors"]))

    def kit(r, full: bool):
        for a in r.get("kit") or []:
            if full or a["ult"]:
                nums = "; ".join(a["numbers"][:8])
                out.append(f"  {'ULT' if a['ult'] else a['slot']} {a['name']}: {a['text']}{' [' + nums + ']' if nums else ''}")

    out += ["", f"## Enemies (item spend {_mix(f['enemies_damage_mix'])})"]
    for e in f["enemies"]:
        tags = [f"cc {'; '.join(e['cc'])}" if e["cc"] else "", f"heals {', '.join(e['heals'])}" if e["heals"] else "",
                f"stealth {', '.join(e['stealth'])}" if e["stealth"] else "",
                f"slow {', '.join(e['slow'])}" if e["slow"] else ""]
        out.append(f"- {e['name']}: me vs {_pct(e['my_wr_vs'])} ({_pct(e['shift'], True)}), their wr "
                   f"{_pct(e['their_wr'])}, {_mix(e.get('spend'))}, ult {e['ult'] or '-'}")
        out.append(f"  {' | '.join(t for t in tags if t) or 'no hard cc/heals'}")
        if e.get("core"):
            out.append(f"  buys {', '.join(e['core'])}")
        if e.get("counters"):
            out.append(f"  counters they buy: {', '.join(e['counters'])}")
        kit(e, True)
    out += ["", f"## Allies (item spend {_mix(f['allies_damage_mix'])}; ults only)"]
    for a in f["allies"]:
        bits = [f"cc {'; '.join(a['cc'])}" if a["cc"] else "", "heals" if a["heals"] else "",
                f"buys {', '.join(a['counters'])}" if a.get("counters") else ""]
        bits = " | ".join(b for b in bits if b)
        out.append(f"- {a['name']}: together {_pct(a['wr_with'])} ({_pct(a['shift'], True)}), "
                   f"{_mix(a.get('spend'))}{' | ' + bits if bits else ''}")
        kit(a, False)

    out += ["", "## Candidate builds (most played, ranked by win-rate lower bound; items = souls split of listed items;",
            "sections in the author's order, (optional) = marked situational by the author, \"...\" = author's note)"]
    for b in f["builds"]:
        lang = "" if b["language"] == 0 else f", lang {b['language']}"
        upd = f", updated {datetime.date.fromtimestamp(b['updated'])}" if b.get("updated") else ""
        out.append(f"{b['tag']} {b['name']} — id {b['hero_build_id']}, wr {_pct(b['win_rate'])}/{b['matches']}"
                   f"{lang}{upd}, items {_mix(b['spend'])}, has {', '.join(b['counters']) or 'no anti-heal/cleanse'}")
        for c in b["categories"]:
            head = f"  [{c['category']}]{' (optional)' if c.get('optional') else ''}" \
                   f"{' ' + repr(c['description']) if c.get('description') else ''} "
            out.append(head + ", ".join(
                f"{i['name']}{' (imbue ' + i['imbue'] + ')' if i.get('imbue') else ''}{_delta(i)}"
                f"{' \"' + i['note'] + '\"' if i.get('note') else ''}" for i in c["items"]))
    if not f["builds"]:
        out.append("none found")

    out += ["", "## Item pool by phase (bought in that phase by >= "
            f"{FLOW_PICK:.0%} of the players who reach it; pick / adj = win rate standardized for net worth at buy, "
            "vs everyone buying in that phase / avg souls at buy; sold = share later sold @ avg sell time; "
            "[B..] = in candidate builds)"]
    for p in f["phases"]:
        reach = f", {_pct(p['reached'])} of games reach it" if p.get("reached") is not None else ""
        out.append(f"{p['phase']} (phase wr {_pct(p['win_rate'])}{reach}): " + ", ".join(
            f"{r['name']} {_pct(r['phase_pick'])} adj {_pct(r['phase_adj'], True)} @{r['nw_at_buy'] / 1000:.1f}k"
            f"{_sold(r)}{_delta(r)}{_tags(r)}" for r in p["items"]))

    def flagged(rows):
        return [f"  {_item(r)}{_tags(r)}" for r in rows] or ["  none significant"]

    out += ["", (f"## Roster corrections (Δ = item win rate vs usual into these enemies, shown at z>={Z_ITEM:g} "
                 f"and |Δ|>={MIN_DELTA * 100:g}; (hero) = biggest single)"),
            "does better:", *flagged(f["add"]), "does worse:", *flagged(f["cut"])]
    out += ["per-enemy standouts:"] + ([f"  {s['item']} vs {s['enemy']} {_pct(s['delta'], True)} (z {s['z']:+.1f})"
                                        f"{_tags(s)}" for s in f["standouts"]]
                                       or ["  none significant"])
    out += ["counters:"] + [f"  [{r['counter']}] {_item(r)}" for r in f["counter_items"]]
    out += [f"{f['resist_kind']} resist (enemy spend lean):"] + \
           [f"  {_item(r, f' +{r["resist"]:g}%')}" for r in f["resist_items"]]

    s = f["skills"]
    out += ["", "## Skills (base numbers, (+x/spirit) = gain per point of spirit power; upgrades T1/T2/T3 cost 1/2/5 points)"]
    for a in s["abilities"]:
        out.append(f"{a['slot']} {a['name']}{' (ult)' if a.get('ult') else ''}: {a.get('text') or ''}")
        if a.get("numbers"):
            out.append(f"  {'; '.join(a['numbers'])}")
        out.append("  " + " / ".join(f"T{i + 1} {t}" for i, t in enumerate(a["upgrades"])))
    out.append("max first: " + ", ".join(f"{m['ability']} {_pct(m['share'])} wr {_pct(m['win_rate'])}"
                                          for m in s["max_first"]))
    for o in s["orders"]:
        out.append(f"{o['label']} ({_pct(o['share'])}, wr {_pct(o['win_rate'])}): unlock {' > '.join(o['unlock'])}"
                   f"; max {' > '.join(o['max'])} (T3 at {', '.join(o['t3_at'])})")
        out.append("  points: " + ", ".join(f"{i + 1}.{x['ability']}@{x['t']}" for i, x in enumerate(o["sequence"])))

    out += ["", "## Item glossary (items worth considering and their components, this game build's numbers; "
                "cond = when triggered/applied; <- = built from, -> = upgrades into)"]
    for g in f["glossary"]:
        stats = "; ".join(x for x in (", ".join(g["innate"]), ", ".join(g["effect"])) if x)
        path = "".join([f" <- {', '.join(g['from'])}" if g.get("from") else "",
                        f" -> {', '.join(g['into'])}" if g.get("into") else ""])
        out.append(f"{g['name']} {g['cost']} {g['kind'] or '?'}{' ACTIVE' if g['active'] else ''}{path}: {stats}"
                   f"{' — ' + g['text'] if g['text'] else ''}")

    out += ["", "## Tempo"]
    if f["nw_targets"]:
        out.append("avg souls: " + ", ".join(f"{_mmss(t)} {n}" for t, n in f["nw_targets"].items()))
    if g := f.get("games"):
        out.append(f"games last {_mmss(g['avg_duration_s'])} on average and end at {g['avg_end_souls'] / 1000:.1f}k souls; "
                   "players who reach each phase: " + ", ".join(f"{k} {_pct(v)}" for k, v in g["reach"].items()))
    if ln := f["lane"]:
        diff = "-" if ln["nw_diff_15"] is None else f"{ln['nw_diff_15']:+.0f}"
        out.append(f"lane {h['name']}{' + ' + ln['partner'] if ln['partner'] else ''} vs {' + '.join(ln['opponents'])}: "
                   f"{ln['matches']} games, wr {_pct(ln['win_rate'])}, souls diff @15:00 {diff}")
        for x in ln["vs_each"]:
            dx = "-" if x["nw_diff_15"] is None else f"{x['nw_diff_15']:+.0f}"
            out.append(f"  {h['name']} in lane vs {x['opponent']}: {x['matches']} games, wr {_pct(x['win_rate'])}, "
                       f"souls diff @15:00 {dx}")
    return "\n".join(out)
