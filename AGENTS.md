# ailock: agent guide

Deadlock match data + version-exact game knowledge + population benchmarks, queryable
through DuckDB. Use this to review a player's matches (builds, timings, net worth flow,
map movement, deaths) with real numbers.

## Quick start

```sh
uv run ailock sync <steam vanity | steamid64 | account_id>   # ingest match history + knowledge
uv run ailock bench                                          # population benchmarks for heroes played
uv run ailock report <match_id> <player>                     # precomputed coaching facts (markdown)
uv run ailock report <match_id> <player> --json              # same, JSON
uv run ailock lane <match_id> <player> [--until 600]          # lane phase facts (compact)
uv run ailock live <hero> -a <allies> -e <enemies> [-p <partner> -o <lane opponents>]  # pre-game brief (/live)
uv run ailock match <match_id> ...                           # ingest specific matches by id
uv run ailock demo <file.dem> ...                            # ingest local replays (e.g. downloaded in-game)
uv run ailock replay <match_id> ...                          # download a replay on demand (don't bulk-download)
uv run ailock sql "select ..."                               # ad-hoc DuckDB query
```

Python: `from ailock.db import connect; connect().sql("...")`. The catalog is `data/ailock.duckdb`.

## How to review a match

1. Start with `ailock report` — it already joins the right game build and benchmarks. Besides net worth,
   purchases, deaths and damage it has:
   - **Farm rate**: souls/min per snapshot window by group (lane, jungle, heroes, objectives, other) vs the hero average.
   - **Purchase gaps**: stretches of 4+ min without buying, what was bought next and its cost, souls earned vs the
     hero average over the same window, and how much sooner the buy lands at average farm. A gap where `earned`
     is close to `cost` means saving for an item (a farm problem), not unspent souls.
   - **Ability flow**: every unlock/upgrade with its ability point cost and the hero's average time for that
     pick, ability point balance per snapshot (earned vs spent; unspent > 0 is either saving for a 5-point T3 or
     wasted), which ability players of this hero max first, damage to enemy heroes by source, and CC dealt by
     kind and target (who your CC actually landed on).
   - **Roster checks**: enemy healing vs healing we prevented, enemy damage type into each of our heroes, CC
     received by kind, each of our heroes' always-on item resists (spirit/bullet/debuff, stacked multiplicatively)
     at 15:00, 25:00 and the end next to the share of spirit damage they took, the anti-heal and cleanse items our
     team bought and when, and which of those players of this hero usually buy.
2. For lane phase questions use `ailock lane`: partner and opponents found from positions (not
   `assigned_lane`), pair-vs-pair economy per snapshot, hero damage traded per 3-min window, positioning per
   minute (advance toward the enemy base, distance to partner and nearest opponent, fighting seconds, time away
   from lane, enemies from other lanes nearby), every lane-phase death with health and who was near, and which
   Tier 1 guardians fell. Prefer it over raw `paths` queries, which are 1 row per player-second.
3. Drill into details with SQL (tables below). Prefer computing from data over game-knowledge recall:
   item/ability stats change every patch; always join on the match's `client_version`.
4. Say what you compared against (benchmark window, hero averages are all-ranks unless synced with badges).

## Live pre-game brief

