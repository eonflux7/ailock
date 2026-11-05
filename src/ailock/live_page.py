"""HTML brief for /live: the agent's own builds next to the threats and rosters, then the popular build with the
agent's corrections.

`ailock live` saves its facts to data/live/last.json and renders a pending page from them (rosters, popular
build, candidates) while the agent decides. The agent writes its decisions to a plan JSON:

    {"build": {"tag": "B1", "why": "..."},
     "fixes": [{"add": "Debuff Reducer", "instead": "Titanic Magazine", "why": "..."},
               {"cut": "Armor Piercer", "why": "..."}],
     "claude": [                                   # 1-3 builds, the first is the main one (a single dict also works)
        {"name": "...", "when": "...",
         "early": {"items": ["..."], "plan": "..."}, "mid": {...}, "late": {...},
         "optional": [{"item": "...", "when": "..."}],              # situational, not counted in slots
         "sell": [{"item": "...", "for": "<item bought>", "why": "..."}],
         "skills": ["Fixation", "Sleep Dagger", ...],   # one entry per ability point; omitted = the first build's
         "skills_why": "..."}],
     "watch": ["..."]}

and `ailock live-page plan.json` renders data/live/brief.html with item/ability/hero icons. The page is served
by `ailock live-serve` (live_server.py), which reloads it in place and shows the pipeline status written here
to data/live/status.json. Names are checked against the build's own data; unknown ones are reported instead of
silently dropped.
"""
import datetime
import html
import json
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import paths
from .live import SLOTS

LIVE_DIR = paths.DATA / "live"
LAST = LIVE_DIR / "last.json"
PAGE = LIVE_DIR / "brief.html"
STATUS = LIVE_DIR / "status.json"
# the agent's phases, on item-flow-stats' boundaries (its 20-30m and 30m+ columns are both "late")
PHASES = [("early", "Early", "0-9m", 0, 540), ("mid", "Mid", "9-20m", 540, 1200), ("late", "Late", "20m+", 1200, 10 ** 6)]
TIERS = ["unlock", "T1", "T2", "T3"]
FONTS = "".join(f'<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fontsource/iosevka@5.3.0/latin-{w}.css">'
                for w in (400, 500, 700))


def save_facts(facts: dict) -> Path:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    LAST.write_text(json.dumps(facts, default=str))
    return LAST


def set_status(stage: str, message: str, **extra) -> None:
    """Pipeline status for the served page: facts -> deciding -> fixing/ready (or error). A "facts" stage starts
    a new run and its clock; the others keep it."""
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    try:
        prev = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        prev = {}
    started = now if stage == "facts" or "started" not in prev else prev["started"]
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stage": stage, "message": message, "started": started, "t": now, **extra}))
    tmp.replace(STATUS)


def new_run() -> None:
    """A new game: drop the last brief (the server shows a status page until the facts are in) and set the old
    plan aside, so it is neither shown with the new roster nor overwritten unread."""
    PAGE.unlink(missing_ok=True)
    plan = LIVE_DIR / "plan.json"
    if plan.exists():
        plan.replace(LIVE_DIR / "plan.prev.json")


def _e(s: Any) -> str:
    return html.escape("" if s is None else str(s))


def _mmss(s) -> str:
    return "" if s is None else f"{int(s) // 60}:{int(s) % 60:02d}"


def _tip(s: str) -> str:
    return f' data-tip="{_e(s)}"' if s else ""


