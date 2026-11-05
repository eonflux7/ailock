"""Per-player match facts for coaching: precomputed numbers an agent can interpret.

Everything is derived from match metadata + the match's own game build +
population benchmarks (if synced). Facts are returned as plain dicts (JSON-able);
render_markdown() turns them into a compact report.
"""
from typing import Any

import duckdb

from . import paths
from .db import connect

BENCH = paths.DATA / "benchmarks"


# Valve's objective names and assigned_lane ids use different lane numbering. Checked against positions of
# heroes hitting each guardian: Lane1 = left (x ~ -7500), Lane3 = middle (x ~ 0), Lane4 = right (x ~ +7500).
OBJECTIVE_LANE = """case when objective like '%Lane1' then 'left' when objective like '%Lane3' then 'middle'
    when objective like '%Lane4' then 'right' end"""
ASSIGNED_LANE = {1: "left", 4: "middle", 6: "right"}


def lane_of_x(x: float) -> str:
    return "left" if x < -3500 else "right" if x > 3500 else "middle"


# damage source label: localized ability/item name; weapons have none, so name them by kind
SOURCE_NAME = """coalesce(a.name, case when a.type = 'Weapon' then
    'Weapon' || case when d.source_name like '%crit' then ' (crit)' else '' end end, d.source_name)"""


def _rows(con: duckdb.DuckDBPyConnection, sql: str, *params) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _bench(name: str, hero_id: int) -> str | None:
    p = BENCH / name / f"{hero_id}.parquet"
    return str(p) if p.exists() else None


