"""Turn a knowledge snapshot (JSON) into parquet tables keyed by client_version."""
import json

from .. import paths
from ..storage import write_rows
from . import gametracking as gt


def write_versions(vs: list[gt.Version]) -> None:
    write_rows(paths.KNOWLEDGE / "versions.parquet", [
        {"client_version": v.client_version, "commit": v.commit, "released_at": v.committed_at} for v in vs
    ])


def write_snapshot_tables(client_version: int) -> None:
    d = paths.KNOWLEDGE / str(client_version)
    heroes = json.loads((d / "heroes.json").read_text())
    abilities = json.loads((d / "abilities.json").read_text())
    cv = {"client_version": client_version}

    write_rows(d / "heroes.parquet", [
        {**cv, "hero_id": h["id"], "class_name": h["class_name"], "name": h["name"], "disabled": h["disabled"],
         "in_development": h["in_development"], "tags": h["tags"]}
        for h in heroes.values()
    ])
    write_rows(d / "hero_stats.parquet", [
        {**cv, "hero_id": h["id"], "kind": kind, "stat": k, "value": _num(v)}
        for h in heroes.values()
        for kind, stats in (("starting", h["starting_stats"]), ("per_level", h["level_up"]))
        for k, v in stats.items()
    ])
    write_rows(d / "hero_abilities.parquet", [
        {**cv, "hero_id": h["id"], "slot": slot, "ability_class": cls,
         "ability_id": abilities.get(cls, {}).get("id"), "ability_name": abilities.get(cls, {}).get("name")}
        for h in heroes.values() for slot, cls in h["abilities"].items()
    ])
    write_rows(d / "abilities.parquet", [
        {**cv, "ability_id": a["id"], "class_name": a["class_name"], "name": a["name"], "type": a["type"],
         "slot": a["slot"], "tier": a["tier"], "cost": a["cost"], "shoppable": a["shoppable"],
         "disabled": a["disabled"], "component_items": a["component_items"], "description": a["description"]}
        for a in abilities.values()
    ])
    write_rows(d / "ability_properties.parquet", [
        {**cv, "ability_id": a["id"], "property": k, "value": None if p.get("value") is None else str(p.get("value")), "value_num": _num(p.get("value")),
         "label": p.get("label"), "units": p.get("units"), "section": p.get("section"),
         "provides": p.get("provides"), "usage": p.get("usage"), "negative": p.get("negative")}
        for a in abilities.values() for k, p in a["properties"].items()
    ])


def _num(v):
    try:
        return float(str(v).rstrip("m").rstrip("s"))
    except (TypeError, ValueError):
        return None
