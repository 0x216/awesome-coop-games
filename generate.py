#!/usr/bin/env python3
"""Regenerate README.md and the full co-op lists from imho.run's public co-op data.

Python 3.12+, standard library only. Run from the repository root:

    python generate.py            # weekly regeneration (what the workflow runs)
    python generate.py --force    # also allow a change bigger than the diff guard
    python generate.py --check    # offline: validate overrides.json and the outputs

Outputs:
    README.md, data/list.json          the hand-checked short list (~90 games)
    ALL-COOP-GAMES.md,                 every co-op game in the 500+ reviews dataset,
    data/all-coop-games.{csv,json}     as a Markdown table plus CSV and JSON
    data/all-coop-games-no-floor.*     every co-op game with no review floor (CSV and
                                       JSON only); written only once imho.run serves
                                       that dataset, skipped while it is missing

Each output has its own "worth a commit" rule (see decide_write): it is written
when 3+ of its entries changed, when generate.py or overrides.json changed, or
when it has not been written for MAX_QUIET_DAYS.

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
import csv
import hashlib
import io
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
ALL_MD = ROOT / "ALL-COOP-GAMES.md"
ALL_CSV = ROOT / "data" / "all-coop-games.csv"
ALL_JSON = ROOT / "data" / "all-coop-games.json"
NOFLOOR_CSV = ROOT / "data" / "all-coop-games-no-floor.csv"
NOFLOOR_JSON = ROOT / "data" / "all-coop-games-no-floor.json"

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
COOP_DATASET_PAGE = f"{SITE}/datasets/steam-coop-games-by-mode"
# The same classification with no review floor. Optional: while imho.run does
# not serve it (404 or unreachable) the no-floor files are simply not written.
NOFLOOR_DATASET = (
    f"{SITE}/datasets/steam-coop-games-all.json",
    f"{API}/api/datasets/steam-coop-games-all.json",
)
NOFLOOR_DATASET_PAGE = f"{SITE}/datasets/steam-coop-games-all"
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
DATASET_FIELDS = ("appid", "name", "imho_url", "mode", "release_year", "reviews_bucket")
# The full lists. The 500+ dataset states its floor; the text of
# ALL-COOP-GAMES.md says "500+ reviews", so a different floor stops the run.
REVIEW_FLOOR = 500
FLOOR_PHRASE = f"{REVIEW_FLOOR}+ reviews"  # our selection rule, not a game's count
ALL_MIN_ROWS = 1500
NOFLOOR_MIN_ROWS = 5000
FLOOR_BUCKETS = ("20k+", "5k+", "1k+", "500+")  # highest first
# No-floor buckets are ranges too ("100+", "<10"...), never an exact count.
NOFLOOR_BUCKET_RE = re.compile(r"^(?:0|<\d{1,3}k?|\d{1,3}k?\+)$")
DATASET_MODES = ("both", "online", "local")  # section order in ALL-COOP-GAMES.md
MODE_DATA = {"both": "online+local", "online": "online", "local": "local"}  # as in list.json
MODE_SHOWN = {"both": "online + local", "online": "online", "local": "local"}
MODE_SECTION = {"both": "Online and local co-op", "online": "Online co-op only", "local": "Local co-op only"}
FULL_COLUMNS = ("appid", "name", "steam_url", "imho_url", "mode", "release_year", "reviews_bucket")
IMHO_URL_RE = re.compile(r"^https://imho\.run/games/\d+/[a-z0-9-]+\?utm_source=github$")
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


def dataset_rows(doc: Any, what: str, min_rows: int, buckets: str) -> list[dict[str, Any]]:
    """Validate a co-op dataset document field by field. Any surprise (missing
    field, unknown mode, a review bucket that is not a range) stops the run."""
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or len(rows) < min_rows:
        got = len(rows) if isinstance(rows, list) else "no"
        raise GenerationError(f"{what}: {got} rows, minimum {min_rows}")
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise GenerationError(f"{what}: a row is not an object")
        row = pick(raw, DATASET_FIELDS, what)
        appid, name, year, bucket = row["appid"], row["name"], row["release_year"], row["reviews_bucket"]
        if not isinstance(appid, int) or isinstance(appid, bool) or appid <= 0 or appid in seen:
            raise GenerationError(f"{what}: bad or duplicate appid {appid!r}")
        if not isinstance(name, str) or not name.strip():
            raise GenerationError(f"{what}: appid {appid} has no name")
        if row["mode"] not in DATASET_MODES:
            raise GenerationError(f"{what}: appid {appid} has unknown mode {row['mode']!r}")
        if year is not None and not (isinstance(year, int) and 1970 <= year <= 2100):
            raise GenerationError(f"{what}: appid {appid} has bad release_year {year!r}")
        if not bucket_ok(bucket, buckets):
            raise GenerationError(f"{what}: appid {appid} has bad reviews_bucket {bucket!r}")
        seen.add(appid)
        row["name"] = name.strip()
        out.append(row)
    return out


def bucket_ok(bucket: Any, buckets: str) -> bool:
    if buckets == "floor":
        return bucket in FLOOR_BUCKETS
    return bucket is None or (isinstance(bucket, str) and bool(NOFLOOR_BUCKET_RE.match(bucket)))


def fetch_dataset() -> list[dict[str, Any]]:
    doc = http_json_first(COOP_DATASET)
    if not isinstance(doc, dict) or doc.get("min_reviews") != REVIEW_FLOOR:
        raise GenerationError(
            f"co-op dataset: min_reviews is {doc.get('min_reviews') if isinstance(doc, dict) else None!r}, "
            f"expected {REVIEW_FLOOR} (ALL-COOP-GAMES.md states that floor)"
        )
    return dataset_rows(doc, "co-op dataset", ALL_MIN_ROWS, "floor")


def fetch_nofloor() -> list[dict[str, Any]] | None:
    """The no-floor dataset, or None while imho.run does not serve it. Not
    there (404, network error) means skip; there but malformed means stop."""
    for url in NOFLOOR_DATASET:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT * 3) as resp:
                body = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  no-floor dataset not available at {url}: {exc}", file=sys.stderr)
            continue
        try:
            doc = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GenerationError(f"no-floor dataset at {url}: not valid JSON ({exc})") from exc
        return dataset_rows(doc, "no-floor dataset", NOFLOOR_MIN_ROWS, "any")
    return None


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
    # The game name opens its Steam store page (what a reader of a game list
    # expects); imho.run is only the secondary "similar games" link.
    name = f"**[{md_escape(g.name)}]({steam_link(g.appid)})**"
    similar = f"[similar games]({imho_link(g)})"
    if e.mod:
        maturity = " (beta)" if e.mod.get("maturity") == "beta" else ""
        mod = f"[{md_escape(str(e.mod['mod_name']))}]({e.mod['mod_url']}){maturity}"
        return f"- {name} · {mod} — {md_escape(e.description)} · {similar}"
    year = f" ({g.year})" if g.year else ""
    return f"- {name}{year} · {mode_label(g)} — {md_escape(e.description)} · {similar}"


def section_title(s: Section, meta: dict[str, Any]) -> str:
    return s.title.format(year=meta.get("awards", {}).get("year", ""))


def full_list_pointer(has_nofloor: bool) -> str:
    line = (
        "Want every co-op game, not just the picks? See [ALL-COOP-GAMES.md](ALL-COOP-GAMES.md): "
        f"all co-op games on Steam with {FLOOR_PHRASE} (CSV/JSON in [data/](data/))."
    )
    if has_nofloor:
        line += (
            " With no review floor at all: [CSV](data/all-coop-games-no-floor.csv) · "
            "[JSON](data/all-coop-games-no-floor.json)."
        )
    return line


def render(
    sections: dict[str, list[Entry]], meta: dict[str, Any], today: str, has_nofloor: bool
) -> str:
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
        full_list_pointer(has_nofloor),
        "",
        "Each game name opens its Steam store page; \"similar games\" opens a list of games "
        "like it on imho.run. The rules behind every section are in "
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
        "- The machine-readable version of this list is [`data/list.json`](data/list.json). "
        "The full co-op list behind it is [ALL-COOP-GAMES.md](ALL-COOP-GAMES.md).",
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


# ── Full lists ──────────────────────────────────────────────────────────────


def bucket_rank(bucket: str | None) -> float:
    if not bucket:
        return -1.0
    m = re.match(r"^(<)?(\d+)(k)?\+?$", bucket)
    if not m:
        return -1.0
    n = int(m.group(2)) * (1000 if m.group(3) else 1)
    return n - 0.5 if m.group(1) else float(n)


def full_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whitelisted output rows, grouped by mode, then review bucket (highest
    first), then name."""
    order = {m: i for i, m in enumerate(DATASET_MODES)}
    ranked = sorted(
        rows,
        key=lambda r: (order[r["mode"]], -bucket_rank(r["reviews_bucket"]), r["name"].casefold(), r["appid"]),
    )
    out = []
    for r in ranked:
        game = Game(appid=r["appid"], name=r["name"])
        out.append(
            {
                "appid": r["appid"],
                "name": r["name"],
                "steam_url": steam_link(r["appid"]),
                "imho_url": imho_link(game),
                "mode": MODE_DATA[r["mode"]],
                "release_year": r["release_year"],
                "reviews_bucket": r["reviews_bucket"],
            }
        )
    return out