def match_facts(match_id: int, account_id: int) -> dict[str, Any]:
    con = connect()
    m = _rows(con, "select m.*, mv.client_version from matches m join match_versions mv using (match_id) "
                   "where match_id = ?", match_id)
    if not m:
        raise SystemExit(f"match {match_id} not ingested")
    m = m[0]
    roster = _rows(con, "select player_slot, account_id, team, hero_id, hero_name, assigned_lane, kills, deaths, "
                        "assists, net_worth, last_hits, denies, level, won from player_heroes where match_id = ? "
                        "order by team, assigned_lane, player_slot", match_id)
    me = next((p for p in roster if p["account_id"] == account_id), None)
    if not me:
        raise SystemExit(f"account {account_id} not in match {match_id}")
    slot, hero_id, team = me["player_slot"], me["hero_id"], me["team"]
    lane_opponents = [p for p in roster if p["team"] != team and p["assigned_lane"] == me["assigned_lane"]]
    lane_mates = [p for p in roster if p["team"] == team and p["assigned_lane"] == me["assigned_lane"]
                  and p["player_slot"] != slot]

    facts: dict[str, Any] = {
        "match": {k: m[k] for k in ("match_id", "start_time", "duration_s", "game_mode", "match_mode",
                                     "client_version", "average_badge_team0", "average_badge_team1")},
        "player": me,
        "allies": [p for p in roster if p["team"] == team and p["player_slot"] != slot],
        "enemies": [p for p in roster if p["team"] != team],
        "lane_opponents": [p["hero_name"] for p in lane_opponents],
        "lane_mates": [p["hero_name"] for p in lane_mates],
    }

    # net worth flow: me vs lane opponents vs hero population curve
    snaps = _rows(con, "select player_slot, time_stamp_s, net_worth, level, kills, deaths, assists, "
                       "creep_kills, neutral_kills, denies, player_damage, player_damage_taken, ability_points "
                       "from player_stats where match_id = ? order by time_stamp_s", match_id)
    curve = _bench("performance_curve", hero_id)
    bench_nw = {}
    if curve:
        bench_nw = {r["game_time"]: r for r in _rows(
            con, f"select game_time, net_worth_avg, net_worth_std from read_parquet('{curve}')")}
    opp_slots = {p["player_slot"] for p in lane_opponents}
    flow = []
    for s in (s for s in snaps if s["player_slot"] == slot):
        t = s["time_stamp_s"]
        opp = [o["net_worth"] for o in snaps if o["player_slot"] in opp_slots and o["time_stamp_s"] == t]
        team_nw = sum(o["net_worth"] for o in snaps if o["time_stamp_s"] == t
                      and any(p["player_slot"] == o["player_slot"] and p["team"] == team for p in roster))
        enemy_nw = sum(o["net_worth"] for o in snaps if o["time_stamp_s"] == t
                       and any(p["player_slot"] == o["player_slot"] and p["team"] != team for p in roster))
        b = _nearest(bench_nw, t)
        flow.append({
            "t": t, "net_worth": s["net_worth"], "level": s["level"], "kda": f'{s["kills"]}/{s["deaths"]}/{s["assists"]}',
            "lane_opp_avg": round(sum(opp) / len(opp)) if opp else None,
            "vs_lane_opp": s["net_worth"] - round(sum(opp) / len(opp)) if opp else None,
            "team_lead": team_nw - enemy_nw,
            "hero_avg": round(b["net_worth_avg"]) if b else None,
            "z_vs_hero": round((s["net_worth"] - b["net_worth_avg"]) / b["net_worth_std"], 2)
            if b and b["net_worth_std"] else None,
        })
    facts["networth_flow"] = flow

    # soul sources at end of game
    facts["soul_sources"] = _rows(
        con, "select source, gold, gold_orbs, kills from player_gold_sources where match_id = ? and player_slot = ? "
             "and time_stamp_s = (select max(time_stamp_s) from player_gold_sources where match_id = ? "
             "and player_slot = ?) and gold + gold_orbs > 0 order by gold + gold_orbs desc",
        match_id, slot, match_id, slot)

    # purchases vs population timing and win rate
    items_bench = _bench("item_stats", hero_id)
    join = (f"left join read_parquet('{items_bench}') b on b.item_id = p.item_id" if items_bench else "")
    cols = (", round(b.avg_buy_time_s) as hero_avg_buy_s, round(b.win_rate, 3) as hero_item_win_rate, "
            "b.matches as hero_item_matches" if items_bench else "")
    facts["purchases"] = _rows(
        con, f"select p.game_time_s, p.name, p.class_name, p.slot, p.tier, p.cost, p.component_items, "
             f"nullif(p.sold_time_s, 0) as sold_time_s {cols} "
             f"from purchases p {join} where p.match_id = ? and p.player_slot = ? order by p.game_time_s",
        match_id, slot)
    for it in facts["purchases"]:
        if it.get("hero_avg_buy_s") is not None:
            it["delta_vs_avg_s"] = it["game_time_s"] - it["hero_avg_buy_s"]
        # a component "sold" the moment its upgrade is bought was consumed, not sold
        it["note"] = None
        if it["sold_time_s"]:
            into = next((u["name"] for u in facts["purchases"] if abs(u["game_time_s"] - it["sold_time_s"]) <= 1
                         and it["class_name"] in (u["component_items"] or [])), None)
            it["note"] = f"upgraded into {into}" if into else "sold"
    for it in facts["purchases"]:
        it.pop("component_items")
        it.pop("class_name")
    facts["ability_flow"] = _ability_flow(con, match_id, slot, hero_id, team, snaps)

    # unspent souls estimate: net worth minus cost of items held (sold items leave the inventory)
    held = facts["purchases"]
    for f in flow:
        inv = sum((i["cost"] or 0) for i in held
                  if i["game_time_s"] <= f["t"] and not (i["sold_time_s"] and i["sold_time_s"] <= f["t"]))
        f["unspent_est"] = f["net_worth"] - inv

    # deaths with killer hero and position
    facts["deaths"] = _rows(
        con, "select d.game_time_s, round(d.time_to_kill_s, 1) as time_to_kill_s, k.hero_name as killer, "
             "d.death_duration_s, round(d.x) as x, round(d.y) as y from deaths d "
             "left join player_heroes k on k.match_id = d.match_id and k.player_slot = d.killer_player_slot "
             "where d.match_id = ? and d.player_slot = ? order by d.game_time_s", match_id, slot)

    # damage taken: per attacker split by damage type, then the top individual sources
    facts["damage_taken"] = _rows(
        con, """
        select coalesce(h.hero_name, 'non-hero') as attacker,
               sum(d.value) as total,
               sum(d.value) filter (where d.source_name = 'Bullet') as bullet,
               sum(d.value) filter (where d.source_name in ('Ability', 'UnknownAbility')) as ability,
               sum(d.value) filter (where d.source_name = 'Melee') as melee,
               round(100.0 * sum(d.value) / sum(sum(d.value)) over (), 1) as pct
        from damage_final d
        left join player_heroes h on h.match_id = d.match_id and h.player_slot = d.dealer_player_slot
        where d.match_id = ? and d.target_player_slot = ? and d.stat_type = 'Damage'
          and d.source_kind = 'type_total' and d.value > 0
        group by all order by total desc
        """, match_id, slot)
    facts["damage_taken_sources"] = _rows(
        con, f"""
        select h.hero_name as attacker, {SOURCE_NAME} as source, a.type, d.value as damage
        from damage_final d
        join player_heroes h on h.match_id = d.match_id and h.player_slot = d.dealer_player_slot
        left join abilities a on a.class_name = regexp_replace(d.source_name, '_crit$', '')
             and a.client_version = h.client_version
        where d.match_id = ? and d.target_player_slot = ? and d.stat_type = 'Damage'
          and d.source_kind = 'source' and d.value > 0
        order by damage desc limit 12
        """, match_id, slot)

    # where the time went (1 Hz path samples)
    facts["time_split"] = _rows(
        con, "select combat_type, count(*) as seconds, round(100.0 * count(*) / sum(count(*)) over (), 1) as pct "
             "from paths where match_id = ? and player_slot = ? group by all order by seconds desc", match_id, slot)
    facts["movement"] = _rows(
        con, "select move_type, count(*) as seconds from paths where match_id = ? and player_slot = ? "
             "group by all order by seconds desc", match_id, slot)
    facts["seconds_dead"] = sum(d["death_duration_s"] or 0 for d in facts["deaths"])

    # objectives timeline from the player's team perspective
    facts["objectives"] = _rows(
        con, f"select destroyed_time_s, objective, {OBJECTIVE_LANE} as lane, "
             "case when team = ? then 'lost' else 'taken' end as result "
             "from objectives where match_id = ? and destroyed_time_s > 0 order by destroyed_time_s", team, match_id)
    facts["farm_windows"], facts["purchase_gaps"] = _farming(con, match_id, slot, facts, bench_nw, curve)
    facts["roster_checks"] = _roster_checks(con, match_id, team, hero_id)
    facts["benchmarks_available"] = bool(curve or items_bench)
    return facts


