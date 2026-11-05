"""Lane phase facts: who actually laned with/against whom, positioning per minute, trades, and lane deaths.

Lane partner and opponents come from positions (who stood near whom), not Valve's assigned_lane, since
players swap. "Advance" is progress toward the enemy base along the map's long (y) axis, in the player's
team orientation: higher = further forward. All distances are world units (~2,500 = a short fight range).
"""
from statistics import median
from typing import Any


from .db import DAMAGE_TYPE_TOTALS, connect
from .report import ASSIGNED_LANE, OBJECTIVE_LANE, _mmss, _rows, _table, lane_of_x

NEAR = 3000  # "nearby" radius for counting allies/enemies around a fight
AWAY = 3500  # this far from the player's lane spot counts as having left lane


def _dist(a: tuple, b: tuple) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def lane_facts(match_id: int, account_id: int, until_s: int = 600) -> dict[str, Any]:
    con = connect()
    roster = _rows(con, "select player_slot, account_id, team, hero_name, assigned_lane, won from player_heroes "
                        "where match_id = ?", match_id)
    me = next((p for p in roster if p["account_id"] == account_id), None)
    if not me:
        raise SystemExit(f"account {account_id} not in match {match_id}")
    by_slot = {p["player_slot"]: p for p in roster}
    pos: dict[int, dict[int, tuple]] = {}
    for r in con.execute("select player_slot, floor(game_time_s)::int, x, y, health, combat_type from paths "
                         "where match_id = ? and game_time_s <= ? and health > 0", [match_id, until_s]).fetchall():
        pos.setdefault(r[0], {})[r[1]] = (r[2], r[3], r[4], r[5])

    # team orientation from spawn: bases sit at opposite ends of the y axis
    spawn_y = median(pos[me["player_slot"]][t][1] for t in sorted(pos[me["player_slot"]])[:5])
    sign = 1 if spawn_y < 0 else -1

    def med_dist(a: int, b: int, t0: int = 60, t1: int = 480) -> float | None:
        d = [_dist(pos[a][t], pos[b][t]) for t in range(t0, min(t1, until_s) + 1)
             if t in pos.get(a, {}) and t in pos.get(b, {})]
        return median(d) if d else None

    slot, team = me["player_slot"], me["team"]
    allies = [p["player_slot"] for p in roster if p["team"] == team and p["player_slot"] != slot]
    enemies = [p["player_slot"] for p in roster if p["team"] != team]
    partner = min(allies, key=lambda s: med_dist(slot, s) or 1e9)
    lane_opps = sorted(enemies, key=lambda s: min(med_dist(slot, s) or 1e9, med_dist(partner, s) or 1e9))[:2]
    lane = [slot, partner, *lane_opps]
    my_spot = (median(v[0] for t, v in pos[slot].items() if 60 <= t <= 480),
               median(v[1] for t, v in pos[slot].items() if 60 <= t <= 480))

    def name(s: int) -> str:
        return by_slot[s]["hero_name"] or f"slot {s}"

    facts: dict[str, Any] = {
        "match_id": match_id, "until_s": until_s, "player": name(slot), "won": me["won"],
        "partner": {"hero": name(partner), "account_id": by_slot[partner]["account_id"],
                    "median_dist": round(med_dist(slot, partner) or 0)},
        "opponents": [{"hero": name(s), "account_id": by_slot[s]["account_id"],
                       "median_dist": round(med_dist(slot, s) or 0)} for s in lane_opps],
        "assigned_lane": ASSIGNED_LANE.get(me["assigned_lane"], me["assigned_lane"]),
        "lane": lane_of_x(my_spot[0]),  # where the player actually stood in 1:00-8:00
    }

    # per-minute positioning
    minutes = []
    for m0 in range(0, until_s, 60):
        secs = range(m0, m0 + 60)
        mine = [(t, pos[slot][t]) for t in secs if t in pos.get(slot, {})]
        row: dict[str, Any] = {"t": m0, "alive_s": len(mine)}
        if mine:
            row["hp"] = round(sum(v[2] for _, v in mine) / len(mine))
            row["advance"] = round(sum(v[1] * sign for _, v in mine) / len(mine))
            pd = [_dist(v, pos[partner][t]) for t, v in mine if t in pos.get(partner, {})]
            row["partner_dist"] = round(sum(pd) / len(pd)) if pd else None
            pa = [pos[partner][t][1] * sign for t in secs if t in pos.get(partner, {})]
            row["partner_advance"] = round(sum(pa) / len(pa)) if pa else None
            oa = [pos[o][t][1] * sign for o in lane_opps for t in secs if t in pos.get(o, {})]
            row["opp_advance"] = round(sum(oa) / len(oa)) if oa else None
            od = [min(_dist(v, pos[o][t]) for o in lane_opps if t in pos.get(o, {})) for t, v in mine
                  if any(t in pos.get(o, {}) for o in lane_opps)]
            row["nearest_opp_dist"] = round(sum(od) / len(od)) if od else None
            # demo-derived paths carry no combat state ("Unknown")
            row["fighting_s"] = sum(1 for _, v in mine if v[3] == "Player") \
                if any(v[3] != "Unknown" for _, v in mine) else None
            row["away_s"] = sum(1 for _, v in mine if _dist(v, my_spot) > AWAY)
            # enemies from other lanes showing up near me = rotations/ganks
            row["extra_enemies_max"] = max(
                sum(1 for e in enemies if e not in lane_opps and t in pos.get(e, {}) and _dist(v, pos[e][t]) < NEAR)
                for t, v in mine)
        minutes.append(row)
    facts["minutes"] = minutes

    # economy per snapshot: our pair vs their pair
    stats = {(r["player_slot"], r["time_stamp_s"]): r for r in _rows(
        con, "select player_slot, time_stamp_s, net_worth, creep_kills, denies, kills, deaths, gold_lane_creep "
             "+ gold_lane_creep_orbs as lane_souls, gold_player + gold_player_orbs as kill_souls, level "
             "from player_stats where match_id = ? and time_stamp_s <= ?", match_id, until_s + 60)}
    times = sorted({t for _, t in stats})
    facts["economy"] = []
    for t in times:
        row = {"t": t}
        for label, slots in (("us", [slot, partner]), ("them", lane_opps)):
            for k in ("net_worth", "creep_kills", "denies", "lane_souls", "kill_souls"):
                row[f"{label}_{k}"] = sum((stats.get((s, t)) or {}).get(k) or 0 for s in slots)
        row["me_net_worth"] = (stats.get((slot, t)) or {}).get("net_worth")
        row["partner_net_worth"] = (stats.get((partner, t)) or {}).get("net_worth")
        row["nw_diff"] = row["us_net_worth"] - row["them_net_worth"]
        facts["economy"].append(row)

    # hero damage traded between the four lane heroes per 3-min sample (cumulative counters -> deltas)
    dmg = _rows(con, f"""
        select sample_time_s as t, dealer_player_slot as dealer, target_player_slot as target, sum(value) as v
        from damage where match_id = ? and stat_type = 'Damage' and source_name in {DAMAGE_TYPE_TOTALS}
          and dealer_player_slot in ({','.join(map(str, lane))}) and target_player_slot in ({','.join(map(str, lane))})
          and sample_time_s <= ? group by all""", match_id, until_s + 180)
    cum = {}
    for r in dmg:
        side = "us" if r["dealer"] in (slot, partner) else "them"
        if (side == "us") == (r["target"] in (slot, partner)):
            continue
        cum.setdefault(r["t"], {"us": 0, "them": 0, "me": 0})
        cum[r["t"]][side] += r["v"]
        if r["dealer"] == slot:
            cum[r["t"]]["me"] += r["v"]
    prev = {"us": 0, "them": 0, "me": 0}
    facts["trades"] = []
    for t in sorted(cum):
        c = cum[t]
        facts["trades"].append({"t": t, "dealt_us": c["us"] - prev["us"], "dealt_by_me": c["me"] - prev["me"],
                                "dealt_them": c["them"] - prev["them"]})
        prev = c

    # deaths involving lane heroes (or near me) with the situation around them
    deaths = _rows(con, "select game_time_s as t, player_slot as victim, killer_player_slot as killer, x, y "
                        "from deaths where match_id = ? and game_time_s <= ? order by 1", match_id, until_s)
    facts["deaths"] = []
    for d in deaths:
        t, v = int(d["t"]), d["victim"]
        here = (d["x"], d["y"])
        if v not in lane and not (t in pos.get(slot, {}) and _dist(pos[slot][t], here) < NEAR):
            continue
        before = t - 5  # look a few seconds earlier: at death the victim has no live position

        def around(team_of: int) -> list[str]:
            return [name(s) for s, p in by_slot.items() if p["team"] == team_of and s != v
                    and before in pos.get(s, {}) and _dist(pos[s][before], here) < NEAR]
        vteam = by_slot[v]["team"]
        hp_before = [pos[v][s][2] for s in range(t - 10, t - 4) if s in pos.get(v, {})]
        facts["deaths"].append({
            "t": t, "victim": name(v), "ours": vteam == team, "killer": name(d["killer"]) if d["killer"] in by_slot
            else "non-hero", "killer_from_lane": d["killer"] in lane,
            "victim_hp_10s_before": hp_before[0] if hp_before else None,
            "advance": round(d["y"] * sign),  # our orientation, also for enemy victims
            "victim_allies_near": around(vteam), "victim_enemies_near": around(1 - vteam),
        })

    facts["tier1_objectives"] = _rows(
        con, f"select destroyed_time_s as t, objective, {OBJECTIVE_LANE} as lane, "
             "case when team = ? then 'lost' else 'taken' end as result "
             "from objectives where match_id = ? and objective like 'Tier1%' and destroyed_time_s > 0 order by 1",
        team, match_id)
    for o in facts["tier1_objectives"]:
        o["this_lane"] = o["lane"] == facts["lane"]
    return facts