class Page:
    def __init__(self, facts: dict, plan: dict):
        self.f, self.plan, self.warnings, self.no_icon = facts, plan, [], []
        self.catalog = {k.lower(): {"name": k, **v} for k, v in facts["catalog"].items()}
        self.gloss = {g["name"]: g for g in facts["glossary"]}
        self.abilities = {a["name"].lower(): a for a in facts["skills"]["abilities"]}
        self.icons = facts.get("icons") or {}

    # --- lookups
    def item(self, name: str) -> dict | None:
        it = self.catalog.get((name or "").strip().lower())
        if not it:
            self.warnings.append(f"unknown item {name!r}")
        return it

    def ability(self, name: str) -> dict | None:
        a = self.abilities.get((name or "").strip().lower())
        if not a:
            self.warnings.append(f"unknown ability {name!r}")
        return a

    def icon(self, kind: str, id_: int, name: str) -> str | None:
        src = self.icons.get(kind, {}).get(str(id_))
        if not src:
            self.no_icon.append(name)
        return src

    def item_icon(self, name: str, cls: str = "", note: str = "") -> str:
        it = self.item(name)
        if not it:
            return f'<div class="item bad"{_tip("not in this game build")}><span class="name">{_e(name)}</span></div>'
        g = self.gloss.get(it["name"])
        tip = f"{it['name']} · {it['cost']}" + (f"\n{'; '.join(g['innate'] + g['effect'])}\n{g['text']}" if g else "")
        if note:
            tip += f"\n\n{note}"
        src = self.icon("items", it["item_id"], it["name"])
        img = f'<img src="{_e(src)}" alt="" loading="lazy">' if src else '<div class="noimg"></div>'
        return (f'<div class="item {it["kind"] or ""} {cls}"{_tip(tip)}>{img}'
                f'<span class="name">{_e(it["name"])}</span><span class="cost">{_e(it["cost"])}</span></div>')

    def hero_icon(self, hero_id: int, cls: str = "hero") -> str:
        src = self.icon("heroes", hero_id, f"hero {hero_id}")
        return f'<img class="{cls}" src="{_e(src)}" alt="">' if src else f'<div class="{cls}"></div>'

    def skill_row(self, seq: list[dict]) -> str:
        """seq: [{"ability": name, "t": "mm:ss" or ""}] in point order."""
        seen: dict[str, int] = {}
        cells = []
        for i, s in enumerate(seq):
            a = self.ability(s["ability"])
            key = a["name"] if a else s["ability"]
            k = seen[key] = seen.get(key, 0) + 1
            src = self.icon("items", a["ability_id"], a["name"]) if a else None
            tip = f"#{i + 1} {key} {TIERS[k - 1] if k <= 4 else '?'}"
            if a and 2 <= k <= 4 and k - 2 < len(a["upgrades"]):
                tip += f"\n{a['upgrades'][k - 2]}"
            img = f'<img src="{_e(src)}" alt="">' if src else f"<b>{_e(key[:3])}</b>"
            slot = f" s{a['slot']}" if a else ""
            t = f'<em>{_e(s["t"])}</em>' if s.get("t") else ""
            cells.append(f'<div class="pt{slot}{" t3" if k == 4 else ""}"{_tip(tip)}><i>{i + 1}</i>{img}'
                         f'<span>{TIERS[k - 1] if k <= 4 else "?"}</span>{t}</div>')
        return f'<div class="skills">{"".join(cells)}</div>'

    def skill_legend(self) -> str:
        cells = []
        for a in self.f["skills"]["abilities"]:
            src = self.icon("items", a["ability_id"], a["name"])
            tip = a["name"] + "".join(f"\n{TIERS[i + 1]}: {u}" for i, u in enumerate(a["upgrades"]))
            img = f'<img src="{_e(src)}" alt="">' if src else ""
            cells.append(f'<div class="leg s{a["slot"]}"{_tip(tip)}>{img}<span><kbd>{a["slot"]}</kbd> {_e(a["name"])}'
                         f'</span></div>')
        return f'<div class="legend">{"".join(cells)}</div>'

    # --- sections
    def topbar(self) -> str:
        h, v, w = self.f["hero"], self.f["version"], self.f["window"]
        rank = h.get("wr_rank")
        rank = f' <span class="muted">#{rank[0]}/{rank[1]}</span>' if rank else ""
        nw = self.f.get("nw_targets") or {}
        souls = "".join(f'<span class="chip"><i>{_mmss(int(t))}</i>{n / 1000:.1f}k</span>' for t, n in nw.items())
        g = self.f.get("games") or {}
        end = (f'<span class="chip end"{_tip("average game length and end souls")}><i>end '
               f'{_mmss(g["avg_duration_s"])}</i>{g["avg_end_souls"] / 1000:.1f}k</span>') if g.get("avg_end_souls") else ""
        return (f'<header id="top">{self.hero_icon(h["hero_id"], "hero big")}<div class="title">'
                f'<h1>{_e(h["name"])} <span class="wr">{_pct(h["win_rate"])}</span>{rank}</h1>'
                f'<div class="sub">build {_e(v["client_version"])} · {_e(v["released_at"])} · last {w["days"]}d'
                f'</div></div><div class="souls"><span class="lbl">avg souls</span>{souls}{end}</div>{STATUS_PILL}'
                f'</header><div id="progress"></div>')

    def lane(self) -> str:
        ln = self.f.get("lane")
        if not ln:
            return ""
        rows = [("exact 2v2", ln["matches"], ln["win_rate"], ln["nw_diff_15"])]
        rows += [(f"vs {x['opponent']}", x.get("matches"), x["win_rate"], x["nw_diff_15"]) for x in ln["vs_each"]]
        body = "".join(f'<tr><td>{_e(n)}</td><td>{_pct(wr)}</td><td class="{_cls(d)}">{_signed(d)}</td>'
                       f'<td class="muted">{_e(m or "")}</td></tr>' for n, m, wr, d in rows)
        me = " + ".join(filter(None, [self.f["hero"]["name"], ln["partner"]]))
        return (f'<section class="card lane"><h3>Lane <span class="muted">{_e(me)} vs {_e(" + ".join(ln["opponents"]))}'
                f'</span></h3><table><tr><th></th><th>wr</th><th>souls@15</th><th>games</th></tr>{body}</table></section>')

    def roster(self, enemy: bool) -> str:
        rs = self.f["enemies" if enemy else "allies"]
        if not rs:
            return ""

        def card(r):
            shift = r.get("shift")
            sp = r.get("spend") or {}
            bar = "".join(f'<i class="{k}" style="width:{sp.get(k, 0) * 100:.0f}%"></i>'
                          for k in ("weapon", "spirit", "vitality")) if sp else ""
            mix = " ".join(f"{k[0].upper()}{sp.get(k, 0):.0%}" for k in ("weapon", "spirit", "vitality")) if sp else ""
            chips = [f'<span class="tag cc">{_e(c)}</span>' for c in r.get("cc") or []]
            chips += [f'<span class="tag heal">heal · {_e(x)}</span>' for x in r.get("heals") or []]
            chips += [f'<span class="tag stealth">stealth · {_e(x)}</span>' for x in r.get("stealth") or []]
            tip = "\n".join(filter(None, [
                f"ult: {r['ult']}" if r.get("ult") else "", f"spend {mix}" if mix else "",
                "buys: " + ", ".join(r["core"]) if r.get("core") else "",
                "counters they buy: " + ", ".join(r["counters"]) if r.get("counters") else ""]))
            label = "me vs" if enemy else "with"
            return (f'<div class="rc"{_tip(tip)}>{self.hero_icon(r["hero_id"])}<div class="rb"><div class="rn">'
                    f'<b>{_e(r["name"])}</b><span class="{_cls(shift)}">{label} {_signed(shift, pts=True)}</span></div>'
                    f'<div class="spend">{bar}</div><div class="tags">{"".join(chips)}</div></div></div>')

        mix = self.f.get("enemies_damage_mix" if enemy else "allies_damage_mix") or {}
        lean = " · ".join(f"{k[0].upper()} {v:.0%}" for k, v in mix.items())
        return (f'<section class="card roster {"enemy" if enemy else "ally"}"><h3>{"Enemies" if enemy else "Allies"}'
                f' <span class="muted">{lean}</span></h3>{"".join(card(r) for r in rs)}</section>')

    def chosen(self) -> dict | None:
        tag = (self.plan.get("build") or {}).get("tag") or "B1"
        bs = self.f["builds"]
        return next((x for x in bs if x["tag"] == tag), bs[0] if bs else None)

    def candidates(self, b: dict | None, pending: bool) -> str:
        rows = []
        for x in self.f["builds"]:
            upd = datetime.date.fromtimestamp(x["updated"]).isoformat() if x.get("updated") else ""
            sp = x.get("spend") or {}
            mix = " ".join(f"{k[0].upper()}{sp.get(k, 0):.0%}" for k in ("weapon", "spirit", "vitality")) if sp else ""
            rows.append(f'<tr class="{"on" if b and x["tag"] == b["tag"] else ""}"><td><b>{x["tag"]}</b></td>'
                        f'<td>{_e(x["name"])}</td><td>{_pct(x["win_rate"])}</td><td>{x["matches"]}</td>'
                        f'<td class="muted">{mix}</td><td class="muted">{_e(", ".join(x.get("counters") or []))}</td>'
                        f'<td class="muted">{upd}</td></tr>')
        return (f'<details class="cands"{" open" if pending else ""}><summary>{len(rows)} candidate builds</summary>'
                f'<table><tr><th></th><th>name</th><th>wr</th><th>games</th><th>spend</th><th>has</th><th>updated</th>'
                f'</tr>{"".join(rows)}</table></details>')

    def popular(self, pending: bool) -> str:
        """The chosen community build in its author's own sections (optional ones marked), with corrections."""
        b = self.chosen()
        if not b:
            return '<section class="card popular"><h2>Popular build</h2><p>no builds found</p></section>'
        fixes = self.plan.get("fixes") or []
        cut = {(x.get("cut") or x.get("instead") or "").lower(): x for x in fixes if x.get("cut") or x.get("instead")}
        in_build = {it["name"].lower() for c in b["categories"] for it in c["items"]}
        # an "add" of an item the build already has highlights it in place (e.g. "buy earlier") instead of repeating it
        boost = {x["add"].lower(): x for x in fixes if x.get("add") and x["add"].lower() in in_build}
        added = [x for x in fixes if x.get("add") and x["add"].lower() not in in_build]
        sections = []
        if added:
            cells = "".join(self.item_icon(x["add"], "add", f"ADD{' instead of ' + x['instead'] if x.get('instead') else ''}:"
                                           f" {x.get('why', '')}") for x in added)
            sections.append(f'<div class="sec added"><h4>Added for this roster</h4><div class="items">{cells}</div></div>')
        for c in b["categories"]:
            cells = []
            for r in c["items"]:
                fx, up = cut.get(r["name"].lower()), boost.get(r["name"].lower())
                stats = f"bought by {r['pick']:.0%}" + (f" @{_mmss(r['buy_s'])}" if r.get("buy_s") else "")
                cells.append(self.item_icon(r["name"], ("cut" if fx else "add" if up else "") + (" small" if c.get("optional") else ""),
                                            "\n".join(filter(None, [
                                                stats, f"imbue: {r['imbue']}" if r.get("imbue") else "",
                                                f"author: {r['note']}" if r.get("note") else "",
                                                f"CUT: {fx.get('why', '')}" if fx else "",
                                                f"PRIORITY: {up.get('why', '')}" if up else ""]))))
            opt = ' <span class="opt">optional</span>' if c.get("optional") else ""
            desc = f'<div class="desc">{_e(c["description"])}</div>' if c.get("description") else ""
            sections.append(f'<div class="sec{" optional" if c.get("optional") else ""}"><h4>{_e(c["category"])}{opt}</h4>'
                            f'{desc}<div class="items">{"".join(cells)}</div></div>')
        order = (self.f["skills"]["orders"] or [None])[0]
        skills = (f'<h4>Skills <span class="muted">{_e(order["label"])} · {_pct(order["share"])} of games · '
                  f'wr {_pct(order["win_rate"])}</span></h4>' + self.skill_row(order["sequence"])) if order else ""
        why = (self.plan.get("build") or {}).get("why")
        fix_rows = []
        for x in fixes:
            if x.get("add"):
                kind, badge = ("up", "priority") if x["add"].lower() in in_build else ("add", "add")
                what = f'<b>{_e(x["add"])}</b>' + (f' <span class="muted">instead of</span> {_e(x["instead"])}'
                                                   if x.get("instead") else "")
            else:
                kind, badge, what = "cut", "cut", f'<b>{_e(x.get("cut"))}</b>'
            fix_rows.append(f'<li class="{kind}"><span class="badge">{badge}</span><div>{what}'
                            f'<div class="why">{_e(x.get("why"))}</div></div></li>')
        title = (f'Popular <span class="tagname">{b["tag"]}</span> {_e(b["name"])}' if not pending else
                 f'Popular builds <span class="muted">· showing {b["tag"]} until Claude picks</span>')
        return (f'<section class="card popular"><h2>{title}</h2>'
                f'<div class="sub">id <b class="copy" data-copy="{b["hero_build_id"]}"'
                f'{_tip("click to copy the build id")}>{b["hero_build_id"]}</b> · wr {_pct(b["win_rate"])} over '
                f'{b["matches"]} games</div>{f"<p class=why>{_e(why)}</p>" if why else ""}'
                f'{f"<ul class=fixes>{chr(10).join(fix_rows)}</ul>" if fix_rows else ""}{self.candidates(b, pending)}'
                f'<div class="secs">{"".join(sections)}</div>{skills}</section>')

    def builds(self) -> list[dict]:
        c = self.plan.get("claude") or []
        return [c] if isinstance(c, dict) else c

    def slots(self, b: dict) -> list[dict]:
        """Walk a build's purchases in order: an upgrade takes its component's slot, a sell frees one (when
        buying the item it names in "for", else when the inventory is full). Per phase: slots used, held items'
        worth (≈ net worth needed), and when the hero's average player has that much."""
        held: list[str] = []
        lost = 0  # sells refund half
        sells = [dict(s) for s in b.get("sell") or [] if s.get("item")]
        out = []
        for key, *_ in PHASES:
            for name in (b.get(key) or {}).get("items") or []:
                it = self.catalog.get(name.lower())
                if not it:
                    continue
                for s in [s for s in sells if (s.get("for") or "").lower() == it["name"].lower()]:
                    lost += self._sell(held, s, sells)
                comps = [c for c in it.get("components") or [] if c in held]
                if comps:
                    held[held.index(comps[0])] = it["name"]
                else:
                    if len(held) >= SLOTS:
                        s = next((s for s in sells if not s.get("for") and s["item"] in held), None)
                        if s:
                            lost += self._sell(held, s, sells)
                    if len(held) >= SLOTS:
                        self.warnings.append(f"{b.get('name') or 'build'}: no free slot for {it['name']} ({SLOTS} held, "
                                             f"add a sell)")
                    held.append(it["name"])
            worth = sum(self.catalog[h.lower()]["cost"] or 0 for h in held) + lost
            out.append({"phase": key, "held": list(held), "worth": worth, "at": self._souls_time(worth)})
        return out

    def _sell(self, held: list[str], s: dict, sells: list[dict]) -> int:
        sells.remove(s)
        name = (self.catalog.get(s["item"].lower()) or {}).get("name")
        if name not in held:
            self.warnings.append(f"sell {s['item']!r}: not held at that point")
            return 0
        held.remove(name)
        return (self.catalog[name.lower()]["cost"] or 0) // 2

    def _souls_time(self, souls: float) -> str | None:
        """When the hero's average player reaches this net worth (linear between curve points)."""
        pts = sorted((int(t), n) for t, n in (self.f.get("nw_targets") or {}).items())
        prev = (0, 0)
        for t, n in pts:
            if souls <= n:
                return _mmss(prev[0] + (t - prev[0]) * (souls - prev[1]) / max(n - prev[1], 1))
            prev = (t, n)
        return f">{_mmss(prev[0])}" if pts else None

    def claude_build(self, b: dict, first_skills: dict) -> str:
        phases = []
        for (key, title, span, _, _), s in zip(PHASES, self.slots(b)):
            p = b.get(key) or {}
            items = "".join(self.item_icon(n) for n in p.get("items") or [])
            used = len(s["held"])
            pips = "".join(f'<i class="{"on" if i < used else ""}"></i>' for i in range(max(SLOTS, used)))
            at = f' · avg player <b>{_e(s["at"])}</b>' if s["at"] else ""
            plan = f'<p>{_e(p.get("plan"))}</p>' if p.get("plan") else ""
            phases.append(f'<div class="phase {key}"><div class="ph"><h4>{title} <span class="muted">{span}</span></h4>'
                          f'<span class="pips{" over" if used > SLOTS else ""}"{_tip(f"{used}/{SLOTS} slots")}>{pips}</span>'
                          f'<span class="meta">{used}/{SLOTS} · {s["worth"] / 1000:.1f}k souls{at}</span></div>'
                          f'<div class="items">{items}</div>{plan}</div>')
        extra = []
        if b.get("optional"):
            extra.append('<div class="side opt-items"><h4>Optional <span class="opt">situational</span></h4>' + "".join(
                f'<div class="cond">{self.item_icon(o.get("item"), "small", "WHEN: " + (o.get("when") or ""))}'
                f'<div class="why">{_e(o.get("when"))}</div></div>' for o in b["optional"]) + "</div>")
        if b.get("sell"):
            extra.append('<div class="side sells"><h4>Sell</h4><ul class="fixes">' + "".join(
                f'<li class="cut"><span class="badge">sell</span><div><b>{_e(s.get("item"))}</b>'
                f'{" <span class=muted>for</span> " + _e(s["for"]) if s.get("for") else ""}'
                f'{"<div class=why>" + _e(s["why"]) + "</div>" if s.get("why") else ""}</div></li>' for s in b["sell"])
                + "</ul></div>")
        sk = b if b.get("skills") else first_skills
        skills = ""
        if sk.get("skills"):
            why = f'<p>{_e(sk.get("skills_why"))}</p>' if sk.get("skills_why") else ""
            skills = f'<div class="phase"><h4>Skill order</h4>{self.skill_row([{"ability": s} for s in sk["skills"]])}{why}</div>'
        when = f'<div class="when">{_e(b["when"])}</div>' if b.get("when") else ""
        return f'{when}{"".join(phases)}{skills}<div class="extras">{"".join(extra)}</div>'

    def claude(self) -> str:
        bs = self.builds()
        if not bs:
            return PENDING
        if len(bs) == 1:
            return (f'<section class="card claude"><h2>Claude <span class="muted">·</span> {_e(bs[0].get("name") or "build")}'
                    f'</h2>{self.claude_build(bs[0], bs[0])}</section>')
        # CSS-only tabs: one radio per build; keys 1-3 switch (see JS)
        radios = "".join(f'<input type="radio" name="cb" id="cb{i}"{" checked" if i == 0 else ""}>'
                         f'<label for="cb{i}"><kbd>{i + 1}</kbd> {_e(b.get("name") or f"Build {i + 1}")}'
                         f'{" <span class=main>main</span>" if i == 0 else ""}</label>' for i, b in enumerate(bs))
        panels = "".join(f'<div class="panel p{i}">{self.claude_build(b, bs[0])}</div>' for i, b in enumerate(bs))
        css = "".join(f"#cb{i}:checked~.panels .p{i}{{display:block}}" for i in range(len(bs)))
        return (f'<section class="card claude"><h2>Claude builds</h2><style>{css}</style>'
                f'<div class="tabs">{radios}<div class="panels">{panels}</div></div></section>')

    def watch(self) -> str:
        w = self.plan.get("watch") or []
        return (f'<section class="card watch"><h3>Watch</h3><ol>{"".join(f"<li>{_e(x)}</li>" for x in w)}</ol></section>'
                if w else "")

    def render(self) -> str:
        pending = not self.builds()
        main = (f'<main><div class="primary">{self.claude()}{self.skill_legend()}</div>'
                f'<aside>{self.watch()}{self.lane()}{self.roster(True)}{self.roster(False)}</aside></main>')
        warn = (f'<div class="warn"><b>Not in this game build:</b> {_e(", ".join(dict.fromkeys(self.warnings)))}</div>'
                if self.warnings else "")
        return shell(f'{self.f["hero"]["name"]} brief',
                     self.topbar() + f'<div class="wrap">{warn}{main}{self.popular(pending)}</div>')