# Ability point cost per upgrade tier. Not in the game files we parse; verified against matches, where
# player_stats.ability_points (points earned so far) never drops below the cost of upgrades taken.
TIER_AP_COST = {1: 1, 2: 2, 3: 5}
TIER_LABEL = {0: "unlock", 1: "T1", 2: "T2", 3: "T3"}


def _ability_flow(con: duckdb.DuckDBPyConnection, match_id: int, slot: int, hero_id: int, team: int,
                  snaps: list[dict]) -> dict[str, Any]:
    """Skill unlocks/upgrades vs population timing, ability point balance, and what each ability did."""
    events = _rows(con, "select game_time_s, item_id as ability_id, name, upgrade_info from item_events "
                        "where match_id = ? and player_slot = ? and type in ('Signature', 'Ultimate') "
                        "order by game_time_s", match_id, slot)
    # population: average time of the k-th pick of each ability (1 = unlock, 2..4 = T1..T3), weighted by games
    orders = _bench("ability_orders", hero_id)
    pop = {(r["ability_id"], r["k"]): r for r in _rows(
        con, f"""
        with o as (select order_id, ability_id, avg_time_s, matches,
                          row_number() over (partition by order_id, ability_id order by position) as k
                   from read_parquet('{orders}'))
        select ability_id, k, sum(avg_time_s * matches) / sum(matches) as avg_s, sum(matches) as games
        from o group by all""")} if orders else {}

    picks, seen = [], {}
    for e in events:
        # each event is one step for that ability: 1st = unlock, 2nd..4th = T1..T3 (newer builds also encode
        # this as bits from 16 in upgrade_info; older ones leave it 0)
        k = seen[e["ability_id"]] = seen.get(e["ability_id"], 0) + 1
        p = pop.get((e["ability_id"], k))
        picks.append({"game_time_s": e["game_time_s"], "ability": e["name"], "tier": TIER_LABEL.get(k - 1, k - 1),
                      "ap_cost": TIER_AP_COST.get(k - 1, 0),
                      "hero_avg_s": round(p["avg_s"]) if p else None,
                      "delta_vs_avg_s": e["game_time_s"] - round(p["avg_s"]) if p else None})

    balance = []
    for s in (s for s in snaps if s["player_slot"] == slot):
        spent = sum(p["ap_cost"] for p in picks if p["game_time_s"] <= s["time_stamp_s"])
        balance.append({"t": s["time_stamp_s"], "level": s["level"], "ap_earned": s["ability_points"],
                        "ap_spent": spent, "ap_unspent": (s["ability_points"] or 0) - spent})

    first_max = next((p for p in picks if p["tier"] == "T3"), None)
    max_first = _bench("ability_max_first", hero_id)
    max_first_stats = _rows(
        con, f"""
        select coalesce(a.name, 'none') as ability, b.matches, round(b.matches / sum(b.matches) over (), 3) as share,
               round(b.win_rate, 3) as win_rate
        from read_parquet('{max_first}') b
        left join abilities a on a.ability_id = b.ability_id
             and a.client_version = (select client_version from match_versions where match_id = ?)
        order by b.matches desc""", match_id) if max_first else []

    heroes = "(select player_slot, team, hero_name, client_version from player_heroes where match_id = ?)"
    damage = _rows(
        con, f"""
        select {SOURCE_NAME} as source, a.type, sum(d.value) as damage,
               round(100.0 * sum(d.value) / sum(sum(d.value)) over (), 1) as pct
        from damage_final d
        join {heroes} t on t.player_slot = d.target_player_slot
        join {heroes} me on me.player_slot = d.dealer_player_slot
        left join abilities a on a.class_name = regexp_replace(d.source_name, '_crit$', '')
             and a.client_version = me.client_version
        where d.match_id = ? and d.dealer_player_slot = ? and t.team <> ? and d.stat_type = 'Damage'
          and d.source_kind = 'source' and d.value > 0
        group by all order by damage desc limit 10
        """, match_id, match_id, match_id, slot, team)
    cc = _rows(
        con, f"""
        select replace(d.source_name, 'Citadel_Graph_CrowdControl_', '') as kind, t.hero_name as target,
               round(100.0 * sum(d.value) / sum(sum(d.value)) over (), 1) as pct
        from damage_final d join {heroes} t on t.player_slot = d.target_player_slot
        where d.match_id = ? and d.dealer_player_slot = ? and t.team <> ? and d.stat_type = 'CrowdControl'
        group by all order by pct desc
        """, match_id, match_id, slot, team)
    return {"picks": picks, "ap_balance": balance,
            "first_max": {"ability": first_max["ability"], "game_time_s": first_max["game_time_s"]}
            if first_max else None,
            "max_first_population": max_first_stats, "damage_to_heroes": damage, "cc_dealt": cc}


