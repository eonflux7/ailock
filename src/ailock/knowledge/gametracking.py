"""Version-exact game data from SteamDatabase/GameTracking-Deadlock.

That repo commits every Deadlock update, including decompiled scripts/*.vdata
(heroes, abilities incl. shop items, generic data) and localization. Each commit
touching game/citadel/steam.inf is one client version, so for any match we can
pick the build that was live when it started and read the data as of that commit.

Snapshots are parsed once per client version into data/knowledge/<version>/.
"""
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import keyvalues3 as kv3

from .. import paths
from .hashing import ability_id

STEAM_INF = "game/citadel/steam.inf"
SCRIPTS = "game/citadel/pak01_dir/scripts"
LOCALIZATION = [
    "citadel_gc_hero_names", "citadel_gc_mod_names", "citadel_mods", "citadel_heroes", "citadel_attributes",
    "citadel_gc", "citadel_main",
]
REPO_URL = "https://github.com/SteamDatabase/GameTracking-Deadlock.git"


@dataclass(frozen=True)
class Version:
    client_version: int
    commit: str
    committed_at: datetime


def _git(*args: str, repo: Path | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo or paths.GAMETRACKING), *args], check=True,
                          capture_output=True, text=True).stdout


def ensure_repo(update: bool = True) -> None:
    """Blobless clone: full history, file contents fetched lazily on `git show`."""
    repo = paths.GAMETRACKING
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", REPO_URL, str(repo)], check=True)
    elif update:
        _git("fetch", "-q", "origin")
        _git("update-ref", "HEAD", "origin/HEAD")


def versions() -> list[Version]:
    """All tracked client versions, oldest first."""
    out = []
    for line in _git("log", "--format=%H %cI %s", "HEAD", "--", STEAM_INF).splitlines():
        commit, ts, subject = line.split(" ", 2)
        m = re.match(r"(\d+)\b", subject)
        if m:
            out.append(Version(int(m.group(1)), commit, datetime.fromisoformat(ts)))
    # several commits can carry one version (e.g. server-only updates); keep the first
    seen: dict[int, Version] = {}
    for v in sorted(out, key=lambda v: v.committed_at):
        seen.setdefault(v.client_version, v)
    return list(seen.values())


def version_at(unix_ts: int, vs: list[Version] | None = None) -> Version:
    """Build that was live at `unix_ts`. Tracker commits land minutes after a release,
    so matches started in that window resolve to the previous build."""
    vs = vs or versions()
    live = [v for v in vs if v.committed_at.timestamp() <= unix_ts]
    return live[-1] if live else vs[0]


def _show(commit: str, path: str) -> str | None:
    try:
        return _git("show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


def _kv3(text: str) -> dict:
    return kv3.textreader.KV3TextReader().parse(text).value


def _localization(commit: str) -> dict[str, str]:
    strings: dict[str, str] = {}
    pat = re.compile(r'^\s*"([^"]+)"\s+"((?:[^"\\]|\\.)*)"', re.M)
    for name in LOCALIZATION:
        text = _show(commit, f"game/citadel/resource/localization/{name}/{name}_english.txt")
        if not text:
            continue
        for k, v in pat.findall(text):
            strings.setdefault(k.split(":")[0], v.replace('\\"', '"').replace("\\n", "\n"))
    return strings


def _plain(s: str | None) -> str | None:
    return re.sub(r"<[^>]+>", "", s).strip() if s else s


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (str, int, float, bool)) or o is None:
        return o
    return str(o)


# bump when the snapshot JSON gains fields; older snapshots are rebuilt on the next sync
SNAPSHOT_FORMAT = 2


def snapshot_current(out: Path) -> bool:
    meta = out / "meta.json"
    return meta.exists() and json.loads(meta.read_text()).get("format", 1) >= SNAPSHOT_FORMAT