`/live` (skill in `.agents/skills/live/`, symlinked from `.claude/skills/` for Claude Code) takes the user's hero and both rosters, runs `ailock live` once, decides
(build B1-B10 from the 10 most played current builds, corrections, 1-3 builds of its own with early/mid/late items, optional items, sells and a
point-by-point skill order, threats), writes the decisions to `data/live/plan.json` and runs `ailock live-page`,
which renders `data/live/brief.html` (the agent's builds as tabs, each phase with slots used and souls needed, next
to threats and rosters; the popular build in its author's sections with corrections below; item/ability/hero icons,
Iosevka). `ailock live` saves its facts to `data/live/last.json`, renders a pending page from them (rosters,
popular builds) and starts `ailock live-serve` in the background: a stdlib server on http://localhost:8765
(`AILOCK_LIVE_PORT`) that pushes status (`data/live/status.json`: facts -> deciding -> fixing/ready, or error) and
in-place reloads to the open tab over server-sent events. The browser (Windows under WSL) is opened only when no
tab is watching. Icons come from deadlock-api's asset index (cached weekly).
`ailock live` makes ~16 deadlock-api calls in parallel (cached for the day under
`data/benchmarks/raw/live/`, ~10 s cold, instant warm) and reads the newest knowledge build directly, so it
doesn't need ingested matches or `ailock bench`. It reports: hero win rate; per enemy the matchup win rate,
item spend split (weapon/spirit/vitality), CC/heals/stealth tags, every ability's description and numbers
(with spirit scaling), usual items with buy times and the anti-heal/cleanse they buy; ally synergy and ults; the
most played builds with the author's sections, optional flags and item notes; an item pool per phase
(item-flow-stats: win rate standardized for net worth at buy vs everyone buying in that phase, plus how often
it's later sold or upgraded); roster-adjusted item deltas (significant ones only, centered on the hero's median
item); anti-heal/cleanse/resist options; an item glossary with upgrade paths; my hero's abilities with base
numbers and per-tier bonuses, upgrade orders with T3 timings; soul targets to 45:00, average game length and end
souls; lane matchup stats.
Inventory is 12 slots of any category (checked against held items in match data); an upgrade takes its
component's slot.

## Identity and conventions

- `account_id` = 32-bit Steam id (SteamID64 − 76561197960265728). `player_slot` identifies a player within a match.
- Lanes: `assigned_lane` 1/4/6 = left/middle/right; objective names use Lane1/Lane3/Lane4 for the same lanes
  (reports add a `lane` column). Lanes are separated along x (~ -7500 / 0 / +7500); bases are at y ~ ±10200.