# soul source groups: match-metadata source names -> performance-curve column prefixes
SOUL_GROUPS = {
    "lane": (("LaneCreeps", "Denies"), ("gold_lane_creep", "gold_lane_creep_orbs", "gold_denied")),
    "jungle": (("Neutrals",), ("gold_neutral_creep", "gold_neutral_creep_orbs")),
    "heroes": (("Players", "Assists", "AbilityAssassinate"),
               ("gold_player", "gold_player_orbs", "gold_assists", "gold_ability_assassinate")),
    "objectives": (("Bosses", "Treasure", "Breakable"), ("gold_boss", "gold_boss_orb", "gold_treasure",
                                                         "gold_breakable")),
    "other": (("TeamBonus", "ItemTrophyCollector", "ItemCultistSacrifice", "ItemGooseEgg"),
              ("gold_team_bonus", "gold_item_trophy_collector", "gold_item_cultist_sacrifice",
               "gold_item_goose_egg")),
}
GAP_MIN_S = 240


def _farming(con: duckdb.DuckDBPyConnection, match_id: int, slot: int, facts: dict, bench_nw: dict,
             curve: str | None) -> tuple[list[dict], list[dict]]:
    """Souls per minute by source per snapshot window vs the hero average, and long gaps between purchases."""
    gold = _rows(con, "select time_stamp_s as t, source, gold + gold_orbs as v from player_gold_sources "
                      "where match_id = ? and player_slot = ?", match_id, slot)
    mine: dict[int, dict[str, int]] = {}
    for g in gold:
        mine.setdefault(g["t"], {})[g["source"]] = g["v"]
    avg_cols = sorted({c for _, cols in SOUL_GROUPS.values() for c in cols})
    avg = {r["game_time"]: r for r in _rows(
        con, f"select game_time, {', '.join(f'{c}_avg as {c}' for c in avg_cols)} from read_parquet('{curve}')")} \
        if curve else {}

    def group_totals(t: int) -> tuple[dict, dict | None]:
        m = {k: sum(mine.get(t, {}).get(s, 0) for s in srcs) for k, (srcs, _) in SOUL_GROUPS.items()}
        b = _nearest(avg, t)
        a = {k: sum(b[c] or 0 for c in cols) for k, (_, cols) in SOUL_GROUPS.items()} if b else None
        return m, a

    # seconds per activity from 1 Hz paths; Valve's combat_type says what the player was fighting
    activity = _rows(con, "select floor(game_time_s)::int as t, combat_type from paths "
                          "where match_id = ? and player_slot = ? and health > 0", match_id, slot)
    dead = [(d["game_time_s"], d["game_time_s"] + (d["death_duration_s"] or 0)) for d in facts["deaths"]]

    def time_split(t0: int, t1: int) -> dict:
        kinds = {"EnemyNPC": "lane_s", "Neutral": "jungle_s", "Player": "fighting_s", "Out": "out_of_combat_s"}
        out = {v: 0 for v in kinds.values()}
        for a in activity:
            if t0 <= a["t"] < t1 and a["combat_type"] in kinds:
                out[kinds[a["combat_type"]]] += 1
        out["dead_s"] = sum(max(0, min(e, t1) - max(s, t0)) for s, e in dead)
        return out

    times = sorted(mine)
    windows = []
    for t0, t1 in zip([0] + times, times):
        m1, a1 = group_totals(t1)
        m0, a0 = group_totals(t0) if t0 else ({k: 0 for k in SOUL_GROUPS}, {k: 0 for k in SOUL_GROUPS})
        mins = (t1 - t0) / 60
        row = {"t0": t0, "t1": t1, "spm": round((sum(m1.values()) - sum(m0.values())) / mins)}
        if a1 and a0 is not None:
            row["hero_avg_spm"] = round((sum(a1.values()) - sum(a0.values())) / mins)
        for k in SOUL_GROUPS:
            row[k] = round((m1[k] - m0[k]) / mins)
            if a1 and a0 is not None:
                row[f"{k}_avg"] = round((a1[k] - a0[k]) / mins)
        windows.append({**row, **time_split(t0, t1)})

    # net worth at any time: linear between snapshots (the ~3 min cadence is all we have)
    flow = [(0, 600, 600)] + [(f["t"], f["net_worth"], (_nearest(bench_nw, f["t"]) or {}).get("net_worth_avg"))
                              for f in facts["networth_flow"]]

    def nw_at(t: float, col: int) -> float | None:
        for (ta, *va), (tb, *vb) in zip(flow, flow[1:]):
            if ta <= t <= tb and va[col - 1] is not None and vb[col - 1] is not None:
                return va[col - 1] + (vb[col - 1] - va[col - 1]) * (t - ta) / (tb - ta)
        return None

    buys = sorted({p["game_time_s"] for p in facts["purchases"]})
    gaps = []
    for t0, t1 in zip(buys, buys[1:]):
        if t1 - t0 < GAP_MIN_S:
            continue
        bought = [p for p in facts["purchases"] if t1 <= p["game_time_s"] <= t1 + 5]
        earned, avg_earned = nw_at(t1, 1), nw_at(t1, 2)
        earned = earned - nw_at(t0, 1) if earned is not None else None
        avg_earned = avg_earned - nw_at(t0, 2) if avg_earned is not None and nw_at(t0, 2) is not None else None
        gap = {"t0": t0, "t1": t1, "gap_s": t1 - t0, "bought_next": ", ".join(p["name"] for p in bought),
               "cost": sum(p["cost"] or 0 for p in bought), "earned": round(earned) if earned is not None else None,
               "hero_avg_earned": round(avg_earned) if avg_earned is not None else None}
        if earned is not None and avg_earned:
            # how much sooner the next buy lands at the hero's average farm rate
            gap["sooner_at_avg_pace_s"] = round(max(0, avg_earned - earned) / (avg_earned / (t1 - t0)))
        gaps.append({**gap, **time_split(t0, t1)})
    return windows, gaps