def render_all_md(rows: list[dict[str, Any]], today: str, has_nofloor: bool) -> str:
    data_mode = {v: k for k, v in MODE_DATA.items()}
    by_mode: dict[str, list[dict[str, Any]]] = {m: [] for m in DATASET_MODES}
    for r in rows:
        by_mode[data_mode[r["mode"]]].append(r)
    lines = [
        f"# All co-op games on Steam with {FLOOR_PHRASE}",
        "",
        f"{len(rows)} games: every Steam game with genuine co-op and {FLOOR_PHRASE} in "
        "imho.run's data. The hand-picked short list is in the [README](README.md).",
        "",
        f"- **Why {FLOOR_PHRASE}:** Steam has thousands of small co-op releases that few people "
        "have played. The floor keeps this list to games with enough players behind them to judge.",
        "- **Mode:** online, local or online + local, classified by imho.run from each game's "
        "Steam co-op categories and community tags; competitive multiplayer alone does not count "
        f"and LAN-only games are left out ([classification rules]({COOP_DATASET_PAGE}?{UTM})).",
        "- **Reviews** is a bucket (500+, 1k+, 5k+, 20k+), not a review count.",
        "- **Game** opens the Steam store page; **Similar** opens games like it on imho.run.",
        "- The same rows as data: [CSV](data/all-coop-games.csv) · [JSON](data/all-coop-games.json).",
    ]
    if has_nofloor:
        lines.append(
            "- **No review floor:** every co-op game imho.run classifies, as "
            "[CSV](data/all-coop-games-no-floor.csv) · [JSON](data/all-coop-games-no-floor.json) "
            f"only, too long for a table here ([dataset page]({NOFLOOR_DATASET_PAGE}?{UTM}))."
        )
    lines += [
        "- **License:** [CC BY 4.0](LICENSE-DATA); credit imho.run (https://imho.run) when you "
        "reuse it. Game names are their owners' trademarks. Not affiliated with Valve.",
        f"- Regenerated weekly from imho.run's [public dataset]({COOP_DATASET_PAGE}?{UTM}) by "
        f"[`generate.py`](generate.py). Last generated: {today}.",
        "",
        "## Contents",
        "",
    ]
    for m in DATASET_MODES:
        title = MODE_SECTION[m]
        lines.append(f"- [{title}](#{anchor(title)}) ({len(by_mode[m])})")
    lines.append("")
    for m in DATASET_MODES:
        lines += [
            f"## {MODE_SECTION[m]}",
            "",
            f"{len(by_mode[m])} games, by review bucket (highest first), then by name.",
            "",
            "| Game | Year | Mode | Reviews | Similar |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in by_mode[m]:
            year = r["release_year"] or ""
            lines.append(
                f"| [{md_escape(r['name'])}]({r['steam_url']}) | {year} | {MODE_SHOWN[m]} | "
                f"{r['reviews_bucket']} | [similar]({r['imho_url']}) |"
            )
        lines.append("")
    return "\n".join(lines)


def render_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(FULL_COLUMNS)
    for r in rows:
        w.writerow(["" if r[c] is None else r[c] for c in FULL_COLUMNS])
    return buf.getvalue()


def render_full_json(
    rows: list[dict[str, Any]], today: str, digest: str, page: str, floor: int | None
) -> str:
    """One row per line, so a weekly diff shows exactly which games changed."""
    meta = {
        "generated_at": today,
        "source": page,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "imho.run (https://imho.run)",
        "review_floor": floor,
        "input_hash": digest,
        "count": len(rows),
        "columns": list(FULL_COLUMNS),
    }
    head = json.dumps(meta, ensure_ascii=False, indent=1)[:-2]  # drop the closing "\n}"
    body = ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in rows)
    return f'{head},\n "rows": [\n{body}\n ]\n}}\n'


