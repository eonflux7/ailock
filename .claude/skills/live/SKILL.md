---
name: live
description: Pre-game Deadlock brief while a match loads. Use when the user gives their hero plus ally/enemy roster (e.g. "/live haze; allies abrams seven ivy dynamo geist; enemies mo infernus vindicta paradox bebop wraith; lane dynamo vs bebop wraith") and wants a build, item corrections, skill order and a game plan fast.
---

# Live pre-game brief

The user reads this while the match starts, so skip exploration and polish, but not thinking: the decisions are
the product. You make them (build choice, corrections, your own phased items and skill order) from the facts the
helper gathers; the helper decides nothing. The result is an HTML page with icons, served on localhost and
opened by `ailock live` (it shows your progress and fills in when you render the plan), plus a short text brief.

1. Parse the message: my hero, up to 5 allies, up to 6 enemies, optionally lane partner and lane opponents
   ("lane X vs Y Z"). Names are fuzzy (prefixes, `mo`, `geist`, `talon`, `danny`, `nurse`, `rat`, `mcg`). Don't
   ask about missing parts: run with what was given.
2. Run ONE command (no other exploration first):
   ```sh
   uv run ailock live <hero> -a <allies...> -e <enemies...> [-p <partner> -o <opponents...>] [--min-badge N]
   ```
   On an ambiguous/unknown name it exits with the candidates: fix the name and rerun.
3. Decide (see "How to decide") and write `data/live/plan.json` with the Write tool:
   ```json
   {"build": {"tag": "B1", "why": "<one line: why this build vs this roster>"},
    "fixes": [{"add": "<item>", "instead": "<item in the build, optional>", "why": "<tied to an enemy/fact>"},
              {"cut": "<item in the build>", "why": "..."}],
    "claude": [
      {"name": "<short name>", "when": "<default, or the game state this build is for>",
       "early": {"items": ["<in buy order>"], "plan": "<lane plan, soul target @9:00>"},
       "mid":   {"items": [...], "plan": "<spike timing, fight/farm plan>"},
       "late":  {"items": [...], "plan": "<win condition>"},
       "optional": [{"item": "<item>", "when": "<trigger: enemy item/hero ahead/...>"}],
       "sell": [{"item": "<held item>", "for": "<item whose purchase needs the slot>", "why": "..."}],
       "skills": ["<ability>", "..."],
       "skills_why": "<what you changed from the popular order and why>"}],
    "watch": ["<enemy threat: hard cc/ult/stealth/heal with numbers, and the answer to it>"]}
   ```
   `claude` holds 1-3 builds; the first is the main one. Add a second (or third) only when a different
   build genuinely fits this roster or a likely game state (e.g. a spirit-heavy enemy getting ahead, a
   different damage route the data supports); don't pad. Alternatives may omit `skills` to reuse the
   main build's. `optional` and `sell` are optional too: use them when the roster calls for situational
   items or the build runs past 12 slots. Phases are early 0-9m, mid 9-20m, late 20m+ (the helper's item-flow
   phases). `skills` has one entry per ability point in order (the 1st entry of an ability = unlock, then T1,
   T2, T3), like the helper's `points:` lines, about 15-16 entries. An "add" of an item the build already has
   means "prioritize it" and is highlighted in place.
4. Run `uv run ailock live-page` (renders `data/live/brief.html`; the open page reloads in place). If it prints
   `warning: unknown item/ability`, fix the name; on `no free slot for X`, add a sell (or drop an item) and rerun.
   The page shows each phase's slots used, souls needed and when the average player has them.
5. Reply with this short text brief and nothing else (the page has the detail):

```
BUILD  B<n> <name> (id <id>) — <wr>/<games> — <why>
FIX    + <item> (instead of <item>): <why> | - <item>: <why>          (2-4 entries)
SKILLS popular: <unlock order>; max <A > B > C> | mine: max <A > B > C> — <why it differs>
EARLY  <items> | <lane plan>
MID    <items> (spike ~mm:ss) | <plan>
LATE   <items> | <win condition>
SELL   <item> for <item>                                              (only if any)
OPT    <item> if <trigger>; ...                                       (only if any)
ALT    <name>: <when> — <how it differs>                              (one line per extra build)
WATCH  <top 2 threats + answer>
```

## How to decide