# Counter items classified from the build's own item data, so this follows patches. Resists aren't classified
# here: nearly every item carries some, so resist_profile measures what each hero actually had instead.
COUNTER_ITEMS = """
select distinct a.client_version, a.ability_id, a.name, a.tier, 'anti_heal' as category
from abilities a join ability_properties p using (client_version, ability_id)
where a.type = 'Item' and p.property = 'HealAmpReceivePenaltyPercent' and p.value_num < 0
  and not coalesce(p.negative, false)  -- e.g. Cheat Death's penalty is on its owner
union
select client_version, ability_id, name, tier, 'cleanse' from abilities
where type = 'Item' and regexp_matches(description, 'remove all.*debuff|cleanse|immune to stun|unstoppable', 'i')
"""
RESISTS = {"TechResist": "spirit_resist", "BulletResist": "bullet_resist", "StatusResistancePercent": "debuff_resist"}


def _roster_checks(con: duckdb.DuckDBPyConnection, match_id: int, team: int, hero_id: int) -> dict[str, Any]:
    """What the enemy roster did to us (healing, damage type, CC) vs the counters our team bought."""
    heroes = "(select player_slot, team, hero_name, client_version from player_heroes where match_id = ?)"
    checks: dict[str, Any] = {}

    checks["enemy_healing"] = _rows(
        con, f"""
        select h.hero_name, sum(d.value) as healing, arg_max(d.source_name, d.value) as top_source
        from damage_final d join {heroes} h on h.player_slot = d.dealer_player_slot
        where d.match_id = ? and d.stat_type = 'Healing' and h.team <> ?
        group by all having sum(d.value) > 0 order by healing desc
        """, match_id, match_id, team)
    checks["heal_prevented"] = _rows(
        con, f"""
        select h.team = ? as by_us, sum(d.value) as prevented
        from damage_final d join {heroes} h on h.player_slot = d.dealer_player_slot
        where d.match_id = ? and d.stat_type = 'HealPrevented' group by all
        """, team, match_id, match_id)

    # enemy hero damage into each of our heroes, by type
    checks["damage_mix"] = _rows(
        con, f"""
        select t.hero_name as target,
               sum(d.value) filter (where d.source_name = 'Bullet') as bullet,
               sum(d.value) filter (where d.source_name in ('Ability', 'UnknownAbility')) as spirit,
               sum(d.value) filter (where d.source_name = 'Melee') as melee,
               round(100.0 * sum(d.value) filter (where d.source_name in ('Ability', 'UnknownAbility'))
                     / sum(d.value), 1) as spirit_pct
        from damage_final d
        join {heroes} e on e.player_slot = d.dealer_player_slot
        join {heroes} t on t.player_slot = d.target_player_slot
        where d.match_id = ? and d.stat_type = 'Damage' and d.source_kind = 'type_total'
          and e.team <> ? and t.team = ?
        group by all order by target
        """, match_id, match_id, match_id, team, team)

    # CC amounts are in the game's own units; compare shares, not absolutes
    checks["cc_received"] = _rows(
        con, f"""
        select e.hero_name as dealer, replace(d.source_name, 'Citadel_Graph_CrowdControl_', '') as kind,
               round(100.0 * sum(d.value) / sum(sum(d.value)) over (), 1) as pct
        from damage_final d
        join {heroes} e on e.player_slot = d.dealer_player_slot
        join {heroes} t on t.player_slot = d.target_player_slot
        where d.match_id = ? and d.stat_type = 'CrowdControl' and e.team <> ? and t.team = ?
        group by all order by pct desc
        """, match_id, match_id, match_id, team, team)

    checks["counters_bought"] = _rows(
        con, f"""
        select c.category, h.hero_name, c.name as item, p.game_time_s
        from player_items p
        join {heroes} h on h.player_slot = p.player_slot
        join ({COUNTER_ITEMS}) c on c.ability_id = p.item_id and c.client_version = h.client_version
        where p.match_id = ? and h.team = ?
        order by c.category, p.game_time_s
        """, match_id, match_id, team)

    # resist from items held at checkpoints; Deadlock stacks resists multiplicatively. Conditional values
    # (actives, on-proc buffs: anything outside the Innate and Passive tooltip sections) and hero base
    # stats are left out.
    duration = con.execute("select duration_s from matches where match_id = ?", [match_id]).fetchall()[0][0]
    checkpoints = sorted({900, 1500, duration})
    profile = _rows(
        con, f"""
        with held as (
            select h.hero_name, cp.t, i.item_id, h.client_version
            from player_items i join {heroes} h on h.player_slot = i.player_slot
            cross join (select unnest(?::int[]) as t) cp
            where i.match_id = ? and h.team = ? and i.game_time_s <= cp.t
              and coalesce(nullif(i.sold_time_s, 0), 1e9) > cp.t)
        select hero_name, t, property, round(100 * (1 - exp(sum(ln(1 - least(p.value_num, 99) / 100)))), 1) as pct
        from held join abilities a on a.ability_id = held.item_id and a.client_version = held.client_version
             and a.type = 'Item'
        join ability_properties p on p.ability_id = a.ability_id and p.client_version = a.client_version
        where p.property in {tuple(RESISTS)} and p.value_num > 0 and not coalesce(p.negative, false)
          and p.usage is distinct from 'ConditionallyApplied' and p.section in ('Innate', 'Passive')
        group by all
        """, match_id, checkpoints, match_id, team)
    ours = [r["hero_name"] for r in _rows(con, "select hero_name from player_heroes where match_id = ? and team = ?",
                                          match_id, team)]
    by_hero: dict[tuple, dict] = {(h, t): {"hero_name": h, "t": t, **{v: 0.0 for v in RESISTS.values()}}
                                  for h in ours for t in checkpoints}
    for r in profile:
        by_hero[(r["hero_name"], r["t"])][RESISTS[r["property"]]] = r["pct"]
    mix = {d["target"]: d["spirit_pct"] for d in checks["damage_mix"]}
    checks["resist_profile"] = [{**v, "spirit_dmg_taken_pct": mix.get(v["hero_name"])}
                                for _, v in sorted(by_hero.items())]

    # how often players of this hero buy each counter item (population, if benchmarks are synced)
    items_bench, hero_bench = _bench("item_stats", hero_id), BENCH / "hero_stats.parquet"
    checks["hero_counter_options"] = _rows(
        con, f"""
        select c.category, c.name as item, c.tier, round(b.matches / hs.matches, 3) as pick_rate,
               round(b.win_rate, 3) as win_rate, round(b.avg_buy_time_s) as avg_buy_s
        from read_parquet('{items_bench}') b
        join ({COUNTER_ITEMS}) c on c.ability_id = b.item_id
             and c.client_version = (select client_version from match_versions where match_id = ?)
        join read_parquet('{hero_bench}') hs on hs.hero_id = ?
        qualify row_number() over (partition by c.category order by b.matches desc) <= 3
        order by c.category, pick_rate desc
        """, match_id, hero_id) if items_bench and hero_bench.exists() else []
    return checks


