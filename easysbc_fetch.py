"""
easysbc_fetch.py
-----------------
Fetches player data (price + full attributes) from easysbc.io's own
internal API -- discovered via the user's own browser DevTools Network
tab (a real JSON API, confirmed working, not scraped HTML):

    https://api-fc27.easysbc.io/players?v2&page=1&sort-meta-rating&club=241&preferredFoot=Left

CONFIRMED (from a real captured response -- see sample_easysbc_fixture.json,
verbatim from the user's own DevTools):
  - clubId/leagueId use the EXACT SAME numeric IDs as fut.gg (confirmed:
    clubId 241 = Barcelona men's on BOTH sites; resourceId 233419 =
    Raphinha on BOTH sites -- his fut.gg page at
    fut.gg/players/233419-raphinha/27-233419/ is real). This means all
    the club/league ID knowledge already built up against fut.gg
    (fetch_ratings.py's club_entity_id/league_entity_id, the men's/
    women's disambiguation work) carries over here for free.
  - Every filter the user wants is a real field in the response: price,
    rating, positions/possiblePositions/preferredPosition, skillMoves,
    weakFoot, preferredFoot, playStyles/playStylesPlus, and a 6-value
    `attributes` array. That array has NO labels in the API response --
    cross-checked against 3 real players' well-known profiles (Raphinha,
    Lamine Yamal, Balde) and [PAC, SHO, PAS, DRI, DEF, PHY] fits all
    three exactly (e.g. Balde: 90/52/75/79/77/69 -- blistering pace, weak
    shooting, solid defense, exactly matching his real playstyle). High
    confidence, not yet independently confirmed against a goalkeeper
    (GKs almost certainly report DIV/HAN/KIC/REF/SPD/POS in that slot
    instead, matching fut.gg's convention, but this hasn't been checked
    against a real GK response yet).

STRATEGY -- fetch broad, filter locally: rather than guess at easysbc's
full filter query-parameter language (stat ranges, position lists,
skill-moves minimums, etc. -- error-prone to reverse-engineer one
parameter at a time), this only relies on the ONE thing already
confirmed to work: scoping by club or league ID, paginated. Every other
filter the user wants (min pace, multi-position OR-match, skill moves,
weak foot, price range, rating range) is applied locally in Python,
exactly like the rest of this project already does for fut.gg data --
zero additional guessing needed about easysbc's query syntax for those.

UNCONFIRMED, needs a quick check before fully trusting this:
  - The query parameter name for filtering by LEAGUE. Only `club=` has
    been confirmed working; `league=` below is a guess based on the
    pattern (`club` matches the `clubId` field, so `league` presumably
    matches `leagueId`) -- NOT verified against a real request yet.
  - Whether a league/club's WOMEN's-team split uses the same IDs here as
    on fut.gg (e.g. Barcelona women = 116325). The user's own test
    already confirmed club=241 excludes the women's team, which is
    consistent with a men's/women's ID split existing here too, exactly
    like fut.gg -- just not yet confirmed the IDs literally match.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

API_BASE = "https://api-fc27.easysbc.io/players"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

OUTFIELD_ATTR_ORDER = ["PAC", "SHO", "PAS", "DRI", "DEF", "PHY"]
# Unconfirmed against a real GK response -- see module docstring.
GK_ATTR_ORDER = ["DIV", "HAN", "KIC", "REF", "SPD", "POS"]


@dataclass
class EasySBCPlayer:
    resource_id: str  # matches fut.gg's ea_id -- confirmed same numbering across both sites
    name: str
    card_name: str
    rating: int
    positions: list
    possible_positions: list
    preferred_position: str
    attributes: dict  # PAC/SHO/PAS/DRI/DEF/PHY (or GK equivalent -- see module docstring)
    country_id: str
    league_id: str
    club_id: str
    price: int
    skill_moves: int
    weak_foot: int
    preferred_foot: str
    play_styles: list = field(default_factory=list)
    play_styles_plus: list = field(default_factory=list)
    metal_id: Optional[int] = None
    player_url: str = ""  # easysbc's own face-only image -- last-resort fallback only

    def stat(self, key: str) -> Optional[int]:
        return self.attributes.get(key.upper())

    def is_gk(self) -> bool:
        return self.preferred_position == "GK" or "GK" in self.positions

    @classmethod
    def from_json(cls, d: dict) -> "EasySBCPlayer":
        raw_attrs = d.get("attributes") or []
        preferred_position = d.get("preferredPosition", "")
        positions = d.get("positions") or []
        is_gk = preferred_position == "GK" or "GK" in positions
        order = GK_ATTR_ORDER if is_gk else OUTFIELD_ATTR_ORDER
        attributes = dict(zip(order, raw_attrs))

        return cls(
            resource_id=str(d.get("resourceId")),
            name=d.get("name", ""),
            card_name=d.get("cardName", d.get("name", "")),
            rating=d.get("rating", 0),
            positions=positions,
            possible_positions=d.get("possiblePositions") or [],
            preferred_position=preferred_position,
            attributes=attributes,
            country_id=str(d.get("countryId")) if d.get("countryId") is not None else "",
            league_id=str(d.get("leagueId")) if d.get("leagueId") is not None else "",
            club_id=str(d.get("clubId")) if d.get("clubId") is not None else "",
            price=d.get("price", 0),
            skill_moves=d.get("skillMoves", 0),
            weak_foot=d.get("weakFoot", 0),
            preferred_foot=d.get("preferredFoot", ""),
            play_styles=d.get("playStyles") or [],
            play_styles_plus=d.get("playStylesPlus") or [],
            metal_id=d.get("metalId"),
            player_url=d.get("playerUrl", ""),
        )


def parse_players_response(data: dict) -> tuple[list[EasySBCPlayer], int]:
    """Returns (players, total_pages)."""
    players = [EasySBCPlayer.from_json(p) for p in data.get("players", [])]
    total_pages = data.get("totalPages", 1)
    return players, total_pages


def fetch_easysbc_page(
    club_id: Optional[str] = None,
    league_id: Optional[str] = None,
    page: int = 1,
    session: Optional[requests.Session] = None,
) -> dict:
    """One page of results. Confirmed base params (v2, page, club) come
    straight from the user's real captured request; `league` is an
    unconfirmed guess -- see module docstring."""
    sess = session or requests.Session()
    params = {"v2": "", "page": str(page), "sort-meta-rating": ""}
    if club_id:
        params["club"] = str(club_id)
    if league_id:
        params["league"] = str(league_id)  # unconfirmed param name

    resp = sess.get(API_BASE, params=params, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_all_easysbc_players(
    club_id: Optional[str] = None,
    league_id: Optional[str] = None,
    delay_seconds: float = 0.25,
    max_pages: Optional[int] = None,
    progress: bool = True,
) -> list[EasySBCPlayer]:
    """Pages through every player for the given club or league scope, OR
    -- if NEITHER club_id nor league_id is given -- every player in the
    whole database (e.g. for a global "cheapest beasts" or "top
    attackers" video that isn't limited to one club/league, using
    --positions/--min-stats/etc. as the only filters). Deliberately does
    NOT try to pass along fine-grained filters (stat minimums, positions,
    etc.) as query parameters -- fetches everything in scope, filtering
    happens locally afterward (see apply_easysbc_filters). This means
    the only easysbc-specific thing that needs to be right is the
    club/league scoping itself.

    UNSCOPED FETCHES ARE UNTESTED AT REAL SCALE: I've never actually run
    one against the live API, so I don't know how many total pages
    easysbc's full player list is, or how long it takes. To avoid a
    first try accidentally kicking off a huge, slow crawl, an unscoped
    call defaults max_pages to a conservative 20 unless you explicitly
    pass a higher value -- confirm a small run behaves as expected
    before asking for more pages."""
    unscoped = not club_id and not league_id
    used_default_cap = False
    if unscoped and not max_pages:
        # Treats BOTH "not passed at all" (None) and an explicit
        # "--max-pages 0" the same way -- confirmed a real inconsistency
        # otherwise: the GUI's "Max pages" field uses 0 to mean "use the
        # safety default", but on the raw CLI, treating 0 as falsy and
        # skipping the cap entirely (0 is not None) would have made
        # `--max-pages 0` mean "no cap at all", the OPPOSITE of what 0
        # should safely mean, and inconsistent with the GUI. Someone
        # who genuinely wants no cap should pass a large explicit
        # number instead of relying on a "0 means unlimited" shortcut.
        max_pages = 20
        used_default_cap = True
        if progress:
            print("No --club-id or --league-id given -- fetching without any club/league scope.")
            print(f"This is untested at real scale, so defaulting to a conservative {max_pages}-page "
                  f"cap for safety. Pass --max-pages explicitly for more once a small run looks right.")

    session = requests.Session()
    data = fetch_easysbc_page(club_id=club_id, league_id=league_id, page=1, session=session)
    players, real_total_pages = parse_players_response(data)

    total_pages = min(real_total_pages, max_pages) if max_pages else real_total_pages

    if progress and total_pages > 1:
        print(f"Fetching {total_pages} pages from easysbc.io...")

    for page in range(2, total_pages + 1):
        time.sleep(delay_seconds)
        data = fetch_easysbc_page(club_id=club_id, league_id=league_id, page=page, session=session)
        more_players, _ = parse_players_response(data)
        players.extend(more_players)
        if progress and page % 10 == 0:
            print(f"  ...page {page}/{total_pages} ({len(players)} players so far)")

    if progress:
        print(f"Fetched {len(players)} players from easysbc.io.")
        if total_pages < real_total_pages:
            # The real, uncapped total -- lets you see exactly how much
            # of the database this run actually covered, not just how
            # many pages were fetched. Added after a real case where 20
            # pages (400 players, easysbc's own top players by meta
            # rating) missed real matches that existed further down the
            # full list -- this makes that coverage gap visible instead
            # of silent.
            cap_label = "unscoped default" if used_default_cap else "requested"
            print(f"Note: easysbc reports {real_total_pages} total pages available for this query, but "
                  f"only {total_pages} were fetched (the {cap_label} "
                  f"cap). Every result is sorted by easysbc's own 'meta rating', so players who satisfy "
                  f"your filters but rank lower on THEIR overall scoring may not have been reached at all "
                  f"-- raise --max-pages (e.g. to {real_total_pages} for everything) if your filters are "
                  f"coming back thinner than expected on the real site.")

    return players


def parse_stat_filters(s: Optional[str]) -> dict:
    """Parses 'PAC=90,DRI=80,SHO=82' into {'PAC': 90, 'DRI': 80, 'SHO': 82}.
    Returns {} for None/empty input."""
    if not s:
        return {}
    result = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Bad stat filter '{part}' -- expected STAT=VALUE, e.g. PAC=90")
        stat, val = part.split("=", 1)
        result[stat.strip().upper()] = int(val.strip())
    return result


def apply_easysbc_filters(
    players: list[EasySBCPlayer],
    positions: Optional[list] = None,       # OR-match against possible_positions
    min_stats: Optional[dict] = None,       # e.g. {"PAC": 90, "DRI": 80}
    max_stats: Optional[dict] = None,
    min_rating: Optional[int] = None,
    max_rating: Optional[int] = None,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    min_skill_moves: Optional[int] = None,
    min_weak_foot: Optional[int] = None,
    preferred_foot: Optional[str] = None,   # "Left" or "Right"
) -> list[EasySBCPlayer]:
    """Every filter the person asked for (multi-position OR-match, min
    stat thresholds, skill moves, weak foot, preferred foot, price/rating
    range), applied locally -- see the module docstring for why this
    approach was chosen over trying to reverse-engineer easysbc's own
    filter query language."""
    result = players

    if positions:
        wanted = {p.upper() for p in positions}
        result = [p for p in result if wanted & {pp.upper() for pp in p.possible_positions}]

    for stat, min_val in (min_stats or {}).items():
        result = [p for p in result if (p.stat(stat) or 0) >= min_val]
    for stat, max_val in (max_stats or {}).items():
        result = [p for p in result if (p.stat(stat) or 0) <= max_val]

    if min_rating is not None:
        result = [p for p in result if p.rating >= min_rating]
    if max_rating is not None:
        result = [p for p in result if p.rating <= max_rating]
    if min_price is not None:
        result = [p for p in result if p.price >= min_price]
    if max_price is not None:
        result = [p for p in result if p.price <= max_price]
    if min_skill_moves is not None:
        result = [p for p in result if p.skill_moves >= min_skill_moves]
    if min_weak_foot is not None:
        result = [p for p in result if p.weak_foot >= min_weak_foot]
    if preferred_foot:
        result = [p for p in result if p.preferred_foot.lower() == preferred_foot.lower()]

    return result


def sort_easysbc_players(players: list[EasySBCPlayer], sort_by: str = "price-asc") -> list[EasySBCPlayer]:
    if sort_by == "price-asc":
        return sorted(players, key=lambda p: p.price)
    if sort_by == "price-desc":
        return sorted(players, key=lambda p: p.price, reverse=True)
    if sort_by == "rating":
        return sorted(players, key=lambda p: p.rating, reverse=True)
    if sort_by == "name":
        return sorted(players, key=lambda p: p.name)
    raise ValueError(f"Unknown sort_by: {sort_by}")


def format_price(n: int) -> str:
    """20000 -> '20,000' -- matches the exact style confirmed from a real
    easysbc.io screenshot showing their own "UT 20,000" coin display
    (full comma-separated number, not abbreviated -- a different, more
    specific reference than an earlier one that showed K/M abbreviations
    for a different app; this one is a direct screenshot of the actual
    data source this scenario pulls from, so it takes priority)."""
    return f"{n:,}"
