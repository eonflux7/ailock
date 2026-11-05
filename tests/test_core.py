import os

import pytest

from ailock.cli import resolve_account
from ailock.knowledge.hashing import ability_id
from ailock.paths import match_dir

# optional: any ingested match id, to run the metadata test against real data
FIXTURE_MATCH = int(os.environ.get("AILOCK_FIXTURE_MATCH", 0))


def test_ability_id_matches_valve_hash():
    # ids as they appear in match metadata
    assert ability_id("drifter_blood_blast") == 3120550633
    assert ability_id("upgrade_toxic_bullets") == 3696726732


def test_resolve_account_numeric_forms():
    assert resolve_account("12345") == 12345
    assert resolve_account("76561197960278073") == 12345
    assert resolve_account("https://steamcommunity.com/profiles/76561197960278073/") == 12345


def test_live_resolve_hero():
    from ailock.live import resolve_hero

    heroes = {1: "Infernus", 4: "Lady Geist", 18: "Mo & Krill", 17: "Grey Talon", 76: "Graves", 2: "Seven"}
    assert resolve_hero("infernus", heroes) == 1
    assert resolve_hero("18", heroes) == 18
    assert resolve_hero("mo", heroes) == 18  # alias
    assert resolve_hero("Lady-Geist", heroes) == 4
    assert resolve_hero("talon", heroes) == 17
    assert resolve_hero("sev", heroes) == 2  # unique prefix
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_hero("gr", heroes)
    with pytest.raises(ValueError, match="unknown"):
        resolve_hero("nobody", heroes)


def test_live_wilson_and_upgrades():
    from ailock.live import _upgrade_text, _wilson_lb

    assert _wilson_lb(0, 0) == 0
    assert _wilson_lb(55, 100) < _wilson_lb(5500, 10000) < 0.55
    ab = {"properties": {"AbilityCooldown": {"label": "Cooldown"}},
          "upgrades": [[{"property": "AbilityCooldown", "bonus": "-20"}], [{"property": "Radius", "bonus": "3m"}]]}
    assert _upgrade_text(ab) == ["Cooldown -20", "Radius +3m"]
    ab["upgrades"] = [[{"property": "BulletResistReductionDuration", "bonus": "6"},
                       {"property": "AbilityCooldown", "bonus": 0.8, "type": "EAddToScale"}]]
    assert _upgrade_text(ab) == ["Bullet Resist Reduction Duration +6, Cooldown +0.8/spirit"]


def test_live_spirit_scaling():
    from ailock.live import _scale

    s = "subclass:{'m_eSpecificStatScaleType': 'ETechPower', 'm_flStatScale': 0.03}"
    assert _scale({"scale": s}) == 0.03
    assert _scale({"scale": "subclass:{'m_eSpecificStatScaleType': 'ETechCooldown'}"}) is None
    assert _scale({"scale": None}) is None


def test_live_page_marks_fixes_and_unknown_names():
    from ailock.live_page import Page

    def row(i, name, pick, buy_s):
        return {"item_id": i, "name": name, "pick": pick, "buy_s": buy_s, "cost": 800, "kind": "weapon"}

    facts = {
        "hero": {"hero_id": 13, "name": "Haze", "win_rate": 0.54}, "version": {"client_version": 1, "released_at": "x"},
        "window": {"days": 30}, "enemies": [], "allies": [], "lane": None, "nw_targets": None,
        "catalog": {"Rapid Rounds": {"item_id": 1, "cost": 800, "kind": "weapon", "buy_s": 240},
                    "Healbane": {"item_id": 2, "cost": 1600, "kind": "vitality", "buy_s": 1300},
                    "Ricochet": {"item_id": 3, "cost": 6400, "kind": "weapon", "buy_s": 1440}},
        "glossary": [], "icons": {"items": {}, "heroes": {}},
        "builds": [{"tag": "B1", "name": "b", "hero_build_id": 9, "win_rate": 0.5, "matches": 10,
                    "categories": [{"category": "x", "items": [row(1, "Rapid Rounds", 0.9, 240),
                                                               row(3, "Ricochet", 0.7, 1440)]}]}],
        "skills": {"abilities": [{"slot": 1, "name": "Sleep Dagger", "ability_id": 5, "upgrades": ["a", "b", "c"]}],
                   "orders": []},
    }
    plan = {"build": {"tag": "B1"}, "fixes": [{"add": "Healbane", "why": "heals"}, {"cut": "Ricochet", "why": "x"}],
            "claude": {"early": {"items": ["Rapid Rounds", "Made Up Item"]}, "skills": ["Sleep Dagger", "Sleep Dagger"]}}
    p = Page(facts, plan)
    out = p.render()
    assert 'class="item weapon cut"' in out and 'class="item vitality add"' in out
    assert "unknown item 'Made Up Item'" in p.warnings
    assert ">T1<" in out  # second point into an ability is its T1
    pending = Page(facts, {}).render()  # before the agent's plan: rosters and popular builds, Claude column pending
    assert "Claude is building your plan" in pending and "candidate builds" in pending


def test_live_page_slots_upgrades_and_sells():
    from ailock.live_page import Page

    names = [f"Item{i}" for i in range(12)]
    catalog = {n: {"item_id": i, "cost": 800, "kind": "weapon", "buy_s": 0, "components": []} for i, n in enumerate(names)}
    catalog["Big"] = {"item_id": 50, "cost": 3200, "kind": "weapon", "buy_s": 0, "components": ["Item0"]}
    catalog["Late"] = {"item_id": 51, "cost": 6400, "kind": "weapon", "buy_s": 0, "components": []}
    facts = {"catalog": catalog, "glossary": [], "icons": {}, "skills": {"abilities": [], "orders": []},
             "nw_targets": {"600": 6000, "1200": 18000}}
    build = {"name": "x", "early": {"items": names}, "mid": {"items": ["Big"]}, "late": {"items": ["Late"]}}
    p = Page(facts, {"claude": [build]})
    s = p.slots(build)
    assert [len(x["held"]) for x in s] == [12, 12, 13]  # Big replaces its component; Late has no slot
    assert s[1]["held"][0] == "Big" and s[0]["at"] == "13:00"
    assert any("no free slot for Late" in w for w in p.warnings)

    p = Page(facts, {})
    build["sell"] = [{"item": "Item5", "for": "Late"}]
    s = p.slots(build)
    assert len(s[2]["held"]) == 12 and "Item5" not in s[2]["held"] and not p.warnings
    assert s[2]["worth"] == 3200 + 10 * 800 + 6400 + 400  # half of the sold item is lost


@pytest.mark.skipif(not (match_dir(FIXTURE_MATCH) / "meta.zst").exists(), reason="AILOCK_FIXTURE_MATCH not set or not downloaded")
def test_flatten_metadata():
    from ailock.metadata import flatten
    from ailock.sources.valve import parse_metadata

    t = flatten(parse_metadata((match_dir(FIXTURE_MATCH) / "meta.zst").read_bytes()))
    assert len(t["players"]) == 12
    assert t["matches"][0]["duration_s"] > 0
    # 1 Hz paths for every player, positions inside the map bounds
    assert len({r["player_slot"] for r in t["paths"]}) == 12
    assert all(-20000 < r["x"] < 20000 and -20000 < r["y"] < 20000 for r in t["paths"])
    # damage matrix values are cumulative
    series: dict = {}
    for r in t["damage"]:
        series.setdefault((r["dealer_player_slot"], r["target_player_slot"], r["source_name"], r["stat_type"]),
                          []).append(r["value"])
    assert all(a <= b for v in series.values() for a, b in zip(v, v[1:]))