def build_snapshot(v: Version, force: bool = False) -> Path:
    """Parse one client version into data/knowledge/<version>/{heroes,abilities,meta}.json."""
    out = paths.KNOWLEDGE / str(v.client_version)
    if snapshot_current(out) and not force:
        return out
    out.mkdir(parents=True, exist_ok=True)
    # most builds don't touch game data; reuse a snapshot parsed from identical files
    sources = _source_blobs(v.commit)
    for other in paths.KNOWLEDGE.glob("*/meta.json"):
        meta = json.loads(other.read_text())
        if meta.get("source_blobs") == sources and other.parent != out and snapshot_current(other.parent):
            for f in ("heroes.json", "abilities.json"):
                shutil.copyfile(other.parent / f, out / f)
            meta.update(client_version=v.client_version, commit=v.commit, committed_at=v.committed_at.isoformat(),
                        same_data_as=meta.get("same_data_as") or meta["client_version"])
            (out / "meta.json").write_text(json.dumps(meta, indent=1))
            return out
    loc = _localization(v.commit)
    raw_heroes = _kv3(_show(v.commit, f"{SCRIPTS}/heroes.vdata"))
    raw_abilities = _kv3(_show(v.commit, f"{SCRIPTS}/abilities.vdata"))
    generic = _kv3(_show(v.commit, f"{SCRIPTS}/generic_data.vdata") or "{}")
    tier_price = list(generic.get("m_nItemPricePerTier") or [])

    abilities = {}
    for cls, a in raw_abilities.items():
        if not isinstance(a, dict) or not cls or cls.startswith("generic_data"):
            continue
        tier = a.get("m_iItemTier")
        sections = _tooltip_sections(a)
        tier_n = int(str(tier).rsplit("_", 1)[-1]) if tier else None
        abilities[cls] = {
            "id": ability_id(cls),
            "class_name": cls,
            "name": loc.get(cls),
            "description": _plain(loc.get(f"{cls}_desc")),
            "type": (a.get("m_eAbilityType") or "").removeprefix("EAbilityType_") or None,
            "slot": (a.get("m_eItemSlotType") or "").removeprefix("EItemSlotType_") or None,
            "tier": tier_n,
            "cost": tier_price[tier_n] if tier_n is not None and tier_n < len(tier_price) else None,
            "disabled": bool(a.get("m_bDisabled")),
            "shoppable": a.get("m_eAbilityType") == "EAbilityType_Item" and not a.get("m_bDisabled")
            and bool(a.get("m_strShopIconLarge") or a.get("m_strAbilityImage")),
            "component_items": list(a.get("m_vecComponentItems") or []),
            "properties": {k: {**_prop(k, p, loc), "section": sections.get(k)}
                           for k, p in (a.get("m_mapAbilityProperties") or {}).items()},
            "upgrades": [
                [{"property": u.get("m_strPropertyName"), "bonus": u.get("m_strBonus"),
                  "type": u.get("m_eUpgradeType")} for u in (tier_up.get("m_vecPropertyUpgrades") or [])]
                for tier_up in (a.get("m_vecAbilityUpgrades") or [])
            ],
            "weapon": _jsonable(a.get("m_WeaponInfo")) if a.get("m_WeaponInfo") else None,
        }

    heroes = {}
    for cls, h in raw_heroes.items():
        if not isinstance(h, dict) or "m_HeroID" not in h:
            continue
        bound = {k.removeprefix("ESlot_"): v for k, v in (h.get("m_mapBoundAbilities") or {}).items()}
        heroes[cls] = {
            "id": h["m_HeroID"],
            "class_name": cls,
            "name": loc.get(cls),
            "disabled": bool(h.get("m_bDisabled")),
            "in_development": bool(h.get("m_bInDevelopment")),
            "abilities": bound,
            "starting_stats": _jsonable(h.get("m_mapStartingStats") or {}),
            "level_up": _jsonable(h.get("m_mapStandardLevelUpUpgrades") or {}),
            "scaling_stats": _jsonable(h.get("m_mapScalingStats") or {}),
            "levels": {int(k): {"required_souls": lv.get("m_unRequiredGold"),
                                "bonus": _jsonable(lv.get("m_mapBonusCurrencies") or {})}
                       for k, lv in (h.get("m_mapLevelInfo") or {}).items()},
            "tags": [loc[t] for i in (1, 2, 3)
                     if (t := f"Citadel_{cls.removeprefix('hero_').title()}_HeroTag_{i}") in loc],
        }

    (out / "heroes.json").write_text(json.dumps(heroes, indent=1))
    (out / "abilities.json").write_text(json.dumps(abilities, indent=1))
    (out / "meta.json").write_text(json.dumps({
        "client_version": v.client_version, "commit": v.commit, "committed_at": v.committed_at.isoformat(),
        "item_price_per_tier": tier_price, "source_blobs": sources, "format": SNAPSHOT_FORMAT,
    }, indent=1))
    return out


def _source_blobs(commit: str) -> dict[str, str]:
    files = [f"{SCRIPTS}/{n}.vdata" for n in ("heroes", "abilities", "generic_data")] + [
        f"game/citadel/resource/localization/{n}/{n}_english.txt" for n in LOCALIZATION]
    out = _git("ls-tree", commit, "--", *files)
    return {line.split("\t")[1].rsplit("/", 1)[-1]: line.split()[2] for line in out.splitlines()}


def _tooltip_sections(a: dict) -> dict[str, str]:
    """property -> tooltip section it's shown in (Innate = small always-on stat, Passive/Active = the item's effect)."""
    out: dict[str, str] = {}
    for sec in a.get("m_vecTooltipSectionInfo") or []:
        kind = (sec.get("m_eAbilitySectionType") or "").removeprefix("EArea_") or None
        for attr in sec.get("m_vecSectionAttributes") or []:
            for key in ("m_vecAbilityProperties", "m_vecElevatedAbilityProperties"):
                for prop in attr.get(key) or []:
                    out.setdefault(prop, kind)
    return out


def _prop(key: str, p: Any, loc: dict[str, str]) -> dict:
    if not isinstance(p, dict):
        return {"value": _jsonable(p)}
    scale = p.get("m_subclassScaleFunction")
    return {
        "value": p.get("m_strValue"),
        "label": loc.get(f"{key}_label") or loc.get(key),
        "units": (p.get("m_eDisplayUnits") or "").removeprefix("EDisplayUnit_") or None,
        "scale": _jsonable(scale) if scale else None,
        # what the value modifies, and whether it's always on, conditional (e.g. applied to a target) or a drawback
        "provides": (p.get("m_eProvidedPropertyType") or "").removeprefix("MODIFIER_VALUE_") or None,
        "usage": p.get("m_eStatsUsageFlags") or None,
        "negative": bool(p.get("m_bIsNegativeAttribute")),
    }