def _nearest(series: dict[int, dict], t: int) -> dict | None:
    if not series:
        return None
    k = min(series, key=lambda x: abs(x - t))
    return series[k] if abs(k - t) <= 90 else None


def _mmss(s) -> str:
    return "-" if s is None else f"{int(s) // 60}:{int(s) % 60:02d}"


def _table(rows: list[dict], cols: list[str], fmt: dict | None = None) -> str:
    fmt = fmt or {}
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(fmt.get(c, lambda v: "-" if v is None else v)(r.get(c))) for c in cols) + " |")
    return "\n".join(out)


def _render_ability_flow(af: dict, t: dict) -> list[str]:
    fm = af["first_max"]
    out = ["## Ability flow",
           _table(af["picks"], ["game_time_s", "ability", "tier", "ap_cost", "hero_avg_s", "delta_vs_avg_s"],
                  {**t, "hero_avg_s": _mmss}),
           "", f"First maxed: {fm['ability']} at {_mmss(fm['game_time_s'])}." if fm else "No ability maxed.",
           "", "Ability points (earned = from levels and bosses so far):",
           _table(af["ap_balance"], ["t", "level", "ap_earned", "ap_spent", "ap_unspent"], t)]
    if af["max_first_population"]:
        out += ["", "Which ability players of this hero max first:",
                _table(af["max_first_population"], ["ability", "matches", "share", "win_rate"])]
    out += ["", "Damage to enemy heroes by source:", _table(af["damage_to_heroes"], ["source", "type", "damage", "pct"]),
            "", "CC dealt to enemy heroes (share):", _table(af["cc_dealt"][:10], ["kind", "target", "pct"])]
    return out


