#!/usr/bin/env python3
"""Regenerate README.md from imho.run's public co-op data.

Python 3.12+, standard library only. Run from the repository root:

    python generate.py            # weekly regeneration (what the workflow runs)
    python generate.py --force    # also allow a change bigger than the diff guard
    python generate.py --check    # offline: validate overrides.json and README.md

What gets published: imho.run's own ranks, co-op mode classification and
taglines, plus each game's name, Steam appid, imho.run URL and Steam store
URL. Never review counts, percentages, store descriptions or images: the
generator reads only a whitelist of fields and checks the output before it
writes anything (see FORBIDDEN_PATTERNS).

Hand curation lives in overrides.json (see CONTRIBUTING.md): excluded games,
pinned games and replacement descriptions are applied on every run, so they
survive regeneration.

The script fails closed. A missing field, a short section, too many
templated descriptions or a change bigger than MAX_CHURN aborts the run with
a non-zero exit and leaves every file untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
README = ROOT / "README.md"
LIST_JSON = ROOT / "data" / "list.json"
TAGLINES = ROOT / "taglines.json"
OVERRIDES = ROOT / "overrides.json"

SITE = "https://imho.run"
API = "https://api.imho.run"
UTM = "utm_source=github"
USER_AGENT = "awesome-coop-games-generator (+https://github.com/0x216/awesome-coop-games)"

# Each public dataset URL comes with the backend URL that serves the same file,
# used when the site's pass-through is briefly unavailable.
COOP_DATASET = (
    f"{SITE}/datasets/steam-coop-games-by-mode.json",
    f"{API}/api/datasets/steam-coop-games-by-mode.json",
)
MODS_DATASET = (f"{SITE}/datasets/coop-mods.json", f"{API}/api/datasets/coop-mods.json")
# API calls go straight to the backend host (imho.run/api/* is a proxy to it).
AWARDS = f"{API}/api/awards"
DISCOVER = f"{API}/api/games/discover"
GAME_FACTS = f"{API}/api/agent/game-facts"
NEW_COOP_FEED = f"{SITE}/feeds/en/new-coop-games.xml"

HTTP_TIMEOUT = 20
HTTP_RETRIES = 3
TAGLINE_SLEEP = 2.5
RATE_LIMIT_WAIT = 65  # the agent API counts requests per IP in one-minute windows
TAGLINE_CALLS_PER_RUN = 15

GLOBAL_MIN = 80
GLOBAL_MAX = 110
SECTION_MIN_SHARE = 0.8
MAX_TEMPLATE_SHARE = 0.10
MAX_CHURN = 0.30  # share of entries added or removed vs the last committed list
MIN_MEANINGFUL_CHANGES = 3  # entries added, removed or moved to another section
# GitHub turns off scheduled workflows in public repos after 60 days without a
# commit, so a quiet list is refreshed (new date, current order) after this many.
MAX_QUIET_DAYS = 45
AWARDS_MIN_ITEMS = 8
CROSS_PLATFORM_MAX_OVERLAP = 0.5
FREE_MAX_PAGES = 5

# The only discover fields the generator ever keeps. Everything else in the
# response (store descriptions, images, review numbers, prices) is dropped on
# read and never reaches the README.
DISCOVER_FIELDS = (
    "appid",
    "name",
    "release_year",
    "genres",
    "coop_online",
    "coop_local",
    "is_free",
    "co_op_hook",
)
DATASET_FIELDS = ("appid", "name", "imho_url", "mode", "release_year")
AWARD_FIELDS = ("rank", "appid", "name")
MOD_FIELDS = ("appid", "game", "imho_url", "mod_name", "mod_url", "maturity", "note_en")

FORBIDDEN_PATTERNS = (
    re.compile(r"\d[\d,.\s]*\+?\s*(?:steam\s+)?(?:reviews?|ratings?|players?\s+online)", re.I),
    re.compile(r"\d\s*%"),
    re.compile(r"<img", re.I),
    re.compile(r"!\["),
    re.compile(r"steamstatic|akamai|cdn\.", re.I),
)
# The awards rule line states the award's own published criteria.
ALLOWED_LINE_PREFIX = "_Top 10 co-op games released in "

GENERIC_GENRES = {"Indie", "Early Access", "Free To Play", "Massively Multiplayer", "Gore"}
# Tags too common among co-op games to say anything about one of them.
GENERIC_TAGS = {
    t.lower()
    for t in (
        "Co-op", "Online Co-Op", "Local Co-Op", "Co-op Campaign", "Multiplayer",
        "Local Multiplayer", "Massively Multiplayer", "Singleplayer", "Split Screen",
        "4 Player Local", "PvP", "PvE", "Online PvP", "Action", "Adventure", "Indie",
        "Casual", "Early Access", "Free to Play", "Great Soundtrack", "Addictive",
        "Masterpiece", "Classic", "Cult Classic", "Beautiful", "Replay Value",
        "Controller", "Family Friendly", "Steam Achievements", "Multiple Endings",
    )
}


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    cap: int
    rule: str
    hub: str | None = None
    optional: bool = False


SECTIONS: tuple[Section, ...] = (
    Section(
        "best-of-year",
        "Best co-op games of {year}",
        10,
        "_Top 10 co-op games released in {year} by Wilson lower bound; "
        "{min_reviews}+ reviews, {min_pct}%+ positive. Provisional until {freeze_on}._",
        hub="/awards/{slug}",
        optional=True,
    ),
    Section(
        "online",
        "Online co-op",
        20,
        "_Ranked by imho.run's Bayesian-adjusted rating, junk-filtered; games with online co-op._",
        hub="/discover/coop",
    ),
    Section(
        "couch",
        "Couch and split-screen co-op",
        20,
        "_Same ranking; games with local or split-screen co-op._",
        hub="/discover/split-screen",
    ),
    Section(
        "couch-4",
        "4-player couch co-op",
        10,
        "_Same ranking; games with couch co-op for 4 players._",
        hub="/discover/couch-coop-4-players",
    ),
    Section(
        "horror",
        "Co-op horror",
        10,
        "_Same ranking; horror games with co-op._",
        hub="/discover/coop-horror",
    ),
    Section(
        "cross-platform",
        "Cross-platform co-op",
        10,
        "_Same ranking; co-op games with cross-platform play._",
        hub="/discover/cross-platform-coop",
        optional=True,
    ),
    Section(
        "free",
        "Free-to-play co-op",
        10,
        "_Same ranking; free games with co-op._",
        hub="/discover/coop",
    ),
    Section(
        "mods",
        "Co-op via mods",
        12,
        "_Single-player games that a fan mod turns into co-op. Hand-reviewed by imho.run._",
        hub="/discover/coop-mods",
    ),
)
SECTION_IDS = {s.id for s in SECTIONS}


class GenerationError(Exception):
    """Fail closed: the run stops and nothing is written."""


class RateLimited(Exception):
    """HTTP 429 from imho.run."""


# ── HTTP ────────────────────────────────────────────────────────────────────


def http_json(url: str) -> Any:
    last: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429:
                raise RateLimited(url) from exc
            if 400 <= exc.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise GenerationError(f"GET {url} failed: {last}")


def http_json_first(urls: tuple[str, ...]) -> Any:
    error: Exception | None = None
    for url in urls:
        try:
            return http_json(url)
        except (GenerationError, RateLimited) as exc:
            print(f"  {exc}; trying the next source", file=sys.stderr)
            error = exc
    raise GenerationError(f"all sources failed: {error}")


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class Game:
    appid: int
    name: str
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    online: bool = False
    local: bool = False
    is_free: bool = False
    hook: str | None = None
    imho_url: str | None = None


@dataclass
class Entry:
    game: Game
    description: str = ""
    source: str = ""  # note | tagline | hook | template | mod
    mod: dict[str, Any] | None = None


def pick(obj: dict[str, Any], fields: tuple[str, ...], what: str) -> dict[str, Any]:
    missing = [f for f in fields if f not in obj]
    if missing:
        raise GenerationError(f"{what}: response is missing {missing}; the API shape changed")
    return {f: obj[f] for f in fields}


def slugify(name: str) -> str:
    """Mirror of imho.run's slugifyGameName (frontend/src/lib/site.ts), so links
    land without a redirect. Order matters: lowercase first, then NFKD, so "™"
    becomes an uppercase "TM" that the [^a-z0-9] filter drops, as on the site."""
    if not name:
        return "game"
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    for ch in ("™", "®", "©"):
        s = s.replace(ch, "")
    s = re.sub(r"[^a-z0-9\s-]", " ", s).strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:80] or "game"


def imho_link(game: Game) -> str:
    # Built here rather than taken from the datasets' imho_url: the site's own
    # slug rule is the canonical one, and a different slug costs a redirect.
    return f"{SITE}/games/{game.appid}/{slugify(game.name)}?{UTM}"


def steam_link(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


def hub_link(path: str) -> str:
    return f"{SITE}{path}?{UTM}"


# ── Fetch ───────────────────────────────────────────────────────────────────


def fetch_dataset() -> dict[int, dict[str, Any]]:
    doc = http_json_first(COOP_DATASET)
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or len(rows) < 500:
        raise GenerationError("co-op dataset: missing or too few rows")
    return {int(r["appid"]): pick(r, DATASET_FIELDS, "co-op dataset") for r in rows}


def discover_page(preset: str, limit: int, offset: int = 0) -> list[Game]:
    doc = http_json(f"{DISCOVER}?preset={preset}&limit={limit}&offset={offset}")
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        raise GenerationError(f"discover {preset}: no items list; the API shape changed")
    out = []
    for raw in items:
        row = pick(raw, DISCOVER_FIELDS, f"discover {preset}")
        out.append(
            Game(
                appid=int(row["appid"]),
                name=str(row["name"]).strip(),
                year=row["release_year"] or None,
                genres=[str(g) for g in (row["genres"] or [])],
                online=bool(row["coop_online"]),
                local=bool(row["coop_local"]),
                is_free=bool(row["is_free"]),
                hook=row["co_op_hook"] or None,
            )
        )
    return out


def fetch_coop_pool() -> list[Game]:
    """The co-op hub ranking, paged until there are enough free games."""
    pool: list[Game] = []
    for page in range(FREE_MAX_PAGES):
        items = discover_page("coop", 100, page * 100)
        pool.extend(items)
        free = sum(1 for g in pool if g.is_free)
        if len(items) < 100 or (page >= 1 and free >= 20):
            break
    return pool


def fetch_awards() -> dict[str, Any] | None:
    doc = http_json(AWARDS)
    awards = doc.get("awards") if isinstance(doc, dict) else None
    if not isinstance(awards, list):
        return None
    for award in awards:
        if award.get("kind") == "online_coop":
            items = [pick(i, AWARD_FIELDS, "awards") for i in award.get("items") or []]
            crit = award.get("criteria") or {}
            return {
                "slug": award.get("slug"),
                "year": award.get("year"),
                "status": award.get("status"),
                "freeze_on": award.get("freeze_on"),
                "min_reviews": crit.get("min_reviews"),
                "min_pct": crit.get("min_positive_pct"),
                "items": sorted(items, key=lambda i: i["rank"]),
            }
    return None


def fetch_mods() -> list[dict[str, Any]]:
    doc = http_json_first(MODS_DATASET)
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise GenerationError("mods dataset: no rows")
    return [pick(r, MOD_FIELDS, "mods dataset") for r in rows]


# ── Overrides and taglines ──────────────────────────────────────────────────


def load_overrides() -> dict[str, Any]:
    if not OVERRIDES.exists():
        return {"exclude": [], "pin": {}, "note": {}}
    data = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GenerationError("overrides.json must be an object")
    unknown = set(data) - {"exclude", "pin", "note"}
    if unknown:
        raise GenerationError(f"overrides.json: unknown keys {sorted(unknown)}")
    exclude = data.get("exclude", [])
    pin = data.get("pin", {})
    note = data.get("note", {})
    if not isinstance(exclude, list) or not all(isinstance(a, int) for a in exclude):
        raise GenerationError("overrides.json: exclude must be a list of appids (integers)")
    if not isinstance(pin, dict):
        raise GenerationError("overrides.json: pin must map a section id to a list of appids")
    for sid, appids in pin.items():
        if sid not in SECTION_IDS or sid in ("best-of-year", "mods"):
            raise GenerationError(f"overrides.json: cannot pin into section {sid!r}")
        if not isinstance(appids, list) or not all(isinstance(a, int) for a in appids):
            raise GenerationError(f"overrides.json: pin[{sid!r}] must be a list of appids")
    if not isinstance(note, dict) or not all(
        k.isdigit() and isinstance(v, str) and v.strip() for k, v in note.items()
    ):
        raise GenerationError('overrides.json: note must map "appid" strings to text')
    for text in note.values():
        if forbidden_hit(text):
            raise GenerationError(f"overrides.json: note breaks the publishing rules: {text!r}")
    return {"exclude": exclude, "pin": pin, "note": note}


Facts = dict[str, Any]  # {"tagline": str | None, "tags": list[str]}


def load_taglines() -> dict[str, Facts]:
    """taglines.json: appid -> {"tagline", "tags"} from imho.run's agent API.
    An entry from the first cache format (a bare string or null) has no tags
    yet, so it is fetched again when its game is listed."""
    if not TAGLINES.exists():
        return {}
    data = json.loads(TAGLINES.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GenerationError("taglines.json must be an object")
    out: dict[str, Facts] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = {"tagline": value.get("tagline"), "tags": value.get("tags")}
        else:
            out[key] = {"tagline": value, "tags": None}
    return out


def fetch_taglines(appids: list[int], cache: dict[str, Facts], budget: int) -> int:
    """Fill missing taglines and tags from the documented agent API. Cache
    hits are kept as they are; a game without a tagline is cached with
    tagline null so it is not asked again. A 429 waits out the one-minute
    window; any other error stops fetching for this run and keeps the rest."""
    calls = 0
    for appid in appids:
        cached = cache.get(str(appid))
        if cached is not None and cached.get("tags") is not None:
            continue
        if calls >= budget:
            break
        if calls:
            time.sleep(TAGLINE_SLEEP)
        calls += 1
        doc = None
        for _ in range(3):
            try:
                doc = http_json(f"{GAME_FACTS}?q={appid}&lang=en")
                break
            except RateLimited:
                print(f"  rate limited at {appid}; waiting {RATE_LIMIT_WAIT}s", file=sys.stderr)
                time.sleep(RATE_LIMIT_WAIT)
            except GenerationError as exc:
                print(f"  tagline fetch failed for {appid}: {exc}", file=sys.stderr)
                break
        if doc is None:
            print("  tagline fetching stopped for this run", file=sys.stderr)
            break
        game = doc.get("game") if isinstance(doc, dict) else None
        tagline, tags = None, []
        if isinstance(game, dict) and int(game.get("appid") or 0) == appid:
            tagline = ((game.get("summary") or {}).get("tagline") or "").strip() or None
            tags = [str(t).strip() for t in (game.get("tags") or []) if str(t).strip()][:10]
        cache[str(appid)] = {"tagline": tagline, "tags": tags}
    return calls


# ── Descriptions ────────────────────────────────────────────────────────────


def humanize_hook(slug: str) -> str:
    text = re.sub(r"[-_]+", " ", slug).strip()
    text = re.sub(r"\bco op\b", "co-op", text)
    return text[:1].upper() + text[1:]


def mode_label(game: Game) -> str:
    if game.online and game.local:
        return "online+local"
    return "local" if game.local else "online"


def template_description(game: Game) -> str:
    mode = {"online+local": "Online and local", "local": "Local", "online": "Online"}[
        mode_label(game)
    ]
    genre = next((g for g in game.genres if g not in GENERIC_GENRES), None)
    parts = [f"{mode} co-op"] + ([genre] if genre else []) + ([str(game.year)] if game.year else [])
    return ", ".join(parts)


def tag_description(tags: list[str] | None) -> str | None:
    """The game's three most-voted descriptive Steam tags, skipping the ones
    every co-op game shares (Co-op, Multiplayer, Action, Indie...)."""
    picked = [t for t in tags or [] if t.lower() not in GENERIC_TAGS][:3]
    if len(picked) < 2:
        return None
    return ", ".join(picked)


def clean_text(text: str) -> str:
    text = " ".join(text.split()).rstrip(".")
    return text[:1].upper() + text[1:]


def describe(entry: Entry, taglines: dict[str, Facts], notes: dict[str, str]) -> None:
    game = entry.game
    key = str(game.appid)
    facts = taglines.get(key) or {}
    candidates = [
        ("note", notes.get(key)),
        ("tagline", facts.get("tagline")),
        ("hook", humanize_hook(game.hook) if game.hook else None),
        ("tags", tag_description(facts.get("tags"))),
    ]
    for source, text in candidates:
        if text and not forbidden_hit(text):
            entry.description, entry.source = clean_text(text), source
            return
    entry.description, entry.source = template_description(game), "template"


# ── Selection ───────────────────────────────────────────────────────────────


def build(
    dataset: dict[int, dict[str, Any]],
    pools: dict[str, list[Game]],
    awards: dict[str, Any] | None,
    mods: list[dict[str, Any]],
    overrides: dict[str, Any],
) -> tuple[dict[str, list[Entry]], dict[str, Any]]:
    excluded = set(overrides["exclude"])
    placed: set[int] = set()
    sections: dict[str, list[Entry]] = {}
    meta: dict[str, Any] = {}

    known: dict[int, Game] = {}
    for pool in pools.values():
        for g in pool:
            known.setdefault(g.appid, g)

    def with_url(g: Game) -> Game:
        row = dataset.get(g.appid)
        if row:
            g.imho_url = row["imho_url"]
        return g

    def from_dataset(appid: int) -> Game | None:
        if appid in known:
            return known[appid]
        row = dataset.get(appid)
        if not row:
            return None
        return Game(
            appid=appid,
            name=row["name"],
            year=row["release_year"] or None,
            online=row["mode"] in ("online", "both"),
            local=row["mode"] in ("local", "both"),
        )

    # Best of the year (optional): the awards ranking, in its own order.
    if awards and len(awards["items"]) >= AWARDS_MIN_ITEMS:
        entries = []
        for item in awards["items"][:10]:
            appid = int(item["appid"])
            if appid in excluded:
                continue
            base = from_dataset(appid) or Game(appid=appid, name=str(item["name"]))
            # The award is for online co-op games released that year.
            g = replace(base, year=base.year or awards["year"], online=True)
            entries.append(Entry(with_url(g)))
            placed.add(appid)
        sections["best-of-year"] = entries
        meta["awards"] = {k: awards[k] for k in ("slug", "year", "freeze_on", "min_reviews", "min_pct")}

    def fill(sid: str, cap: int, candidates: list[Game]) -> list[Entry]:
        out: list[Entry] = []
        for appid in overrides["pin"].get(sid, []):
            g = from_dataset(appid)
            if g is None:
                raise GenerationError(f"overrides.json: pinned appid {appid} is not a known co-op game")
            if appid in placed or appid in excluded or len(out) >= cap:
                continue
            out.append(Entry(with_url(g)))
            placed.add(appid)
        for g in candidates:
            if len(out) >= cap:
                break
            if g.appid in placed or g.appid in excluded:
                continue
            out.append(Entry(with_url(g)))
            placed.add(g.appid)
        return out

    coop = pools["coop"]
    sections["online"] = fill("online", 20, [g for g in coop if g.online])
    sections["couch"] = fill("couch", 20, [g for g in pools["split-screen"] if g.local])
    sections["couch-4"] = fill("couch-4", 10, pools["couch-coop-4-players"])
    sections["horror"] = fill("horror", 10, pools["coop-horror"])

    xplat = pools["cross-platform-coop"]
    head = [g.appid for g in xplat if g.appid not in excluded][:10]
    overlap = sum(1 for a in head if a in placed) / max(len(head), 1)
    meta["cross_platform_overlap"] = round(overlap, 2)
    if overlap <= CROSS_PLATFORM_MAX_OVERLAP:
        sections["cross-platform"] = fill("cross-platform", 10, xplat)

    sections["free"] = fill("free", 10, [g for g in coop if g.is_free])

    mod_entries = []
    for row in mods:
        appid = int(row["appid"])
        if appid in excluded:
            continue
        g = Game(appid=appid, name=str(row["game"]), imho_url=row["imho_url"])
        mod_entries.append(Entry(g, description=clean_text(row["note_en"]), source="mod", mod=row))
    sections["mods"] = mod_entries
    return sections, meta


# ── Rendering ───────────────────────────────────────────────────────────────


def md_escape(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>|])", r"\\\1", text)


def anchor(title: str) -> str:
    a = title.strip().lower()
    a = re.sub(r"[^\w\- ]", "", a)
    return a.replace(" ", "-")


def entry_line(e: Entry) -> str:
    g = e.game
    name = f"**[{md_escape(g.name)}]({imho_link(g)})**"
    if e.mod:
        maturity = " (beta)" if e.mod.get("maturity") == "beta" else ""
        mod = f"[{md_escape(str(e.mod['mod_name']))}]({e.mod['mod_url']}){maturity}"
        return f"- {name} · {mod} — {md_escape(e.description)} · [Steam]({steam_link(g.appid)})"
    year = f" ({g.year})" if g.year else ""
    return (
        f"- {name}{year} · {mode_label(g)} — {md_escape(e.description)}"
        f" · [Steam]({steam_link(g.appid)})"
    )


def section_title(s: Section, meta: dict[str, Any]) -> str:
    return s.title.format(year=meta.get("awards", {}).get("year", ""))


def render(sections: dict[str, list[Entry]], meta: dict[str, Any], today: str) -> str:
    present = [s for s in SECTIONS if sections.get(s.id)]
    total = sum(len(v) for v in sections.values())
    lines = [
        "# Awesome Co-op Games",
        "",
        f"A short list of the best co-op games on Steam: {total} games in {len(present)} "
        "categories, from online squads to couch co-op and fan-made co-op mods.",
        "",
        "Maintained by [imho.run](https://imho.run/?utm_source=github), generated weekly from "
        "its public data. Not affiliated with Valve.",
        "",
        "Each game links to its imho.run page (similar games, co-op details) and to its Steam "
        "store page. The rules behind every section are in "
        "[How this list is built](#how-this-list-is-built).",
        "",
        "## Contents",
        "",
    ]
    for s in present:
        title = section_title(s, meta)
        lines.append(f"- [{title}](#{anchor(title)})")
    lines += [
        "- [How this list is built](#how-this-list-is-built)",
        "- [Contributing](#contributing)",
        "- [License](#license)",
        "",
    ]
    feed_line = (
        f"New co-op releases from the last 60 days: [RSS feed]({NEW_COOP_FEED}) · "
        f"[list on imho.run]({hub_link('/discover/new-releases-coop')})"
    )
    for s in present:
        title = section_title(s, meta)
        rule = s.rule
        hub = s.hub or ""
        if s.id == "best-of-year":
            aw = meta["awards"]
            rule = rule.format(
                year=aw["year"],
                min_reviews=aw["min_reviews"],
                min_pct=aw["min_pct"],
                freeze_on=aw["freeze_on"],
            )
            hub = hub.format(slug=aw["slug"])
        lines += [f"## {title}", "", f"{rule} [Full list]({hub_link(hub)})", ""]
        lines += [entry_line(e) for e in sections[s.id]]
        lines.append("")
        if s.id == "best-of-year":
            lines += [feed_line, ""]
    if "best-of-year" not in sections:
        lines += [feed_line, ""]
    lines += [
        "## How this list is built",
        "",
        "- Source: the public [co-op datasets](https://imho.run/datasets?utm_source=github) and "
        "co-op hubs of imho.run, an independent Steam game recommender. A GitHub Action "
        "regenerates this file every week with [`generate.py`](generate.py).",
        "- Co-op means a genuine Steam co-op category (online, LAN or shared/split screen). "
        "Competitive multiplayer alone does not count.",
        "- Ranking: imho.run's Bayesian-adjusted rating with a junk filter, the same order as "
        "its co-op hubs. Each game appears in one section only, the first one it qualifies "
        "for in the order above.",
        "- Descriptions are imho.run's own one-line summaries of what players say about the "
        "game. Where there is none yet, the line lists the game's top player tags instead. "
        "Nothing is copied from the Steam store.",
        "- No review counts, percentages, store text or images are published here.",
        "- The machine-readable version of this list is [`data/list.json`](data/list.json).",
        "",
        "## Contributing",
        "",
        "Found a wrong co-op mode, a misleading description or a missing classic? Open an issue, "
        "or send a pull request that edits [`overrides.json`](overrides.json). README.md is "
        "generated, so edits to it are overwritten. See [CONTRIBUTING.md](CONTRIBUTING.md).",
        "",
        "## License",
        "",
        "The list (names, rankings, classifications and descriptions) is published under "
        "[CC BY 4.0](LICENSE-DATA): credit imho.run (https://imho.run) when you reuse it. "
        "The generator code is [MIT](LICENSE). Game names are their owners' trademarks; "
        "Steam is a trademark of Valve Corporation. This project is not affiliated with Valve "
        "or with any game's developer or publisher.",
        "",
        f"_Last generated: {today}._",
        "",
    ]
    return "\n".join(lines)


# ── Checks ──────────────────────────────────────────────────────────────────


def forbidden_hit(text: str) -> str | None:
    for pat in FORBIDDEN_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def check_readme(text: str) -> list[str]:
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith(ALLOWED_LINE_PREFIX):
            continue
        hit = forbidden_hit(line)
        if hit:
            problems.append(f"README.md:{n}: forbidden content {hit!r}: {line[:120]}")
    return problems


def validate(sections: dict[str, list[Entry]], meta: dict[str, Any]) -> None:
    problems = []
    for s in SECTIONS:
        got = len(sections.get(s.id, []))
        if s.optional and got == 0:
            continue
        need = int(s.cap * SECTION_MIN_SHARE) if s.id != "mods" else 1
        if got < need:
            problems.append(f"section {s.id}: {got} entries, minimum {need}")
    total = sum(len(v) for v in sections.values())
    if not GLOBAL_MIN <= total <= GLOBAL_MAX:
        problems.append(f"total {total} entries, allowed {GLOBAL_MIN}-{GLOBAL_MAX}")
    ranked = [e for sid, v in sections.items() if sid != "mods" for e in v]
    templated = sum(1 for e in ranked if e.source == "template")
    if ranked and templated / len(ranked) > MAX_TEMPLATE_SHARE:
        problems.append(f"{templated}/{len(ranked)} templated descriptions (max {MAX_TEMPLATE_SHARE:.0%})")
    if problems:
        raise GenerationError("; ".join(problems))


def input_hash() -> str:
    h = hashlib.sha256()
    for path in (Path(__file__), OVERRIDES):
        if path.exists():
            h.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def diff_against_previous(
    sections: dict[str, list[Entry]],
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    if not LIST_JSON.exists():
        return None, {}
    prev = json.loads(LIST_JSON.read_text(encoding="utf-8"))
    before = {a: s["id"] for s in prev.get("sections", []) for a in s["appids"]}
    after = {e.game.appid: sid for sid, v in sections.items() for e in v}
    stats = {
        "before": len(before),
        "added": len(after.keys() - before.keys()),
        "removed": len(before.keys() - after.keys()),
        "moved": sum(1 for a in after.keys() & before.keys() if after[a] != before[a]),
    }
    return prev, stats


def write_taglines(taglines: dict[str, Facts]) -> None:
    ordered = dict(sorted(taglines.items(), key=lambda kv: int(kv[0])))
    write_lf(TAGLINES, json.dumps(ordered, ensure_ascii=False, indent=1) + "\n")


def write_lf(path: Path, text: str) -> None:
    """UTF-8 with LF line endings on every OS, so local and CI runs match."""
    path.write_text(text, encoding="utf-8", newline="\n")


def days_since(day: str | None) -> int:
    try:
        then = datetime.fromisoformat(str(day)).date()
    except ValueError:
        return 10**6
    return (datetime.now(UTC).date() - then).days


# ── Main ────────────────────────────────────────────────────────────────────


def run(force: bool, tagline_budget: int, prefetch: int) -> int:
    overrides = load_overrides()
    taglines = load_taglines()

    print("Fetching imho.run public data ...")
    dataset = fetch_dataset()
    pools = {"coop": fetch_coop_pool()}
    for preset in ("split-screen", "couch-coop-4-players", "coop-horror", "cross-platform-coop"):
        pools[preset] = discover_page(preset, 100)
    awards = fetch_awards()
    mods = fetch_mods()
    print(f"  pool sizes: { {k: len(v) for k, v in pools.items()} }, mods {len(mods)}")

    sections, meta = build(dataset, pools, awards, mods, overrides)

    listed = [e.game.appid for sid, v in sections.items() if sid != "mods" for e in v]
    wanted = list(listed)
    if prefetch:
        # Warm the cache for games just below each cut, so next week's newcomers
        # already have a tagline. Used for the one-off seeding run.
        extra = [g.appid for pool in pools.values() for g in pool[:prefetch]]
        wanted += [a for a in extra if a not in set(listed)]
    calls = fetch_taglines(wanted, taglines, tagline_budget)
    print(f"  tagline calls this run: {calls}")
    if calls:
        # Keep what was fetched even if a later check fails, so the next run
        # does not ask for the same games again.
        write_taglines(taglines)

    for v in sections.values():
        for e in v:
            if e.source != "mod":
                describe(e, taglines, overrides["note"])
    validate(sections, meta)

    today = datetime.now(UTC).date().isoformat()
    readme = render(sections, meta, today)
    problems = check_readme(readme)
    if problems:
        raise GenerationError("forbidden content in README:\n" + "\n".join(problems))

    prev, stats = diff_against_previous(sections)
    digest = input_hash()
    total = sum(len(v) for v in sections.values())
    sources: dict[str, int] = {}
    for v in sections.values():
        for e in v:
            sources[e.source] = sources.get(e.source, 0) + 1
    print(f"  {total} entries; descriptions by source: {sources}")
    for s in SECTIONS:
        print(f"    {s.id:15s} {len(sections.get(s.id, []))}")
    if prev is not None:
        print(f"  vs last list: {stats}")
        churn = (stats["added"] + stats["removed"]) / max(stats["before"], 1)
        if churn > MAX_CHURN and not force:
            raise GenerationError(
                f"{churn:.0%} of entries changed (max {MAX_CHURN:.0%}). Check the data, then "
                "re-run the workflow by hand with force=true if the change is real."
            )
        changes = stats["added"] + stats["removed"] + stats["moved"]
        quiet_days = days_since(prev.get("generated_at"))
        if (
            changes < MIN_MEANINGFUL_CHANGES
            and prev.get("input_hash") == digest
            and quiet_days < MAX_QUIET_DAYS
        ):
            print(
                f"  {changes} entries changed (< {MIN_MEANINGFUL_CHANGES}), last written "
                f"{quiet_days} days ago; nothing written."
            )
            return 0

    LIST_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": today,
        "source": "https://imho.run",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "imho.run (https://imho.run)",
        "input_hash": digest,
        "sections": [
            {
                "id": s.id,
                "title": section_title(s, meta),
                "appids": [e.game.appid for e in sections[s.id]],
                "games": [
                    {
                        "appid": e.game.appid,
                        "name": e.game.name,
                        "imho_url": imho_link(e.game),
                        "steam_url": steam_link(e.game.appid),
                        "year": e.game.year,
                        "mode": None if e.mod else mode_label(e.game),
                        "description": e.description,
                    }
                    for e in sections[s.id]
                ],
            }
            for s in SECTIONS
            if sections.get(s.id)
        ],
    }
    write_lf(LIST_JSON, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
    write_lf(README, readme)
    print("  README.md and data/list.json written.")
    return 0


def check_only() -> int:
    load_overrides()
    load_taglines()
    problems = check_readme(README.read_text(encoding="utf-8")) if README.exists() else []
    for p in problems:
        print(p, file=sys.stderr)
    print("check: ok" if not problems else f"check: {len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="allow a change bigger than MAX_CHURN")
    ap.add_argument("--check", action="store_true", help="offline validation only")
    ap.add_argument("--tagline-budget", type=int, default=TAGLINE_CALLS_PER_RUN)
    ap.add_argument("--prefetch", type=int, default=0, help="also cache taglines for the top N of each pool")
    args = ap.parse_args()
    try:
        if args.check:
            return check_only()
        return run(args.force, args.tagline_budget, args.prefetch)
    except GenerationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