def check_full_rows(rows: Any, fname: str, buckets: str) -> list[str]:
    """Every field of every row must be one we mean to publish."""
    if not isinstance(rows, list) or not rows:
        return [f"{fname}: no rows"]
    problems = []
    for i, r in enumerate(rows, 1):
        where = f"{fname}: row {i}"
        if not isinstance(r, dict) or tuple(r) != FULL_COLUMNS:
            problems.append(f"{where}: columns are not {FULL_COLUMNS}")
            continue
        appid = r["appid"]
        if not isinstance(appid, int) or appid <= 0:
            problems.append(f"{where}: bad appid {appid!r}")
            continue
        if not isinstance(r["name"], str) or not r["name"].strip():
            problems.append(f"{where}: empty name")
        if r["steam_url"] != steam_link(appid):
            problems.append(f"{where}: bad steam_url {r['steam_url']!r}")
        imho = r["imho_url"]
        if not (isinstance(imho, str) and IMHO_URL_RE.match(imho) and f"/games/{appid}/" in imho):
            problems.append(f"{where}: bad imho_url {imho!r}")
        if r["mode"] not in MODE_DATA.values():
            problems.append(f"{where}: bad mode {r['mode']!r}")
        year = r["release_year"]
        if year is not None and not (isinstance(year, int) and 1970 <= year <= 2100):
            problems.append(f"{where}: bad release_year {year!r}")
        if not bucket_ok(r["reviews_bucket"], buckets):
            problems.append(f"{where}: bad reviews_bucket {r['reviews_bucket']!r}")
    return problems


def parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.reader(io.StringIO(text))
    header = tuple(next(reader, ()))
    if header != FULL_COLUMNS:
        raise ValueError(f"header is {header}")
    out = []
    for cells in reader:
        if len(cells) != len(FULL_COLUMNS):
            raise ValueError(f"row with {len(cells)} cells")
        r: dict[str, Any] = dict(zip(FULL_COLUMNS, cells, strict=True))
        r["appid"] = int(r["appid"])
        r["release_year"] = int(r["release_year"]) if r["release_year"] else None
        r["reviews_bucket"] = r["reviews_bucket"] or None
        out.append(r)
    return out


def check_full_files(
    md: str | None, csv_text: str, json_text: str, prefix: str, buckets: str
) -> list[str]:
    """Checks the files as they are written (or as committed, with --check)."""
    problems = check_text(md, ALL_MD.name) if md is not None else []
    try:
        problems += check_full_rows(json.loads(json_text).get("rows"), f"{prefix}.json", buckets)
    except (json.JSONDecodeError, AttributeError) as exc:
        problems.append(f"{prefix}.json: unreadable ({exc})")
    try:
        problems += check_full_rows(parse_csv(csv_text), f"{prefix}.csv", buckets)
    except (ValueError, csv.Error) as exc:
        problems.append(f"{prefix}.csv: unreadable ({exc})")
    return problems