def render_markdown(f: dict) -> str:
    p, m = f["player"], f["match"]
    t = {"t": _mmss, "game_time_s": _mmss, "sold_time_s": _mmss, "hero_avg_buy_s": _mmss}
    lines = [
        f"# Match {m['match_id']}: {p['hero_name']} ({'win' if p['won'] else 'loss'})",
        f"{m['game_mode']}/{m['match_mode']}, {_mmss(m['duration_s'])}, client version {m['client_version']}. "
        f"KDA {p['kills']}/{p['deaths']}/{p['assists']}, net worth {p['net_worth']}, level {p['level']}, "
        f"last hits {p['last_hits']}, denies {p['denies']}.",
        f"Lane {ASSIGNED_LANE.get(p['assigned_lane'], p['assigned_lane'])}: with {', '.join(f['lane_mates']) or '-'} vs {', '.join(f['lane_opponents']) or '-'}.",
        f"Allies: {', '.join(a['hero_name'] or str(a['hero_id']) for a in f['allies'])}. "
        f"Enemies: {', '.join(e['hero_name'] or str(e['hero_id']) for e in f['enemies'])}.",
        "", "## Net worth flow",
        _table(f["networth_flow"], ["t", "net_worth", "lane_opp_avg", "vs_lane_opp", "team_lead", "hero_avg",
                                    "z_vs_hero", "unspent_est", "kda"], t),
        "", "## Purchases",
        _table(f["purchases"], ["game_time_s", "name", "slot", "tier", "cost", "sold_time_s", "note"]
               + (["hero_avg_buy_s", "delta_vs_avg_s", "hero_item_win_rate"] if f["benchmarks_available"] else []), t),
        "", *_render_ability_flow(f["ability_flow"], t),
        "", "## Soul sources (end of game)", _table(f["soul_sources"], ["source", "gold", "gold_orbs", "kills"]),
        "", "## Deaths", _table(f["deaths"], ["game_time_s", "killer", "time_to_kill_s", "death_duration_s", "x", "y"], t),
        "", "## Damage taken", _table(f["damage_taken"], ["attacker", "total", "bullet", "ability", "melee", "pct"]),
        "", "Top damage sources:", _table(f["damage_taken_sources"], ["attacker", "source", "type", "damage"]),
        "", f"## Time split (dead {f['seconds_dead']}s)", _table(f["time_split"], ["combat_type", "seconds", "pct"]),
        "", "## Objectives", _table(f["objectives"], ["destroyed_time_s", "objective", "lane", "result"],
                                    {"destroyed_time_s": _mmss}),
    ]
    win = {"t0": _mmss, "t1": _mmss}
    groups = [c for k in SOUL_GROUPS for c in (k, f"{k}_avg")]
    lines += [
        "", "## Farm rate (souls/min per window; *_avg = hero average)",
        _table(f["farm_windows"], ["t0", "t1", "spm", "hero_avg_spm"] + groups, win),
        "", "Where the time went (seconds):",
        _table(f["farm_windows"], ["t0", "t1", "lane_s", "jungle_s", "fighting_s", "out_of_combat_s", "dead_s"], win),
        "", f"## Purchase gaps (>= {GAP_MIN_S // 60} min without buying)",
        _table(f["purchase_gaps"], ["t0", "t1", "bought_next", "cost", "earned", "hero_avg_earned",
                                    "sooner_at_avg_pace_s", "lane_s", "jungle_s", "fighting_s", "out_of_combat_s",
                                    "dead_s"], win) if f["purchase_gaps"] else "_none_",
    ]
    rc = f["roster_checks"]
    prevented = {r["by_us"]: r["prevented"] for r in rc["heal_prevented"]}
    lines += [
        "", "## Roster checks",
        f"Enemy healing {sum(r['healing'] for r in rc['enemy_healing'])}; healing we prevented "
        f"{prevented.get(True, 0)} (only sources the game records as HealPrevented).",
        _table(rc["enemy_healing"], ["hero_name", "healing", "top_source"]),
        "", "Enemy hero damage into our heroes:",
        _table(rc["damage_mix"], ["target", "bullet", "spirit", "melee", "spirit_pct"]),
        "", "CC our team received (share of total):",
        _table(rc["cc_received"][:10], ["dealer", "kind", "pct"]),
        "", "Always-on item resists of our heroes (%, multiplicative; excludes hero base stats and actives):",
        _table(rc["resist_profile"], ["hero_name", "t", "spirit_resist", "bullet_resist", "debuff_resist",
                                      "spirit_dmg_taken_pct"], t),
        "", "Anti-heal and cleanse items our team bought:",
        _table(rc["counters_bought"], ["category", "hero_name", "item", "game_time_s"], t)
        if rc["counters_bought"] else "_none_",
    ]
    if rc["hero_counter_options"]:
        lines += ["", f"Counter items {p['hero_name']} players usually buy:",
                  _table(rc["hero_counter_options"], ["category", "item", "tier", "pick_rate", "win_rate", "avg_buy_s"],
                         {"avg_buy_s": _mmss})]
    if not f["benchmarks_available"]:
        lines.append("\n_No population benchmarks for this hero yet: run `ailock bench`._")
    return "\n".join(lines)