def shell(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_e(title)}</title><link rel="icon" href="data:,">
<meta name="viewport" content="width=device-width, initial-scale=1">{FONTS}<style id="css">{CSS}</style>
<script>{JS}</script></head><body>{body}<div id="tip" hidden></div></body></html>"""


def placeholder() -> str:
    """What the server shows before the first facts of a run are in."""
    return shell("live brief", f'<header id="top"><div class="hero big"></div><div class="title"><h1>Live brief</h1>'
                               f'<div class="sub">waiting for <code>ailock live</code></div></div>{STATUS_PILL}</header>'
                               f'<div id="progress"></div><div class="wrap"><main><div class="primary">{PENDING}</div>'
                               f'</main></div>')


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _signed(v, pts=False) -> str:
    if v is None:
        return "-"
    return f"{v * 100:+.1f}" if pts else f"{v:+.0f}"


def _cls(v) -> str:
    return "" if not v else ("good" if v > 0 else "bad")


def open_page(target: str) -> None:
    """Open a URL or file in the Windows default browser when under WSL, else the local one."""
    if shutil.which("explorer.exe"):
        if not target.startswith("http"):
            target = subprocess.run(["wslpath", "-w", target], check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["explorer.exe", target], check=False)  # explorer exits 1 even on success
    else:
        webbrowser.open(target if target.startswith("http") else Path(target).as_uri())


def write(plan: dict) -> tuple[Path, list[str]]:
    facts = json.loads(LAST.read_text())
    page = Page(facts, plan)
    tmp = PAGE.with_suffix(".tmp")
    tmp.write_text(page.render())
    tmp.replace(PAGE)  # atomic, so the server never serves half a page
    warnings = list(dict.fromkeys(page.warnings))
    if page.no_icon:
        warnings.append(f"no icon for {', '.join(dict.fromkeys(page.no_icon))} (deadlock-api asset index)")
    return PAGE, warnings


def render(plan_path: Path) -> tuple[Path, list[str]]:
    page, warnings = write(json.loads(plan_path.read_text()))
    problems = [w for w in warnings if not w.startswith("no icon")]
    if problems:
        set_status("fixing", f"Claude is fixing {len(problems)} issue{'s' * (len(problems) > 1)}", warnings=problems)
    else:
        set_status("ready", "Ready")
    return page, warnings


STATUS_PILL = ('<div id="status" data-stage="idle"><span class="dot"></span><span class="msg">offline</span>'
               '<span class="el"></span></div>')

PENDING = """<section class="card claude pending"><h2>Claude is building your plan</h2>
<ol class="steps"><li data-step="facts">Gathering facts <span class="muted">matchups, builds, item stats</span></li>
<li data-step="deciding">Reading the room <span class="muted">build pick, corrections, own builds, skill order</span></li>
<li data-step="ready">Brief ready</li></ol>
<div class="skel"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
<div class="skel"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
<div class="skel"><i></i><i></i><i></i><i></i><i></i></div></section>"""

# Tooltips for [data-tip], build tabs on keys 1-3, click-to-copy, and (when served) live status + in-place reload
JS = r"""
(() => {
  const $ = s => document.querySelector(s);
  const fmt = s => (s = Math.max(0, Math.round(s)), `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`);
  addEventListener('mouseover', e => {
    const t = e.target.closest('[data-tip]'), tip = $('#tip');
    if (!t) { tip.hidden = true; return; }
    tip.textContent = t.dataset.tip; tip.hidden = false;
    const r = t.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    let x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8), y = r.bottom + 8;
    if (y + h > innerHeight - 8) y = Math.max(8, r.top - h - 8);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });
  addEventListener('scroll', () => { $('#tip').hidden = true; }, {passive: true});
  addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey || e.altKey || /input|textarea/i.test(e.target.tagName)) return;
    const r = document.getElementById('cb' + (+e.key - 1)); if (r) r.checked = true;
  });
  addEventListener('click', e => {
    const c = e.target.closest('[data-copy]'); if (!c) return;
    navigator.clipboard?.writeText(c.dataset.copy).then(() => { c.classList.add('copied'); setTimeout(() => c.classList.remove('copied'), 900); });
  });
  if (!location.protocol.startsWith('http')) return;
  let st = null, rev = null;
  const ORDER = ['facts', 'deciding', 'ready'];
  function paint() {
    const el = $('#status'); if (!el) return;
    const stage = st ? st.stage : 'offline';
    document.documentElement.dataset.stage = stage;
    el.dataset.stage = stage;
    el.querySelector('.msg').textContent = st ? st.message : 'server offline';
    const busy = st && !['ready', 'error'].includes(stage);
    el.querySelector('.el').textContent = st ? (busy ? fmt(Date.now() / 1000 - st.started) : stage === 'ready' ? 'in ' + fmt(st.t - st.started) : '') : '';
    const at = ORDER.indexOf(stage === 'fixing' ? 'deciding' : stage);
    document.querySelectorAll('.steps li').forEach(li => {
      const i = ORDER.indexOf(li.dataset.step);
      li.className = stage === 'error' && i === Math.max(at, 0) ? 'err' : i < at || stage === 'ready' ? 'done' : i === at ? 'active' : '';
    });
    const hero = document.title.replace(/^[^A-Za-z]+/, '');
    document.title = (busy ? '⏳ ' : stage === 'error' ? '⚠ ' : '') + hero;
  }
  async function reload() {
    const doc = new DOMParser().parseFromString(await (await fetch('/', {cache: 'no-store'})).text(), 'text/html');
    const tab = document.querySelector('.tabs input:checked')?.id, y = scrollY;
    const open = [...document.querySelectorAll('details[open]')].map(d => d.className);
    document.title = doc.title;
    $('#css').replaceWith(doc.querySelector('#css'));
    document.body.replaceWith(doc.body);
    if (tab && document.getElementById(tab)) document.getElementById(tab).checked = true;
    document.querySelectorAll('details').forEach(d => { if (open.includes(d.className)) d.open = true; });
    scrollTo(0, y); paint();
    document.body.classList.add('flash'); setTimeout(() => document.body.classList.remove('flash'), 700);
  }
  setInterval(paint, 1000);
  const es = new EventSource('/events');
  es.addEventListener('status', e => { st = JSON.parse(e.data); paint(); });
  es.addEventListener('page', e => { if (rev !== null && e.data !== rev) reload(); rev = e.data; });
  es.onerror = () => { st = null; paint(); };
  addEventListener('DOMContentLoaded', paint);
})();
"""

CSS = """
:root{--bg:#0f1114;--panel:#171a1f;--panel2:#1c2027;--line:#272c35;--text:#e8e4d8;--muted:#8d8a82;
--weapon:#d9862f;--spirit:#9a68d6;--vitality:#62ad40;--good:#74c47a;--bad:#e8705f;--accent:#e2bd5e;--info:#6fa8dc;
--mono:'Iosevka','Iosevka Term',ui-monospace,'Cascadia Mono',Consolas,monospace}
*{box-sizing:border-box}html{scroll-padding-top:80px}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--mono);font-variant-numeric:tabular-nums;
transition:background .7s}body.flash{background:#15180f}
h1,h2,h3,h4{font-weight:700}h1{margin:0;font-size:22px;letter-spacing:-.01em}
h2{margin:0 0 6px;font-size:17px;color:var(--accent)}
h3{margin:0 0 8px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
h4{margin:0;font-size:13px;text-transform:uppercase;letter-spacing:.06em}
p{margin:8px 0 0}code,kbd{font:inherit}.muted{color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0}
.good{color:var(--good)}.bad{color:var(--bad)}.sub{color:var(--muted);font-size:12px}.sub b{color:var(--text)}
kbd{display:inline-block;min-width:1.5em;padding:0 4px;border:1px solid var(--line);border-bottom-width:2px;
border-radius:4px;font-size:11px;text-align:center;color:var(--muted);background:var(--bg)}
/* top bar */
#top{position:sticky;top:0;z-index:5;display:flex;gap:14px;align-items:center;padding:10px 20px;
background:rgba(15,17,20,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
#top .title{flex:none;white-space:nowrap}.wr{color:var(--accent)}
.souls{display:flex;gap:4px;align-items:center;margin-left:auto;min-width:0;overflow:hidden;white-space:nowrap}.chip{flex:none}
.souls .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-right:4px}
.chip{display:inline-flex;gap:5px;padding:2px 7px;border:1px solid var(--line);border-radius:999px;font-size:12px;
font-weight:500;background:var(--panel)}.chip i{font-style:normal;color:var(--muted);font-weight:400}
.chip.end{border-color:#4a3f22}
#status{display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:999px;border:1px solid var(--line);
background:var(--panel);font-size:13px;white-space:nowrap;flex:none}
#status .el{color:var(--muted)}#status .dot{width:9px;height:9px;border-radius:50%;background:var(--muted)}
#status[data-stage=facts] .dot,#status[data-stage=deciding] .dot,#status[data-stage=fixing] .dot{background:var(--accent);
animation:pulse 1.1s ease-in-out infinite}#status[data-stage=fixing] .dot{background:var(--weapon)}
#status[data-stage=ready] .dot{background:var(--good)}#status[data-stage=error]{border-color:var(--bad);color:var(--bad)}
#status[data-stage=error] .dot{background:var(--bad)}
@keyframes pulse{50%{opacity:.25;transform:scale(.7)}}
#progress{position:sticky;top:0;z-index:6;height:2px;margin-top:-2px;overflow:hidden}
html[data-stage=facts] #progress,html[data-stage=deciding] #progress,html[data-stage=fixing] #progress{
background:linear-gradient(90deg,transparent,var(--accent),transparent) 0 0/40% 100% no-repeat;animation:slide 1.6s linear infinite}
@keyframes slide{from{background-position:-40% 0}to{background-position:140% 0}}
/* layout */
.wrap{max-width:1680px;margin:0 auto;padding:16px 20px 40px}
main{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(320px,1fr);gap:16px;align-items:start}
.primary,aside{display:flex;flex-direction:column;gap:16px;min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;min-width:0}
.card.claude{border-color:#5a4a20;box-shadow:0 0 0 1px #2c2512 inset}
.card.popular{margin-top:16px}
.hero{width:40px;height:40px;border-radius:8px;object-fit:cover;background:var(--panel2);flex:none}
.hero.big{width:52px;height:52px}
.warn{background:#3a1a16;border:1px solid #6b2a22;color:#ffd7d2;padding:8px 12px;border-radius:8px;margin-bottom:16px}
/* items */
.items{display:flex;flex-wrap:wrap;gap:8px}
.item{width:74px;text-align:center;font-size:11px;line-height:1.15;position:relative;cursor:default;
padding:4px 2px 3px;border-radius:6px;border-bottom:3px solid var(--line);background:var(--panel2);transition:transform .1s}
.item:hover{transform:translateY(-2px);background:#232833}
.item img,.item .noimg{width:48px;height:48px;display:block;margin:0 auto 3px}
.item .name{display:block}.item .cost{display:block;color:var(--muted);font-size:10px;margin-top:1px}
.item.weapon{border-color:var(--weapon)}.item.spirit{border-color:var(--spirit)}.item.vitality{border-color:var(--vitality)}
.item.cut{opacity:.35;text-decoration:line-through}.item.cut img{filter:grayscale(1)}
.item.add{box-shadow:0 0 0 2px var(--good);background:#1a2a1c}
.item.bad{color:var(--bad);border:1px dashed var(--bad);display:flex;align-items:center;justify-content:center;min-height:60px}
.item.small{width:60px;font-size:10px}.item.small img{width:34px;height:34px}
/* claude build */
.tabs>input{position:absolute;opacity:0;pointer-events:none}
.tabs>label{display:inline-flex;gap:6px;align-items:center;padding:5px 12px;margin:0 6px 12px 0;border:1px solid var(--line);
border-radius:999px;cursor:pointer;color:var(--muted);font-size:13px}.tabs>label:hover{color:var(--text)}
.tabs>input:checked+label{color:var(--bg);background:var(--accent);border-color:var(--accent);font-weight:700}
.tabs>input:checked+label kbd{background:transparent;color:inherit;border-color:rgba(0,0,0,.35)}
.tabs>input:focus-visible+label{outline:2px solid var(--info);outline-offset:2px}
.main{font-size:10px;text-transform:uppercase;letter-spacing:.06em;opacity:.7}
.panels .panel{display:none}
.when{color:var(--muted);font-size:13px;margin:-2px 0 4px;padding-left:10px;border-left:2px solid var(--accent)}
.phase{padding:12px 0 2px;border-top:1px solid var(--line);margin-top:12px}.when+.phase{border-top:0;margin-top:0}
.phase p{font-size:13px;color:#cfcabd;max-width:100ch}
.ph{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;margin-bottom:10px}
.phase.early h4{color:#b5d7a8}.phase.mid h4{color:#e2c27a}.phase.late h4{color:#e59b86}
.pips{display:inline-flex;gap:2px}.pips i{width:7px;height:12px;border-radius:2px;background:var(--line)}
.pips i.on{background:var(--accent)}.pips.over i.on{background:var(--bad)}
.meta{color:var(--muted);font-size:12px}.meta b{color:var(--text);font-weight:500}
.extras{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px 20px;margin-top:12px}
.extras:empty{display:none}.side{border-top:1px solid var(--line);padding-top:12px}.side h4{margin-bottom:8px}
.cond{display:flex;gap:10px;align-items:center;margin-bottom:8px}.cond .why{font-size:12px;color:#cfcabd}
/* skills */
.skills{display:flex;flex-wrap:wrap;gap:3px}
.pt{width:40px;text-align:center;font-size:10px;color:var(--muted);cursor:default}
.pt i{display:block;font-style:normal;font-size:9px}
.pt img,.pt b{width:32px;height:32px;display:block;margin:1px auto;border-radius:6px;padding:3px;
border:2px solid var(--line);background:var(--panel2)}
.pt span{display:block;color:var(--text)}.pt em{display:block;font-style:normal;font-size:9px}
.pt.t3 span{color:var(--accent);font-weight:700}.pt.t3 img{box-shadow:0 0 0 1px var(--accent)}
.s1 img,.s1 b{border-color:#3d86bd}.s2 img,.s2 b{border-color:#b07a3f}.s3 img,.s3 b{border-color:#4f9a5c}
.s4 img,.s4 b{border-color:#9b4d9b}
.legend{display:flex;flex-wrap:wrap;gap:8px 18px;padding:0 4px}
.leg{display:flex;align-items:center;gap:7px;font-size:13px;cursor:default}
.leg img{width:28px;height:28px;border-radius:6px;padding:2px;border:2px solid var(--line);background:var(--panel2)}
/* aside */
.watch{border-color:#5c2a24;background:linear-gradient(180deg,#1f1615,var(--panel) 60%)}
.watch h3{color:var(--bad)}.watch ol{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:8px;font-size:13px}
.watch li::marker{color:var(--bad);font-weight:700}
.rc{display:flex;gap:10px;padding:8px 0;border-top:1px solid var(--line);cursor:default}.rc:first-of-type{border-top:0;padding-top:0}
.rb{flex:1;min-width:0}.rn{display:flex;justify-content:space-between;gap:8px;font-size:13px}.rn span{font-size:12px}
.spend{display:flex;height:4px;border-radius:2px;overflow:hidden;margin:4px 0 5px;background:var(--line)}
.spend .weapon{background:var(--weapon)}.spend .spirit{background:var(--spirit)}.spend .vitality{background:var(--vitality)}
.tags{display:flex;flex-wrap:wrap;gap:3px}.tag{font-size:11px;padding:0 6px;border-radius:4px;background:var(--panel2);
color:#c9c4b6;border:1px solid var(--line)}.tag.cc{border-color:#4b3a6b}.tag.heal{border-color:#2f5a33}
.tag.stealth{border-color:#3a4b5c}
table{border-collapse:collapse;width:100%;font-size:12px}th{text-align:left;color:var(--muted);font-weight:400}
td,th{padding:3px 8px 3px 0}
/* popular */
.tagname{color:var(--bg);background:var(--accent);border-radius:4px;padding:0 6px;font-size:13px;vertical-align:2px}
.popular>.why{font-size:13px;color:#cfcabd;max-width:110ch}
.copy{cursor:pointer;border-bottom:1px dotted var(--muted)}.copy.copied{color:var(--good)}
.fixes{list-style:none;margin:10px 0 0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:8px}
.fixes li{display:flex;gap:10px;align-items:flex-start;font-size:13px;padding:8px 10px;border-radius:8px;background:var(--panel2)}
.fixes .why{color:#bdb8aa;font-size:12px;margin-top:2px}
.badge{flex:none;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:1px 6px;
border-radius:4px;color:var(--bg);margin-top:2px}.add .badge{background:var(--good)}.cut .badge{background:var(--bad)}
.up .badge{background:var(--info)}
.cands{margin-top:12px;font-size:13px}.cands summary{cursor:pointer;color:var(--muted)}
.cands table{margin-top:6px}.cands tr.on td{color:var(--accent)}.cands td:nth-child(2){max-width:34ch;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.secs{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:16px 24px;margin:16px 0}
.sec h4{margin-bottom:4px}.sec .desc{color:var(--muted);font-size:12px;margin-bottom:8px}.sec h4+.items{margin-top:8px}
.sec.optional h4{color:var(--muted)}.sec.added h4{color:var(--good)}
.opt{font-size:10px;font-weight:700;color:var(--bg);background:var(--muted);border-radius:3px;padding:1px 5px;
letter-spacing:.04em;vertical-align:1px}
.popular>h4{margin:4px 0 10px}
/* pending */
.steps{margin:8px 0 16px;padding:0;list-style:none;display:flex;flex-direction:column;gap:8px}
.steps li{display:flex;gap:10px;align-items:baseline;color:var(--muted)}
.steps li::before{content:'';flex:none;width:10px;height:10px;border-radius:50%;border:2px solid var(--line);transform:translateY(1px)}
.steps li.active{color:var(--text)}.steps li.active::before{border-color:var(--accent);background:var(--accent);animation:pulse 1.1s infinite}
.steps li.done{color:var(--text)}.steps li.done::before{border-color:var(--good);background:var(--good)}
.steps li.err{color:var(--bad)}.steps li.err::before{border-color:var(--bad);background:var(--bad)}
.skel{display:flex;gap:8px;margin-top:14px}.skel i{width:74px;height:80px;border-radius:6px;
background:linear-gradient(90deg,var(--panel2),#252a33,var(--panel2)) 0 0/300% 100%;animation:shimmer 1.4s infinite}
@keyframes shimmer{from{background-position:100% 0}to{background-position:-100% 0}}
#tip{position:fixed;z-index:10;max-width:380px;padding:8px 10px;border-radius:8px;background:#0a0b0d;border:1px solid #3a404c;
box-shadow:0 8px 24px rgba(0,0,0,.5);font-size:12px;line-height:1.45;white-space:pre-line;pointer-events:none}
@media (max-width:1100px){main{grid-template-columns:1fr}
.souls{display:none}}
.cands{overflow-x:auto}
@media (max-width:600px){.wrap{padding:12px 12px 32px}#top{padding:8px 12px;flex-wrap:wrap}#top .title{white-space:normal}
#status{margin-left:auto}.secs{grid-template-columns:1fr}
.fixes{grid-template-columns:1fr}#status .msg{max-width:40vw;overflow:hidden;text-overflow:ellipsis}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""