def render_markdown(f: dict) -> str:
    t = {"t": _mmss}
    opp = ", ".join(f"{o['hero']} ({o['median_dist']})" for o in f["opponents"])
    lines = [
        f"# Lane phase {f['match_id']}: {f['player']} ({'win' if f['won'] else 'loss'}), 0:00-{_mmss(f['until_s'])}",
        f"Partner: {f['partner']['hero']} (median distance {f['partner']['median_dist']}). Opponents: {opp}. "
        f"Lane: {f['lane']} (Valve assigned: {f['assigned_lane']}).",
        "", "## Economy (our pair vs their pair)",
        _table(f["economy"], ["t", "me_net_worth", "partner_net_worth", "us_net_worth", "them_net_worth", "nw_diff",
                              "us_creep_kills", "them_creep_kills", "us_denies", "them_denies", "us_lane_souls",
                              "them_lane_souls", "us_kill_souls", "them_kill_souls"], t),
        "", "## Hero damage traded per window (lane heroes only)",
        _table(f["trades"], ["t", "dealt_us", "dealt_by_me", "dealt_them"], t),
        "", "## Positioning per minute (advance: higher = further toward enemy base)",
        _table(f["minutes"], ["t", "alive_s", "hp", "advance", "partner_advance", "opp_advance", "partner_dist",
                              "nearest_opp_dist", "fighting_s", "away_s", "extra_enemies_max"], t),
        "", "## Deaths in lane phase (advance in our orientation; near = within 3000 of the death, 5 s before)",
        _table(f["deaths"], ["t", "victim", "ours", "killer", "killer_from_lane", "victim_hp_10s_before",
                             "advance", "victim_allies_near", "victim_enemies_near"], t) if f["deaths"] else "_none_",
        "", "## Tier 1 guardians",
        _table(f["tier1_objectives"], ["t", "lane", "this_lane", "result"], t),
    ]
    return "\n".join(lines)
