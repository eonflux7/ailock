"""DuckDB catalog: views over the parquet tables, plus joined convenience views.

The .duckdb file holds only view definitions, so rebuilding it is cheap and
always reflects whatever parquet is on disk.
"""
import duckdb

from . import paths

MATCH_TABLES = [
    "matches", "players", "player_items", "player_stats", "player_gold_sources", "player_ability_stats",
    "deaths", "objectives", "mid_boss", "paths", "damage", "pauses", "pings", "power_up_buffs",
]
KNOWLEDGE_TABLES = ["heroes", "hero_stats", "hero_abilities", "abilities", "ability_properties"]

DAMAGE_TYPE_TOTALS = ("Bullet", "Ability", "Melee", "Misc", "UnknownAbility")

DERIVED = {
    # final cumulative value per dealer/target/source. Valve logs per-damage-type totals
    # (Bullet/Ability/Melee/...) next to per-source rows that sum to the same amount:
    # filter on source_kind to avoid double counting. dealer slot 0 = non-hero (troopers, guardians, ...)
    "damage_final": f"""
        select match_id, dealer_player_slot, target_player_slot, source_name, stat_type,
               max_by(value, sample_time_s) as value,
               case when source_name in {DAMAGE_TYPE_TOTALS} then 'type_total'
                    when source_name like 'Citadel_Graph_CrowdControl_%' then 'crowd_control'
                    when source_name like 'Citadel_Graph_Mitigation_%' then 'mitigation'
                    else 'source' end as source_kind
        from damage group by all
    """,
    # build live when the match started (see knowledge.gametracking.version_at)
    "match_versions": """
        select m.match_id, v.client_version
        from matches m asof join versions v on m.start_time >= epoch(v.released_at)
    """,
    "player_heroes": """
        select p.*, h.name as hero_name, h.class_name as hero_class, mv.client_version
        from players p
        join match_versions mv using (match_id)
        left join heroes h on h.hero_id = p.hero_id and h.client_version = mv.client_version
    """,
    # every purchase/ability unlock with names and costs from the match's own build
    "item_events": """
        select i.match_id, i.player_slot, i.game_time_s, i.sold_time_s, i.item_id, i.upgrade_id,
               i.imbued_ability_id, i.flags, i.upgrade_info,
               a.class_name, a.name, a.type, a.slot, a.tier, a.cost, a.shoppable, a.component_items,
               mv.client_version
        from player_items i
        join match_versions mv using (match_id)
        left join abilities a on a.ability_id = i.item_id and a.client_version = mv.client_version
    """,
    "purchases": """
        select * from item_events where type = 'Item'
    """,
}


def build() -> duckdb.DuckDBPyConnection:
    paths.DATA.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(paths.DB))
    for t in MATCH_TABLES:
        files = paths.TABLES / t
        if any(files.glob("*.parquet")):
            con.execute(f"create or replace view {t} as select * from "
                        f"read_parquet('{files}/*.parquet', union_by_name=true)")
    versions = paths.KNOWLEDGE / "versions.parquet"
    if versions.exists():
        con.execute(f"create or replace view versions as select * from read_parquet('{versions}')")
    for t in KNOWLEDGE_TABLES:
        if any(paths.KNOWLEDGE.glob(f"*/{t}.parquet")):
            con.execute(f"create or replace view {t} as select * from "
                        f"read_parquet('{paths.KNOWLEDGE}/*/{t}.parquet', union_by_name=true)")
    existing = {r[0] for r in con.execute("select view_name from duckdb_views() where not internal").fetchall()}
    if {"matches", "versions", "players", "heroes", "player_items", "abilities"} <= existing:
        for name, sql in DERIVED.items():
            con.execute(f"create or replace view {name} as {sql}")
    return con


def connect(read_only: bool = True) -> duckdb.DuckDBPyConnection:
    if not paths.DB.exists():
        build().close()
    return duckdb.connect(str(paths.DB), read_only=read_only)