- **Read the room first**: before picking anything, work out from the facts: what the enemy team wins with (who
  carries, damage lean, which ults/cc stop your key ability, who heals and how much), what your team lacks
  (frontline, anti-heal, cc, wave clear) and what your hero's job is in this game. Every later choice should
  trace back to this.
- **Build (pick one of B1-B10)**: the candidates are the 10 most played current builds (with their last update).
  Compare them all, not just the top one: where they agree (items most builds carry, their order, the sections
  marked optional) is the consensus core; where they differ is a choice to make for this roster. Start from win
  rate and games, then check fit. Do its souls split and defensive picks match the enemy damage lean (enemy
  `W/S/V` spend → bullet vs spirit resist)? Does it already carry the anti-heal or cleanse the roster needs
  (`has ...`)? Are its core items flagged Δ- into these enemies? A build last updated before the current patch
  may list items that changed. Prefer the highest win rate unless another candidate clearly fits better, and say
  why. Your own builds can borrow items and ideas from any candidate (say which).
- **Corrections (fixes)**: swap or add items in the chosen build for this roster. Evidence in order: significant
  roster Δ (does better / does worse / per-enemy standouts), then enemy kits (hard cc → cleanse/debuff resist;
  heals or lifesteal items in `buys` → anti-heal; damage lean → resist type), then the glossary numbers.
  An empty `fixes` list is fine when nothing calls for one.
- **Your phased items**: your own order built from the chosen build plus corrections. Use the item pool by
  phase for what to buy when: `pick` is the share of players reaching that phase who bought it then, `adj` is
  its win rate standardized for the buyer's net worth, minus everyone buying in that phase (so late items are
  not flattered just for showing up in long games), `@Nk` the average souls when bought. Prefer the
  build's items, and use `adj` to choose between them; don't chase a single big `adj` on a small pick. The
  candidate builds' authors' notes (in quotes) and section names (optional/situational) carry expert intent:
  imbue targets, what to sell, when an item is worth it. Name only items that appear in the helper output (it
  reflects the current patch) and use the glossary for what each item does.
- **Slots and souls**: 12 item slots of any kind. An upgrade (glossary `<-`) takes its component's slot
  and costs the difference, so plan components that upgrade (Extra Health -> Fortitude) and count final slots.
  When the list passes 12, add `sell` entries: early items players sell (`sold N%@mm:ss` in the pool; this
  also counts components consumed by an upgrade) go first. Check costs against `avg souls` and `games last ...
  end at ...k souls`: items past the average end souls are a priority order, so put the ones that matter
  most into this roster first.
- **Optional items**: situational answers with a concrete trigger ("Spirit Resilience if Mo/Bebop are ahead",
  "Inhibitor when Wraith has Mercurial Magnum"), taken from enemy kits, their usual buys and buy times
  (`buys ...@mm:ss`) and the counters section. They don't count toward slots: say what they replace in `when`.
- **Your skill order**: start from the popular orders (`points:` lines, max-first shares), then adjust for the
  roster using each ability's description, base numbers (`+x/spirit` = spirit scaling, so spirit items grow it)
  and T1/T2/T3 bonuses. Examples: a T3 that dispels or grants evasion against heavy cc or burst; a slow or
  damage T1 early against a lane bully; an earlier ult point against a team that groups up.
- **Enemy kits**: tags (`cc`, `heals`, `stealth`) are quick hints; `(text)` tags come from the description.
  Read the ability lines for what each one does (a channel that holds you, a swap, a time-stop) and the
  numbers (cooldown, duration) to time fights and pick answers (cleanse vs debuff resist vs spirit resist).
- **Roster Δ**: an item's win rate into this roster relative to the hero's other items. It is shown only when
  significant and is usually 0.5-3 points. "none significant" is normal: then lean on the kit facts, not noise.
- **Enemies**: `me vs` = my hero's win rate with that enemy on the other team (shift vs my average). `W/S/V` =
  share of their item souls in weapon/spirit/vitality, a proxy for their damage type. `buys` is what they
  usually build with average buy times (their spikes); `counters they buy` = anti-heal/cleanse they often get.
  For allies, `buys` shows the anti-heal/cleanse they often get, so you don't have to.
- **Lane**: win rate and souls difference at 15:00 for the exact 2v2 and for my hero against each opponent.
- Data covers all ranks over the last 30 days unless `--min-badge/--max-badge` was passed. Game knowledge is the
  newest synced build (in the header). If it looks stale after a patch, say so in one line.
