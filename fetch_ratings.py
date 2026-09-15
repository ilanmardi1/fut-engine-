"""
fetch_ratings.py
-----------------
Pulls player ratings + card-render image URLs from fut.gg for a given
EA FC game year (e.g. 27 for FC27, 26 for FC26).

WHY FUT.GG: unlike futbin.com (which blocks basic scraping / bot-detects
plain HTTP requests) and the official EA ratings page (which loads its
table via client-side JS, so a plain GET returns an empty table), fut.gg
server-renders the full ratings table with every stat AND a direct CDN
image URL for each player's card, in one plain HTML response. That means
this module works with plain `requests` -- no headless browser needed.

STATUS: the parsing logic below is built and unit-tested against a real,
saved sample of fut.gg's page output (see test_fetch_ratings.py). The
network call itself (requests.get against the live site) has NOT been
exercised end-to-end from this environment, since this sandbox has no
internet access. Run test_fetch_ratings.py first to confirm the parser
still matches fut.gg's current markup, then try fetch_top_ratings()
live -- if fut.gg has changed their page structure since this was
written, the SELECTOR / REGEX section below is what to adjust.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

OUTFIELD_STATS = ["PAC", "SHO", "PAS", "DRI", "DEF", "PHY"]
GK_STATS = ["DIV", "HAN", "KIC", "REF", "SPD", "POS"]


@dataclass
class PlayerRating:
    ea_id: str
    name: str
    club: str
    position: str
    ovr: int
    stats: dict  # e.g. {"PAC": 96, "SHO": 91, ...} or GK stats
    rank: Optional[int] = None
    card_image_url: str = ""       # transparent face/portrait cutout only
    nation_icon_url: str = ""
    club_icon_url: str = ""
    league_icon_url: str = ""
    rarity_frame_url: str = ""     # the gold shield/frame graphic behind the face
    detail_url: str = ""
    game_year: str = ""

    def stat(self, key: str) -> Optional[int]:
        return self.stats.get(key.upper())

    def is_gk(self) -> bool:
        return "DIV" in self.stats

    def to_dict(self) -> dict:
        return {
            "ea_id": self.ea_id, "name": self.name, "club": self.club,
            "position": self.position, "ovr": self.ovr, "stats": self.stats,
            "rank": self.rank, "card_image_url": self.card_image_url,
            "nation_icon_url": self.nation_icon_url, "club_icon_url": self.club_icon_url,
            "league_icon_url": self.league_icon_url,
            "rarity_frame_url": self.rarity_frame_url,
            "detail_url": self.detail_url, "game_year": self.game_year,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerRating":
        # tolerate old cache files saved before nation/club/league/rarity
        # icon fields existed -- default them to "" instead of a hard
        # KeyError.
        d = dict(d)
        for key in ("nation_icon_url", "club_icon_url", "league_icon_url", "rarity_frame_url"):
            d.setdefault(key, "")
        return cls(**d)


# fut.gg can show the SAME display name ("FC Barcelona", "Arsenal", ...)
# for two entirely different underlying club entities that happen to
# share a name -- confirmed live: FC Barcelona's men's squad (Pedri,
# Lamine Yamal, ...) and women's squad (Aitana Bonmatí, Ewa Pajor, ...)
# both show as plain "FC Barcelona" in the player text, but their club
# badge IMAGE URLs use different numeric IDs (241 vs 116325). fut.gg's
# own "Gender" filter/counter is unreliable right now -- it showed
# "Women (0)" on that exact page while visibly listing Bonmatí -- so
# this ID, not any gender label, is the reliable way to tell the squads
# apart. See resolve_club_players() in build_video.py for where this is
# actually used.
CLUB_ENTITY_ID_RE = re.compile(r"/club/(\d+)\.")


def club_entity_id(club_icon_url: str) -> Optional[str]:
    m = CLUB_ENTITY_ID_RE.search(club_icon_url or "")
    return m.group(1) if m else None


# Same idea as club_entity_id, for filtering a comparison video by an
# entire league instead of one club. Unlike clubs, we don't have a
# readable league NAME anywhere in the scraped data -- fut.gg's player
# listing shows a league badge IMAGE with no accompanying text label
# (compare to club, which does show as visible text, e.g. "FC
# Barcelona"). So there's no name-based lookup possible here, only ID.
# Confirmed from real data gathered across this project: league id 13 =
# Premier League (Haaland/Man City, Bruno Fernandes/Man Utd both showed
# league/13), league id 53 = La Liga (the whole Barcelona men's squad
# fixture showed league/53). Every other league needs to be identified
# by the person via summarize_leagues()/print_league_summary() in
# build_video.py, the same self-service pattern used for the club
# men's/women's split -- I don't want to guess/hardcode IDs I haven't
# actually verified against real data.
LEAGUE_ENTITY_ID_RE = re.compile(r"/league/(\d+)\.")


def league_entity_id(league_icon_url: str) -> Optional[str]:
    m = LEAGUE_ENTITY_ID_RE.search(league_icon_url or "")
    return m.group(1) if m else None


def save_ratings_db(players: list[PlayerRating], path: str) -> None:
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in players], f)
    print(f"Saved {len(players)} players to {path}")


def load_ratings_db(path: str) -> list[PlayerRating]:
    import json
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [PlayerRating.from_dict(d) for d in data]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
# fut.gg's rendered page (once stripped of HTML tags into a plain text
# stream) repeats one block per player, in this order:
#
#   [<Name> FC <year> official rating](<detail_url>)
#   #<rank><Name>Official
#   <club badge><Club Name>
#   <placeholder alt text>
#   <rarity badge><card image alt=Name>(<card_image_url>)
#   <Name>
#   <ovr>
#   <POS>
#   PAC <val> SHO <val> PAS <val> DRI <val> DEF <val> PHY <val>
#     (or DIV/HAN/KIC/REF/SPD/POS for goalkeepers)
#   <nation/league/club badges>
#
# We anchor on the "player-item" CDN image URL (unique per player, always
# present) and pull rank/name/club/position/stats from the text immediately
# around it, rather than depending on exact CSS class names -- markup
# (div/class names) is far more likely to change than this visible text
# order, and we have no way to inspect the real class names from here.

PLAYER_ITEM_IMG_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<url>https://game-assets\.fut\.gg/cdn-cgi/image/[^)]*?/player-item/(?P<year>\d+)-(?P<ea_id>\d+)\.[a-f0-9]+\.webp)\)"
)

# fut.gg's own links are RELATIVE ("/players/227203-.../27-227203/"), not
# absolute -- the optional (?:https://www\.fut\.gg)? below accepts either
# form, since a live fetch confirmed the relative form is what's actually
# served.
DETAIL_LINK_RE = re.compile(
    r"\[(?P<linktext>[^\]]*FC \d+ official rating)\]\((?P<detail_url>(?:https://www\.fut\.gg)?/players/\d+-[\w-]+/\d+-\d+/)\)"
)

# The rank/name/"Official" text is spread across separate lines on the
# real page ("#\n1\nAlexia Putellas\nOfficial"), not run together --
# \s* / [\s\S] tolerate the newlines in between.
RANK_NAME_RE = re.compile(r"#\s*(?P<rank>\d+)\s*(?P<name>[\s\S]+?)\s*Official")

# \s+ (not \n+) as the separator throughout: tolerant of the real page's
# one-item-per-line rendering, including any blank/whitespace-only lines.
STAT_BLOCK_RE = re.compile(
    r"(?P<name2>[^\n]+)\s+(?P<ovr>\d{2,3})\s+(?P<pos>[A-Z]{2,3})\s+"
    r"(?P<s1label>PAC|DIV)\s+(?P<s1val>\d{1,3})\s+"
    r"(?P<s2label>SHO|HAN)\s+(?P<s2val>\d{1,3})\s+"
    r"(?P<s3label>PAS|KIC)\s+(?P<s3val>\d{1,3})\s+"
    r"(?P<s4label>DRI|REF)\s+(?P<s4val>\d{1,3})\s+"
    r"(?P<s5label>DEF|SPD)\s+(?P<s5val>\d{1,3})\s+"
    r"(?P<s6label>PHY|POS)\s+(?P<s6val>\d{1,3})"
)

CLUB_LINE_RE = re.compile(r"\]\((?P<club_icon_url>https://game-assets\.fut\.gg[^)]*?/club/[^)]*)\)\s*(?P<club>[^\n!]+)")

NATION_ICON_RE = re.compile(r"!\[Nation\]\((?P<url>https://game-assets\.fut\.gg[^)]+)\)")

LEAGUE_ICON_RE = re.compile(r"!\[League\]\((?P<url>https://game-assets\.fut\.gg[^)]+)\)")

RARITY_FRAME_RE = re.compile(r"!\[\]\((?P<url>https://game-assets\.fut\.gg[^)]*?/rarities-level-\d+-large/[^)]*)\)")

# Footer text: "10000 official ratings · page 1 of 417" -- used to
# discover how many pages to crawl for a full local database.
TOTAL_PAGES_RE = re.compile(r"[\d,]+\s+official ratings\s*[·\-]\s*page\s+\d+\s+of\s+(?P<total_pages>\d+)")


def _split_into_player_chunks(text: str) -> list[str]:
    """Split the page text into one chunk per player, using the detail-link
    line (guaranteed unique, one per player) as the boundary."""
    starts = [m.start() for m in DETAIL_LINK_RE.finditer(text)]
    starts.append(len(text))
    return [text[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


def parse_ratings_page(text: str) -> list[PlayerRating]:
    """Parse fut.gg's rendered ratings-page text into PlayerRating objects.

    `text` should be the page reduced to a readable text stream (this is
    what you get from BeautifulSoup(html, "html.parser") run through a
    markdown-style extractor, or from web_fetch's markdown output -- both
    preserve the same reading order fut.gg renders in).
    """
    results: list[PlayerRating] = []

    for chunk in _split_into_player_chunks(text):
        detail_m = DETAIL_LINK_RE.search(chunk)
        img_m = PLAYER_ITEM_IMG_RE.search(chunk)
        rank_m = RANK_NAME_RE.search(chunk)
        club_m = CLUB_LINE_RE.search(chunk)
        stat_m = STAT_BLOCK_RE.search(chunk)
        nation_m = NATION_ICON_RE.search(chunk)
        league_m = LEAGUE_ICON_RE.search(chunk)
        rarity_m = RARITY_FRAME_RE.search(chunk)

        if not (img_m and stat_m):
            # Doesn't look like a player block (e.g. leading page chrome) -- skip.
            continue

        labels = [stat_m.group(f"s{i}label") for i in range(1, 7)]
        vals = [int(stat_m.group(f"s{i}val")) for i in range(1, 7)]
        stats = dict(zip(labels, vals))

        detail_url = detail_m.group("detail_url") if detail_m else ""
        if detail_url.startswith("/"):
            detail_url = "https://www.fut.gg" + detail_url

        results.append(
            PlayerRating(
                ea_id=img_m.group("ea_id"),
                name=stat_m.group("name2").strip(),
                club=club_m.group("club").strip() if club_m else "",
                position=stat_m.group("pos"),
                ovr=int(stat_m.group("ovr")),
                stats=stats,
                rank=int(rank_m.group("rank")) if rank_m else None,
                card_image_url=img_m.group("url"),
                nation_icon_url=nation_m.group("url") if nation_m else "",
                club_icon_url=club_m.group("club_icon_url") if club_m else "",
                league_icon_url=league_m.group("url") if league_m else "",
                rarity_frame_url=rarity_m.group("url") if rarity_m else "",
                detail_url=detail_url,
                game_year=img_m.group("year"),
            )
        )

    return results


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

RATINGS_URLS = {
    "top100": "https://www.fut.gg/players/rating-predictions/top-100/",
    "all": "https://www.fut.gg/players/rating-predictions/",
    "upgrades": "https://www.fut.gg/players/rating-predictions/upgrades/",
    "downgrades": "https://www.fut.gg/players/rating-predictions/downgrades/",
}


def fetch_page_text(url: str, session: Optional[requests.Session] = None) -> str:
    """Fetch a fut.gg page and reduce it to a text stream suitable for
    parse_ratings_page(). Uses BeautifulSoup if available for cleaner
    extraction; falls back to a crude tag-stripper otherwise."""
    sess = session or requests.Session()
    resp = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    html = resp.text

    try:
        from bs4 import BeautifulSoup  # local import: optional dependency

        soup = BeautifulSoup(html, "html.parser")
        # Preserve image alt text + href targets, since the parser keys off
        # them -- get_text() alone would drop the URLs. We reconstruct a
        # markdown-ish stream: for every <a> and <img>, emit the
        # [text](url) / ![alt](src) form, then fall through to get_text()
        # for everything else.
        for img in soup.find_all("img"):
            alt = img.get("alt", "")
            src = img.get("src", "")
            img.replace_with(f"![{alt}]({src})")
        for a in soup.find_all("a"):
            href = a.get("href", "")
            label = a.get_text()
            a.replace_with(f"[{label}]({href})")
        return soup.get_text("\n")
    except ImportError:
        # crude fallback: strip tags, keep raw text only (image URLs lost --
        # only useful for stat parsing, not card images).
        return re.sub(r"<[^>]+>", "\n", html)


def fetch_top_ratings(game_year: str = "27", limit: int = 100) -> list[PlayerRating]:
    """Fetch the top N overall-rated players for a given game year. Fine
    for a quick smoke test, but NOT for 'top N by <stat>' -- this is
    always OVR-sorted, so a fast-but-lower-OVR player would never show up
    here even though they belong in a 'fastest players' video. Use
    fetch_all_ratings() + a local cache for anything stat-based."""
    url = RATINGS_URLS["top100"] if limit <= 100 else RATINGS_URLS["all"]
    text = fetch_page_text(url)
    players = parse_ratings_page(text)
    return players[:limit]


def fetch_all_ratings(
    game_year: str = "27",
    delay_seconds: float = 0.4,
    max_pages: Optional[int] = None,
    progress: bool = True,
) -> list[PlayerRating]:
    """Crawls fut.gg's full ratings list (every page) and returns every
    player parsed. This is the correct source for ANY 'top/bottom N by
    <stat>' video -- the site is always OVR-sorted, so only a full pull
    (not just the top 100) can tell you who's really fastest/slowest/etc.

    NOT yet exercised live (no network in this sandbox) -- the per-page
    parsing is the same tested parse_ratings_page() used everywhere else,
    so the main untested part is purely the pagination loop itself
    (fetch page 1 -> read 'page 1 of N' -> fetch pages 2..N). Expect this
    to take a few minutes for the ~417 pages (10,000 players) at the
    default delay; raise delay_seconds if fut.gg starts rate-limiting you,
    or set max_pages to a small number first to sanity check the loop.

    Run this via build_ratings_db.py, which saves the result to a local
    JSON file -- you should only need to re-run the full crawl
    occasionally (e.g. after EA reveals a new batch of ratings), not
    before every video.
    """
    session = requests.Session()
    base_url = RATINGS_URLS["all"]

    first_text = fetch_page_text(base_url, session=session)
    all_players = list(parse_ratings_page(first_text))

    total_pages_m = TOTAL_PAGES_RE.search(first_text)
    total_pages = int(total_pages_m.group("total_pages")) if total_pages_m else 1
    if max_pages:
        total_pages = min(total_pages, max_pages)

    if progress:
        print(f"page 1/{total_pages}: {len(all_players)} players (running total {len(all_players)})")

    for page in range(2, total_pages + 1):
        time.sleep(delay_seconds)
        page_url = f"{base_url}?page={page}"
        try:
            text = fetch_page_text(page_url, session=session)
            page_players = parse_ratings_page(text)
        except Exception as e:
            print(f"  page {page}/{total_pages}: FAILED ({e}) -- skipping")
            continue
        all_players.extend(page_players)
        if progress:
            print(f"page {page}/{total_pages}: {len(page_players)} players (running total {len(all_players)})")

    return all_players


def fetch_upgrades_or_downgrades(kind: str = "upgrades", limit: int = 30) -> list[PlayerRating]:
    """Shortcut for the FC26-vs-FC27 comparison scenario: fut.gg already
    publishes pre-computed upgrade/downgrade lists, which may save you from
    having to fetch both years and diff them yourself. Verify the returned
    fields include both old and new OVR before relying on this -- the
    current parser only captures the single OVR/stat block shown per
    player, so this may need a small extension once you see the real page
    (the upgrades page likely shows two OVR numbers per player, e.g. "82 -> 85")."""
    if kind not in ("upgrades", "downgrades"):
        raise ValueError("kind must be 'upgrades' or 'downgrades'")
    text = fetch_page_text(RATINGS_URLS[kind])
    players = parse_ratings_page(text)
    return players[:limit]


def higher_res_card_url(card_image_url: str, width: int = 512) -> str:
    """fut.gg serves card art through an image-resizing CDN
    (.../cdn-cgi/image/quality=85,format=auto,width=300/...). Bumping the
    width= param gets you a larger source image for compositing -- no
    extra endpoint needed."""
    return re.sub(r"width=\d+", f"width={width}", card_image_url)


# ---------------------------------------------------------------------------
# Comparison scenario (FC26 vs FC27): each player's OWN detail page
# already embeds their prior-year card image AND the exact stat deltas,
# server-side, in one response -- confirmed against a real fetch of
# fut.gg/players/239085-erling-haaland/27-239085/. No separate FC26 page
# fetch is needed; you already have detail_url for every player from the
# main ratings crawl (fetch_all_ratings / build_ratings_db.py).
# ---------------------------------------------------------------------------

OLD_CARD_IMG_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<url>https://game-assets\.fut\.gg/cdn-cgi/image/[^)]*?/player-item-card/(?P<year>\d+)-(?P<ea_id>\d+)\.[a-f0-9]+\.webp)\)"
)

# The fully-flattened, official-looking card fut.gg generates for social
# link previews (Twitter/Discord/etc.) -- this is a REAL rendered card
# image (frame + face + stats baked in by fut.gg itself), not just the
# bare face cutout that PLAYER_ITEM_IMG_RE captures. Only present on a
# player's own detail page (in the <head> meta tags), not on the bulk
# listing pages -- so this costs one extra request per player actually
# used in a video, not per player in the whole 10k-player crawl.
SOCIAL_CARD_RE = re.compile(
    r"meta-og:image:\s*(?P<url>https://game-assets\.fut\.gg/cdn-cgi/image/[^\s]*?/player-item-social-small/(?P<year>\d+)-(?P<ea_id>\d+)\.[a-f0-9]+\.webp)"
)

# Matches every "+2 OVR" / "-3 DEF" style delta token on the page.
# Confirmed via a real fetch (Bruno Fernandes' actual page) that the
# sign+number and the stat code can be split across two lines --
# "+2\nOVR", not "+2OVR" -- so \s* between them is required, not
# optional; an earlier version required them glued together with zero
# whitespace, which silently matched nothing on this format and made
# every delta default to 0. Only the 6 main stats (or GK equivalents) +
# OVR are matched, so this doesn't false-positive against the finer
# per-sub-attribute deltas shown further down the same page
# (Acceleration, Sprint Speed, etc. use different formatting entirely).
DELTA_RE = re.compile(r"(?P<sign>[+-])(?P<amt>\d+)\s*(?P<stat>OVR|PAC|SHO|PAS|DRI|DEF|PHY|DIV|HAN|KIC|REF|SPD|POS)")


def fetch_social_card_url(player: PlayerRating, session: Optional[requests.Session] = None) -> Optional[str]:
    """Fetches player.detail_url and pulls out fut.gg's own fully-rendered
    social-preview card image URL (see SOCIAL_CARD_RE above). Returns
    None if the page has no such meta tag. IMPORTANT: I could not
    visually verify what this image actually looks like -- this
    environment can fetch a page's text but not render/view an image
    from a URL. The regex extraction itself is tested against a real
    saved fixture (both the URL pattern and the "FC 27 card at 91 OVR"
    alt text are copied verbatim from a live fetch of Haaland's and
    Mbappé's real pages). Whether the image itself matches the reference
    card you want needs a one-time visual check on your end -- see
    build_video.py's docstring for how to do a cheap single-player test
    before running a full batch.
    """
    if not player.detail_url:
        return None
    text = fetch_page_text(player.detail_url, session)
    m = SOCIAL_CARD_RE.search(text)
    return m.group("url") if m else None


def fetch_comparison_card(new_player: PlayerRating, session: Optional[requests.Session] = None) -> Optional[dict]:
    """Fetches new_player's own detail page (new_player.detail_url, already
    known from the ratings cache) and pulls out their PRIOR-year card image
    URL, their OWN current-year social card image (see
    fetch_social_card_url -- extracted from the SAME page fetch, no extra
    request), plus the exact stat deltas fut.gg already computed. Returns
    None if the page has no prior-year card block at all (e.g. a player
    who's brand new to Ultimate Team this year, with nothing to compare
    against) -- callers should skip those players for a comparison video,
    not treat it as an error.

    Tested against a real saved fixture (sample_futgg_detail_fixture.txt,
    fetched from Haaland's actual FC27 page) -- see
    test_fetch_comparison_card.py.
    """
    if not new_player.detail_url:
        return None
    text = fetch_page_text(new_player.detail_url, session)
    return parse_comparison_card(text, new_player.ovr, new_player.stats)


def parse_comparison_card(text: str, new_ovr: int, new_stats: Optional[dict] = None) -> Optional[dict]:
    img_m = OLD_CARD_IMG_RE.search(text)
    if not img_m:
        return None

    social_m = SOCIAL_CARD_RE.search(text)

    # Scope the delta search to the text right after the old-card image,
    # NOT the whole page. Real bug, found from an actual comparison
    # video: every delta showed as "0"/flat regardless of the player's
    # true change (e.g. Bruno Fernandes 87->89 showed 0, Mbeumo 85->84
    # showed 0) -- because DELTA_RE was searching the ENTIRE page, and
    # something else on it (most likely a "vote for the next change"
    # widget using this exact same "+1OVR"-style text, reset to 0 once a
    # rating is officially confirmed) was overwriting the real,
    # historically-correct delta -- finditer() + a plain dict assignment
    # keeps whichever match comes LAST, not the first/correct one.
    #
    # Multiple known boundary markers (not just one): the real delta
    # block sits ~1000 characters after the old-card image (there's a
    # full stat block, nation/league/club icons, foot/skill-move badges
    # in between first) -- confirmed by measuring a real fixture -- so a
    # single small fixed fallback risks ending BEFORE the real delta
    # even starts, while a single large one risks running past it into
    # spurious later content (confirmed: in one real case, only ~80
    # characters separated the end of the real delta block from the
    # start of a spurious one). Trying several confirmed real markers,
    # earliest-first, is far more robust than either fixed number alone.
    window_start = img_m.end()
    next_section_markers = ["Tier vote", "PlayStyle+ Vote", "Player Facts"]
    candidate_positions = [p for p in
                            (text.find(marker, window_start) for marker in next_section_markers)
                            if p != -1]
    window_end = min(candidate_positions) if candidate_positions else window_start + 1000
    delta_window = text[window_start:window_end]

    deltas = {}
    for m in DELTA_RE.finditer(delta_window):
        stat = m.group("stat")
        if stat in deltas:
            continue  # keep only the FIRST match within the window, extra safety
        sign = 1 if m.group("sign") == "+" else -1
        deltas[stat] = sign * int(m.group("amt"))

    ovr_delta = deltas.get("OVR", 0)
    old_stats = {}
    if new_stats:
        # A stat with no delta line shown means it didn't change --
        # confirmed against Haaland's real page (DRI had no delta line
        # and was in fact unchanged between FC26 and FC27).
        old_stats = {k: v - deltas.get(k, 0) for k, v in new_stats.items()}

    return {
        "old_card_image_url": img_m.group("url"),
        "old_year": img_m.group("year"),
        "old_ovr": new_ovr - ovr_delta,
        "ovr_delta": ovr_delta,
        "old_stats": old_stats,
        "deltas": deltas,
        "new_social_card_url": social_m.group("url") if social_m else None,
    }


if __name__ == "__main__":
    # Quick manual smoke test once you have network access:
    #   python fetch_ratings.py
    top = fetch_top_ratings(limit=10)
    for p in top:
        print(p.rank, p.name, p.club, p.position, p.ovr, p.stats, p.card_image_url)