- Match history: deadlock-api's stored history is incomplete. After `ailock steam-login` (one-time QR login with
  the player's own Steam account; token in data/steam/, only sent to Steam), `ailock sync` merges the full
  history from Deadlock's game coordinator (steam/gc.mjs) and gets replay salts from it when deadlock-api has
  none (deadlock-api's own lookup is limited to 3/hour per IP). GC calls refuse to run while the account is in a
  game, since starting a GC session would kick it.
- `team` is 0 or 1. Times are game seconds. Positions are world units (x, y, z).
- Every match table has `match_id`. Join players within a match on (`match_id`, `player_slot`).
- `match_versions` maps each match to the game build (`client_version`) that was live at start.

## Match tables (from Valve match metadata)

| table | grain | notes |
|---|---|---|
| matches | match | start_time (unix), duration_s, winning_team, game_mode, match_mode, avg badges |
| players | player | hero_id, team, assigned_lane, k/d/a, net_worth, last_hits, denies, level, won |
| player_stats | player × ~3 min snapshot | net_worth, gold_* by source, damage, healing, shots, level, ... (cumulative) |
| player_gold_sources | player × snapshot × source | souls by source (Players, LaneCreeps, Neutrals, Bosses, Treasure, ...) |
| player_items | player × event | every item buy and ability unlock/upgrade: game_time_s, item_id, sold_time_s |
| deaths | death | game_time_s, killer_player_slot, time_to_kill_s, death/killer positions |
| paths | player × second | x, y, health (%), combat_type (Out/Player/EnemyNPC/Neutral), move_type |
| damage | dealer × target × source × sample | cumulative value per stat_type (Damage, Healing, Mitigated, ...) |
| objectives | objective | owner team, objective name, destroyed_time_s, damage breakdown |
| mid_boss, pauses, pings, power_up_buffs, player_ability_stats | | |

## Knowledge tables (from game files, per client_version)

| table | notes |
|---|---|
| versions | client_version, released_at, GameTracking commit |
| heroes | hero_id, class_name, name, tags |
| hero_stats | starting stats and per-level gains (`kind` = starting/per_level) |
| hero_abilities | hero's bound abilities by slot (Signature1..4, weapon, melee, ...) |
| abilities | items AND abilities: ability_id (= player_items.item_id), name, type (Item/Signature/Ultimate/Weapon/...), slot (WeaponMod/Armor/Tech = weapon/vitality/spirit), tier, cost, description |
| ability_properties | every property value with its UI label and units; `section` = tooltip section (Innate = small always-on stat, Passive/Active = the item's effect), `provides` = stat it modifies, `usage` (ConditionallyApplied = e.g. applied to a target), `negative` = drawback |

Raw JSON per build also lives at `data/knowledge/<client_version>/{heroes,abilities}.json` (includes upgrades and weapon info).

## Joined views

- `player_heroes`: players + hero names + client_version
- `item_events`: player_items + ability/item data from the match's build
- `purchases`: item_events where type = 'Item' (shop items only)

## Benchmarks (`data/benchmarks/`, from deadlock-api analytics)

Read with `read_parquet('data/benchmarks/<name>/<hero_id>.parquet')`:
- `item_stats`: per item for the hero — wins, matches, win_rate, avg_buy_time_s, avg_sell_time_s
- `performance_curve`: per game_time — net_worth / kills / souls-by-source avg and std
- `ability_max_first`: which ability gets maxed first — matches, win_rate
- `ability_orders`: common full upgrade orders (`order_id`, `position`, `ability_id`, `avg_time_s`, wins, matches);
  the k-th occurrence of an ability is its unlock (k=1) or T1..T3 (k=2..4)
- `build_items`: top build-browser builds (by favorites), item order by category with annotations
- `hero_stats.parquet`, `hero_counters.parquet`: global hero win rates and matchup stats

## Caveats

- Damage: use `damage_final` and filter `source_kind` — `type_total` rows (Bullet/Ability/Melee/...) and
  `source` rows (individual weapons/abilities/items) describe the same damage twice. Dealer slot 0 = non-hero
  (troopers, guardians). `player_stats.player_damage_taken` counts hero damage only (verified equal to the
  matrix's hero `type_total` sum).
- `paths.combat_type` is Valve's combat state, not location: most seconds read `Out` even while laning, so it
  tells you when a player was fighting heroes/NPCs, not whether they were in lane or jungle.
- Anti-heal/cleanse categories come from item properties and descriptions. Resist totals count only Innate/Passive
  tooltip values (not actives like Fury Trance or Metal Skin, nor hero base resists). CC amounts are in the game's
  own units, so compare shares, not absolutes.
- Benchmarks are all ranks unless `ailock bench` was run with `--min-badge/--max-badge` (badge = tier*10 + subrank,
  e.g. 46 = Sentinel 6). Unranked matches report average badge 0.
- Ability upgrades cost 1/2/5 points for T1/T2/T3 (not in the parsed game files; checked against
  `player_stats.ability_points`, which counts points earned). Hero-average pick times come from common orders
  only (>= 50 games each), so rare orders are left out.
- `player_stats` snapshots are every ~3 minutes (finer near the start); `paths` are 1 Hz.
- `unspent_est` in reports = net worth − cost of items held; approximate (ignores partial refunds).
- Version mapping uses tracker commit time; matches started within minutes of a patch may map to the prior build.
- Demos (`data/raw/matches/<id>/demo.dem.bz2`, or `demo.dem` for local replays) are downloaded when Valve still
  has them. `ailock demo` reads a replay's embedded post-match details (same data as Valve's metadata, minus
  positions) and rebuilds `paths` at 1 Hz with the Rust extractor in `parser/` (haste; build with
  `cargo build --release --manifest-path parser/Cargo.toml`). Demo-derived paths have `combat_type` =
  'Unknown', so the lane tool shows `fighting_s` as `-`; health comes from the controller (current/max).
