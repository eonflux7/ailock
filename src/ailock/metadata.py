"""Flatten CMsgMatchMetaDataContents into normalized row tables.

One match -> dict[table_name, list[row]]. Every row carries match_id so the
per-match parquet files can be unioned into one DuckDB view per table.
Times are game seconds unless the column says otherwise.
"""
from typing import Any

from .proto import cm

MC = cm.CMsgMatchMetaDataContents
PATHS = cm.CMsgMatchPlayerPathsData
DMG = cm.CMsgMatchPlayerDamageMatrix

TEAM = {0: 0, 1: 1, 16: -1}  # ECitadelLobbyTeam: Team0, Team1, Spectator


def _enum(enum_type, value: int, prefix: str) -> str:
    try:
        return enum_type.Name(value).removeprefix(prefix)
    except ValueError:
        return str(value)


def _scalars(msg, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    """All non-repeated, non-message fields of a proto message (defaults included)."""
    out = {}
    for f in msg.DESCRIPTOR.fields:
        if f.name in skip or f.is_repeated or f.type == f.TYPE_MESSAGE:
            continue
        out[f.name] = getattr(msg, f.name)
    return out


def flatten(contents: MC) -> dict[str, list[dict]]:
    mi = contents.match_info
    mid = mi.match_id
    t: dict[str, list[dict]] = {k: [] for k in (
        "matches", "players", "player_items", "player_stats", "player_gold_sources", "player_ability_stats",
        "deaths", "objectives", "mid_boss", "paths", "damage", "pauses", "pings", "power_up_buffs",
    )}

    t["matches"].append({
        "match_id": mid,
        "start_time": mi.start_time,
        "duration_s": mi.duration_s,
        "winning_team": TEAM.get(mi.winning_team, mi.winning_team),
        "match_outcome": _enum(MC.EMatchOutcome, mi.match_outcome, "k_eOutcome_"),
        "game_mode": _enum(cm.ECitadelGameMode, mi.game_mode, "k_ECitadelGameMode_"),
        "match_mode": _enum(cm.ECitadelMatchMode, mi.match_mode, "k_ECitadelMatchMode_"),
        "game_mode_version": mi.game_mode_version,
        "average_badge_team0": mi.average_badge_team0,
        "average_badge_team1": mi.average_badge_team1,
        "objectives_mask_team0": mi.objectives_mask_team0,
        "objectives_mask_team1": mi.objectives_mask_team1,
        "is_high_skill_range_parties": mi.is_high_skill_range_parties,
        "low_pri_pool": mi.low_pri_pool,
        "new_player_pool": mi.new_player_pool,
        "not_scored": mi.not_scored,
        "ranked_type": _enum(cm.ECitadelRankedType, mi.ranked_type, "k_eCitadelRankedType_"),
        "paths_interval_s": mi.match_paths.interval_s,
    })

    for p in mi.players:
        slot = p.player_slot
        key = {"match_id": mid, "player_slot": slot}
        t["players"].append({
            **key,
            "account_id": p.account_id,
            "team": TEAM.get(p.team, p.team),
            "hero_id": p.hero_id,
            "assigned_lane": p.assigned_lane,
            "kills": p.kills, "deaths": p.deaths, "assists": p.assists,
            "net_worth": p.net_worth, "last_hits": p.last_hits, "denies": p.denies,
            "level": p.level, "ability_points": p.ability_points,
            "abandon_match_time_s": p.abandon_match_time_s,
            "mvp_rank": p.mvp_rank,
            "outcome": _enum(cm.EPlayerMatchOutcome, p.player_match_outcome, "k_EPlayerMatchOutcome_"),
            "won": TEAM.get(p.team, p.team) == TEAM.get(mi.winning_team, mi.winning_team),
        })
        for it in p.items:
            t["player_items"].append({**key, **_scalars(it)})
        for s in p.stats:
            t["player_stats"].append({**key, **_scalars(s)})
            for g in s.gold_sources:
                t["player_gold_sources"].append({
                    **key, "time_stamp_s": s.time_stamp_s,
                    "source": _enum(MC.EGoldSource, g.source, "k_e"),
                    "kills": g.kills, "damage": g.damage, "gold": g.gold, "gold_orbs": g.gold_orbs,
                })
        for a in p.ability_stats:
            t["player_ability_stats"].append({**key, "ability_id": a.ability_id, "value": a.ability_value})
        for d in p.death_details:
            t["deaths"].append({
                **key,
                "game_time_s": d.game_time_s, "time_to_kill_s": d.time_to_kill_s,
                "killer_player_slot": d.killer_player_slot, "death_duration_s": d.death_duration_s,
                "x": d.death_pos.x, "y": d.death_pos.y, "z": d.death_pos.z,
                "killer_x": d.killer_pos.x, "killer_y": d.killer_pos.y, "killer_z": d.killer_pos.z,
            })
        for pg in p.pings:
            t["pings"].append({**key, "game_time_s": pg.game_time_s, "ping_type": pg.ping_type,
                               "ping_data": pg.ping_data})
        for b in p.power_up_buffs:
            for i, ts in enumerate(b.pickup_times_s):
                t["power_up_buffs"].append({
                    **key, "type": b.type, "is_permanent": b.is_permanent, "game_time_s": ts,
                    "stat_value": b.pickup_stat_values[i] if i < len(b.pickup_stat_values) else None,
                })

    for o in mi.objectives:
        t["objectives"].append({
            "match_id": mid,
            "team": TEAM.get(o.team, o.team),  # team that owned the objective
            "objective": _enum(cm.ECitadelTeamObjective, o.team_objective_id, "k_eCitadelTeamObjective_"),
            "destroyed_time_s": o.destroyed_time_s, "first_damage_time_s": o.first_damage_time_s,
            "creep_damage": o.creep_damage, "creep_damage_mitigated": o.creep_damage_mitigated,
            "player_damage": o.player_damage, "player_damage_mitigated": o.player_damage_mitigated,
            "player_spirit_damage": o.player_spirit_damage,
        })
    for b in mi.mid_boss:
        t["mid_boss"].append({
            "match_id": mid, "team_killed": TEAM.get(b.team_killed, b.team_killed),
            "team_claimed": TEAM.get(b.team_claimed, b.team_claimed), "destroyed_time_s": b.destroyed_time_s,
        })
    for pz in mi.match_pauses:
        t["pauses"].append({"match_id": mid, "game_time_s": pz.game_time_s,
                            "pause_duration_s": pz.pause_duration_s, "player_slot": pz.player_slot})

    t["paths"] = _paths(mid, mi.match_paths)
    t["damage"] = _damage(mid, mi.damage_matrix)
    return t


def _paths(mid: int, mp: PATHS) -> list[dict]:
    """1 sample per interval_s per player, positions de-quantized to world units."""
    rows = []
    for p in mp.paths:
        sx = (p.x_max - p.x_min) / (mp.x_resolution or 1)
        sy = (p.y_max - p.y_min) / (mp.y_resolution or 1)
        for i in range(len(p.x_pos)):
            rows.append({
                "match_id": mid, "player_slot": p.player_slot,
                "game_time_s": i * mp.interval_s,
                "x": p.x_min + p.x_pos[i] * sx,
                "y": p.y_min + p.y_pos[i] * sy,
                "health": p.health[i] if i < len(p.health) else None,
                "combat_type": _enum(PATHS.ECombatType, p.combat_type[i], "k_eCombatType_")
                if i < len(p.combat_type) else None,
                "move_type": _enum(PATHS.EMoveType, p.move_type[i], "k_eMoveType_")
                if i < len(p.move_type) else None,
            })
    return rows


def _damage(mid: int, dm: DMG) -> list[dict]:
    """Damage/healing/mitigation per dealer -> target -> source at each sample time.

    Values are as Valve reports them (cumulative up to sample_time_s).
    """
    sd = dm.source_details
    times = list(dm.sample_time_s)
    rows = []
    for dealer in dm.damage_dealers:
        for src in dealer.damage_sources:
            idx = src.source_details_index
            name = sd.source_name[idx] if idx < len(sd.source_name) else None
            stat = _enum(DMG.EStatType, sd.stat_type[idx], "k_eType_") if idx < len(sd.stat_type) else None
            for tgt in src.damage_to_players:
                for i, v in enumerate(tgt.damage):
                    rows.append({
                        "match_id": mid, "dealer_player_slot": dealer.dealer_player_slot,
                        "target_player_slot": tgt.target_player_slot, "source_name": name, "stat_type": stat,
                        "sample_time_s": times[i] if i < len(times) else None, "value": v,
                    })
    return rows