def diff_rows(path: Path, rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    if not path.exists():
        return None, {}
    prev = json.loads(path.read_text(encoding="utf-8"))

    def key(r: dict[str, Any]) -> tuple[Any, ...]:
        return (r["name"], r["mode"], r["release_year"], r["reviews_bucket"])

    before = {r["appid"]: key(r) for r in prev.get("rows", [])}
    after = {r["appid"]: key(r) for r in rows}
    stats = {
        "before": len(before),
        "added": len(after.keys() - before.keys()),
        "removed": len(before.keys() - after.keys()),
        "changed": sum(1 for a in after.keys() & before.keys() if after[a] != before[a]),
    }
    return prev, stats


def decide_write(
    label: str,
    prev: dict[str, Any] | None,
    stats: dict[str, int],
    digest: str,
    force: bool,
    structural: bool = False,
) -> bool:
    """The commit rule, the same for every output: write when 3+ entries were
    added, removed or changed (moved section / mode / bucket), when the
    generator or overrides changed, when the output's layout must change
    (structural), or when it has been quiet for MAX_QUIET_DAYS. Adding or
    removing more than MAX_CHURN of the entries stops the run unless forced."""
    if prev is None:
        print(f"  {label}: no previous version; writing it")
        return True
    print(f"  {label} vs last: {stats}")
    churn = (stats["added"] + stats["removed"]) / max(stats["before"], 1)
    if churn > MAX_CHURN and not force:
        raise GenerationError(
            f"{label}: {churn:.0%} of entries changed (max {MAX_CHURN:.0%}). Check the data, "
            "then re-run the workflow by hand with force=true if the change is real."
        )
    changes = stats["added"] + stats["removed"] + stats.get("moved", 0) + stats.get("changed", 0)
    quiet_days = days_since(prev.get("generated_at"))
    if (
        changes < MIN_MEANINGFUL_CHANGES
        and prev.get("input_hash") == digest
        and quiet_days < MAX_QUIET_DAYS
        and not structural
    ):
        print(
            f"  {label}: {changes} entries changed (< {MIN_MEANINGFUL_CHANGES}), last written "
            f"{quiet_days} days ago; not written."
        )
        return False
    return True


# ── Checks ──────────────────────────────────────────────────────────────────


def forbidden_hit(text: str) -> str | None:
    for pat in FORBIDDEN_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


# The Game cell of an ALL-COOP-GAMES.md table row. Game names are factual
# identifiers ("100% Orange Juice"), so the check skips them; every other cell
# is checked, and the data files are checked field by field.
NAME_CELL_RE = re.compile(r"^\| \[(?:\\.|[^\\\]])*\]\(https://store\.steampowered\.com/app/\d+/\) \|")


def check_text(text: str, fname: str) -> list[str]:
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith(ALLOWED_LINE_PREFIX):
            continue
        # "500+ reviews" is the list's selection rule, not a game's count.
        body = NAME_CELL_RE.sub("|", line).replace(FLOOR_PHRASE, "")
        hit = forbidden_hit(body)
        if hit:
            problems.append(f"{fname}:{n}: forbidden content {hit!r}: {line[:120]}")
    return problems


def check_readme(text: str) -> list[str]:
    return check_text(text, README.name)


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
    coop_rows = fetch_dataset()
    dataset = {r["appid"]: r for r in coop_rows}
    nofloor = fetch_nofloor()
    print(f"  co-op dataset {len(coop_rows)} rows; no-floor dataset "
          f"{'not available' if nofloor is None else f'{len(nofloor)} rows'}")
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
    digest = input_hash()
    # The no-floor files stay linked once they exist, even in a week when the
    # dataset is briefly unreachable (their last version is kept).
    has_nofloor = nofloor is not None or NOFLOOR_JSON.exists()

    # Render and check everything first; nothing is written unless all of it passes.
    readme = render(sections, meta, today, has_nofloor)
    problems = check_readme(readme)

    full = full_rows(coop_rows)
    all_md = render_all_md(full, today, has_nofloor)
    all_csv = render_csv(full)
    all_json = render_full_json(full, today, digest, COOP_DATASET_PAGE, REVIEW_FLOOR)
    problems += check_full_files(all_md, all_csv, all_json, "data/all-coop-games", "floor")

    nf_rows = full_rows(nofloor) if nofloor is not None else None
    if nf_rows is not None:
        nf_csv = render_csv(nf_rows)
        nf_json = render_full_json(nf_rows, today, digest, NOFLOOR_DATASET_PAGE, None)
        problems += check_full_files(None, nf_csv, nf_json, "data/all-coop-games-no-floor", "any")
    if problems:
        raise GenerationError("publishing rules broken:\n" + "\n".join(problems[:50]))

    total = sum(len(v) for v in sections.values())
    sources: dict[str, int] = {}
    for v in sections.values():
        for e in v:
            sources[e.source] = sources.get(e.source, 0) + 1
    print(f"  {total} entries; descriptions by source: {sources}")
    for s in SECTIONS:
        print(f"    {s.id:15s} {len(sections.get(s.id, []))}")

    def mentions_nofloor(path: Path) -> bool | None:
        return NOFLOOR_JSON.name in path.read_text(encoding="utf-8") if path.exists() else None

    # Each output decides on its own; all guards run before any file is written.
    prev, stats = diff_against_previous(sections)
    old_readme = README.read_text(encoding="utf-8") if README.exists() else ""
    readme_layout = mentions_nofloor(README) != has_nofloor or ALL_MD.name not in old_readme
    write_readme = decide_write("README list", prev, stats, digest, force, readme_layout)
    prev_all, stats_all = diff_rows(ALL_JSON, full)
    all_layout = mentions_nofloor(ALL_MD) != has_nofloor or not ALL_CSV.exists()
    write_all = decide_write("full list", prev_all, stats_all, digest, force, all_layout)
    write_nf = False
    if nf_rows is not None:
        prev_nf, stats_nf = diff_rows(NOFLOOR_JSON, nf_rows)
        write_nf = decide_write(
            "no-floor list", prev_nf, stats_nf, digest, force, not NOFLOOR_CSV.exists()
        )

    LIST_JSON.parent.mkdir(parents=True, exist_ok=True)
    if write_all:
        write_lf(ALL_MD, all_md)
        write_lf(ALL_CSV, all_csv)
        write_lf(ALL_JSON, all_json)
        print(f"  ALL-COOP-GAMES.md and data/all-coop-games.csv/.json written ({len(full)} games).")
    if write_nf and nf_rows is not None:
        write_lf(NOFLOOR_CSV, nf_csv)
        write_lf(NOFLOOR_JSON, nf_json)
        print(f"  data/all-coop-games-no-floor.csv/.json written ({len(nf_rows)} games).")
    if not write_readme:
        return 0

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

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    if ALL_MD.exists() or ALL_CSV.exists() or ALL_JSON.exists():
        if not (ALL_MD.exists() and ALL_CSV.exists() and ALL_JSON.exists()):
            problems.append("full list: ALL-COOP-GAMES.md, data/all-coop-games.csv and .json go together")
        else:
            problems += check_full_files(
                read(ALL_MD), read(ALL_CSV), read(ALL_JSON), "data/all-coop-games", "floor"
            )
    if NOFLOOR_CSV.exists() or NOFLOOR_JSON.exists():
        if not (NOFLOOR_CSV.exists() and NOFLOOR_JSON.exists()):
            problems.append("no-floor list: the .csv and .json go together")
        else:
            problems += check_full_files(
                None, read(NOFLOOR_CSV), read(NOFLOOR_JSON), "data/all-coop-games-no-floor", "any"
            )
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
