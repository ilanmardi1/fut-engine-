"""
player_history.py
------------------
Fetches a player's full FIFA/EA FC version history from fut.gg's
overview page (e.g. https://www.fut.gg/players/231443-ousmane-dembele/)
and picks out just the plain BASE card for each year -- never
TOTS/Icons/promos/TOTY/etc. Built and tested against a real, saved
copy of that page (sample_futgg_history_fixture.txt), not synthetic
data.

CONFIRMED from a real fetch:
- The overview page is server-rendered (same reliable pattern as every
  other fut.gg page this project already depends on) and its "FIFA
  History" section lists EVERY card ever released for that player,
  grouped by year, each with an explicit rarity label.
- The rarity labels to match are "Rare" and "Common" -- confirmed
  there are confusable rarity names that CONTAIN the word "rare" but
  are NOT the base card (e.g. "Champions League Rare" in FIFA 21), so
  matching must be an EXACT match (case-insensitive), never a
  substring check.
- Both labels are needed because a base item is only "Rare" once the
  player is rated high enough; below that fut.gg labels it "Common",
  which covers every bronze and silver AND low-rated golds. Confirmed
  live: Pedri's FIFA 20/21 (72 OVR silvers), De Bruyne's FIFA 10 (64
  bronze) and FIFA 13/14 (78/80 golds), Raphinha's FIFA 18 (76 gold)
  are all "Common", and a Rare-only match silently dropped every one
  of them -- starting Pedri's career at FIFA 22 instead of FIFA 20.
- Recent years (confirmed FIFA 22 through FC 27) link each card to its
  own detail page, using the exact same URL pattern
  (.../{year}-{ea_id}/) our existing CardScreenshotter already knows
  how to use -- these get real screenshots.
- Older years (confirmed FIFA 17-21, at least for this player -- this
  will vary by player) are NOT individually linked on the current
  site at all -- no detail page exists to screenshot. But the same
  page directly gives real face image URL, OVR, position, and all 6
  stats for these -- enough to render an accurate fallback card via
  the existing hand-drawn shield renderer, just not fut.gg's own
  visual design for those specific years.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# fut.gg dropped the "### " markdown-heading prefix on these year
# headers (confirmed live 2026-09: the line is now just
# "<Name> in EA FC 27"), so the prefix is optional here.
YEAR_HEADER_RE = re.compile(r"^(?:###\s+)?.+?\s+in\s+(EA FC (\d+)|FIFA (\d+))\s*$")
# Card links in the FIFA History section are now site-relative
# ("/players/231443-ousmane-dembele/27-231443/") rather than
# absolute; accept both and normalise to absolute below.
LINK_ENTRY_RE = re.compile(r"^\[(.+?)\]\(((?:https://www\.fut\.gg)?/players/[^)]+)\)$")
IMAGE_ENTRY_RE = re.compile(
    r"^!\[.*?\]\((https://game-assets\.fut\.gg/cdn-cgi/image/[^)]*?/historical-player-face/[^)]+)\)$"
)
# Outfield AND goalkeeper stat codes. Keeping this outfield-only meant a
# keeper's cards parsed with an EMPTY stats dict, so every pre-FC24 year
# fell through to a hand-drawn card labelled "NO stats available" --
# confirmed against Donnarumma, whose FIFA 17-23 slides all came out that
# way. GK cards use DIV/HAN/KIC/REF/SPD/POS in place of PAC/SHO/PAS/DRI/
# DEF/PHY.
OUTFIELD_STAT_NAMES = {"PAC", "SHO", "PAS", "DRI", "DEF", "PHY"}
GK_STAT_NAMES = {"DIV", "HAN", "KIC", "REF", "SPD", "POS"}
STAT_NAMES = OUTFIELD_STAT_NAMES | GK_STAT_NAMES
COMBINED_STAT_RE = re.compile(
    r"(\d{1,3})\s*(PAC|SHO|PAS|DRI|DEF|PHY|DIV|HAN|KIC|REF|SPD|POS)", re.IGNORECASE)


@dataclass
class YearCard:
    year_label: str          # "FIFA 17", "EA FC 27", etc. -- exactly as shown on the page
    year_sort_key: int       # 17, 18, ..., 24, 25, 26, 27 -- for chronological ordering
    detail_url: Optional[str] = None       # present for years with their own detail page
    face_image_url: Optional[str] = None   # present for years without one (the fallback path)
    ovr: Optional[int] = None
    position: Optional[str] = None
    stats: dict = field(default_factory=dict)
    # Set by the builder to whatever card source ACTUALLY produced
    # this slide, so the contents list can report the truth rather
    # than re-deriving an intention from has_real_screenshot_source.
    source_used: Optional[str] = None

    @property
    def has_real_screenshot_source(self) -> bool:
        return self.detail_url is not None


def _year_label_to_sort_key(label: str) -> int:
    """'FIFA 17' -> 17, 'EA FC 27' -> 27 -- both count up the same way
    since EA FC picked up numbering right where FIFA left off (FIFA 23
    was the last FIFA-branded release, EA FC 24 the next), so plain
    numeric ordering already gives correct chronological order."""
    m = re.search(r"(\d+)", label)
    return int(m.group(1)) if m else 0


def _parse_entry_meta(entry_lines: list) -> dict:
    """From the lines between one entry's start marker and its rarity
    label, extracts ovr/position/stats. Handles BOTH real formats seen
    on the actual page: stat value and label combined on one line
    ('99PAC' or '99 PAC'), or split across two separate lines ('99'
    then 'PAC') -- confirmed both exist depending on the year."""
    ovr = None
    position = None
    stats = {}
    pending_value = None

    for line in entry_lines:
        if line.startswith("!["):
            continue

        combined_m = COMBINED_STAT_RE.fullmatch(line)
        if combined_m:
            stats[combined_m.group(2).upper()] = int(combined_m.group(1))
            pending_value = None
            continue

        if line.upper() in STAT_NAMES:
            if pending_value is not None:
                stats[line.upper()] = pending_value
                pending_value = None
            continue

        if re.fullmatch(r"\d{1,3}", line):
            if ovr is None:
                ovr = int(line)
            else:
                pending_value = int(line)
            continue

        if position is None and ovr is not None and re.fullmatch(r"[A-Z]{2,3}", line):
            position = line
            continue

        # Otherwise: the player's own name repeated, or something else
        # irrelevant to stats -- ignore.

    return {"ovr": ovr, "position": position, "stats": stats}


INLINE_FACE_RE = re.compile(
    r"!\[[^\]]*\]\((https://game-assets\.fut\.gg/[^)]*?/(?:historical-player-face|players)/[^)]+)\)"
)
INLINE_OVR_POS_RE = re.compile(r"(?<![\d])(\d{2,3})\s*([A-Z]{2,3})(?![A-Za-z])")


def _parse_link_entry_meta(label: str) -> dict:
    """Linked years put the whole card on ONE line, e.g.
        [![Dembele](.../2022/players/231443.png)83RW![Nation](..)![Club](..)Dembele93PAC86DRI77SHO...](url)
    so face/OVR/position/stats are all recoverable without another
    request. The old parser kept only the URL and threw this away, which
    left a linked year with NO usable fallback when its card image and
    screenshot both failed -- producing a blank 0-OVR card. Confirmed
    live (2026-09) against FIFA 22 and 23, the two years that have a
    detail page but no standalone card asset."""
    face_m = INLINE_FACE_RE.search(label)
    face_url = face_m.group(1) if face_m else None

    # Drop every ![alt](url) so image URLs can't be mined for digits.
    stripped = re.sub(r"!\[[^\]]*\]\([^)]*\)", "|", label)

    stats = {}
    for value, name in COMBINED_STAT_RE.findall(stripped):
        stats.setdefault(name.upper(), int(value))

    # OVR/position is the first "<number><POS>" pair that is NOT a stat token.
    ovr = position = None
    for value, token in INLINE_OVR_POS_RE.findall(stripped):
        if token.upper() in STAT_NAMES:
            continue
        ovr, position = int(value), token.upper()
        break

    return {"face_url": face_url, "ovr": ovr, "position": position, "stats": stats}


def parse_fifa_history_page(text: str) -> list:
    """Returns one YearCard per year found, for that year's BASE card --
    the entry explicitly labeled "Rare" or "Common" (exact match,
    case-insensitive -- never a substring match, since e.g. "Champions
    League Rare" is a real, different, confusable rarity name that must
    NOT match).

    "Common" matters because a base item is only a gold card once the
    player is rated high enough; below that it is a silver or bronze,
    which fut.gg labels "Common" rather than "Rare". Confirmed from a
    real page: Pedri's FIFA 20 and FIFA 21 entries are both 72 OVR
    "Common" silvers, and a Rare-only version silently dropped both,
    starting his career at FIFA 22 instead of FIFA 20. Where one year
    somehow carries both labels, "Rare" wins -- a player has exactly one
    base item per year, so that can only mean an extra listed entry."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    BASE_RARITIES = ("rare", "common")
    year_results = {}   # year_label -> YearCard, replaced on each base-rarity match within that year
    year_rarities = {}  # year_label -> which label ("rare"/"common") produced the stored card
    current_year_label = None

    entry_kind = None   # "link" or "image" or None
    entry_url = None
    entry_label = None
    entry_face_url = None
    entry_lines: list = []

    def make_card_for_current_entry():
        if entry_kind == "link":
            meta = _parse_link_entry_meta(entry_label or "")
            return YearCard(
                year_label=current_year_label,
                year_sort_key=_year_label_to_sort_key(current_year_label),
                detail_url=entry_url,
                face_image_url=meta["face_url"],
                ovr=meta["ovr"],
                position=meta["position"],
                stats=meta["stats"],
            )
        meta = _parse_entry_meta(entry_lines)
        return YearCard(
            year_label=current_year_label,
            year_sort_key=_year_label_to_sort_key(current_year_label),
            face_image_url=entry_face_url,
            ovr=meta["ovr"],
            position=meta["position"],
            stats=meta["stats"],
        )

    def flush_if_base(rarity_text: str):
        # Deliberately does NOT stop after the first match within a year:
        # a real, confirmed edge case (FIFA 17, at least for one real
        # player) has TWO entries both labeled exactly "Rare" in the same
        # year -- keeping the LAST match, not the first, matches the
        # pattern seen in every OTHER year on the same page, where the
        # base entry is consistently the lowest-rated, final entry in
        # that year's list (every other entry is strictly a higher-rated
        # promo/upgrade above it). That "last one wins" rule stays
        # per-rarity, so a later "Common" never displaces a "Rare" the
        # same year already produced.
        rarity = rarity_text.strip().lower()
        if rarity not in BASE_RARITIES:
            return
        if entry_kind is None:
            return
        if year_rarities.get(current_year_label) == "rare" and rarity != "rare":
            return
        year_results[current_year_label] = make_card_for_current_entry()
        year_rarities[current_year_label] = rarity

    for line in lines:
        year_m = YEAR_HEADER_RE.match(line)
        if year_m:
            current_year_label = year_m.group(1)
            entry_kind, entry_url, entry_face_url, entry_lines = None, None, None, []
            entry_label = None
            continue

        if current_year_label is None:
            continue  # haven't reached the FIFA History section yet

        link_m = LINK_ENTRY_RE.match(line)
        if link_m:
            href = link_m.group(2)
            if href.startswith("/"):
                href = "https://www.fut.gg" + href
            entry_label = link_m.group(1)
            entry_kind, entry_url, entry_face_url, entry_lines = "link", href, None, []
            continue

        img_m = IMAGE_ENTRY_RE.match(line)
        if img_m:
            entry_label = None
            entry_kind, entry_url, entry_face_url, entry_lines = "image", None, img_m.group(1), []
            continue

        # A standalone rarity-label line closes out the current entry.
        # (Works for both "Rare"/"RARE" and every other rarity name --
        # only "Rare"/"Common" trigger flush_if_base's actual capture.)
        if entry_kind is not None and not line.startswith("!["):
            # Heuristic: a rarity label line is short prose with no digits
            # -- stat lines and OVR/position lines were already consumed
            # by the branches above in _parse_entry_meta's equivalent
            # checks, so by the time we get here for an "image" entry,
            # this could still be a stat/meta line OR the rarity line.
            # Distinguish by checking against the same patterns used in
            # _parse_entry_meta: if it matches none of them, and it's
            # not empty, treat it as a potential rarity label.
            if entry_kind == "image" and (
                COMBINED_STAT_RE.fullmatch(line)
                or line.upper() in STAT_NAMES
                or re.fullmatch(r"\d{1,3}", line)
                or re.fullmatch(r"[A-Z]{2,3}", line)
            ):
                entry_lines.append(line)
                continue
            # A likely rarity/label line (or the player's own name, for
            # "image" entries -- harmless to check both kinds here).
            flush_if_base(line)
            if entry_kind == "image":
                entry_lines.append(line)  # harmless if it was the name, not consumed if rarity already flushed
            continue

    return sorted(year_results.values(), key=lambda c: c.year_sort_key)


def _normalize_for_matching(s: str) -> str:
    """Strips accents/diacritics and lowercases, so 'Dembele' matches
    'Dembélé', 'Mbappe' matches 'Mbappé', etc. Real bug found and fixed:
    plain lowercase substring matching failed on exactly this -- most
    people don't bother typing accented characters, so name lookup
    needs to be accent-insensitive to be usable in practice."""
    import unicodedata
    normalized = unicodedata.normalize("NFKD", s)
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower()


def _rank_index_matches(needle: str, hits: list) -> list:
    """Orders sitemap hits so the obvious player wins. `hits` is a list of
    (position, entry) where position is the entry's index in the sitemap.

    Real ambiguity this has to survive: "dembele" matches Ousmane, Fatou
    AND Karamoko; "mbappe" matches Kylian and Ethan.

    Ties break on sitemap POSITION, which fut.gg orders by prominence --
    verified: Kylian Mbappe sits at 20 and Ethan at 3208, Ousmane Dembele
    at 44 against Fatou at 7466, Erling Haaland at 19 against Markus at
    16019. An earlier version broke ties on the shorter name and duly
    picked Ethan Mbappe for "Mbappe", which is exactly the wrong answer.
    """
    def score(item):
        position, entry = item
        name = _normalize_for_matching(entry["name"])
        if name == needle:
            tier = 0
        elif name.split() and name.split()[-1] == needle:
            tier = 1
        elif name.startswith(needle + " ") or name.endswith(" " + needle):
            tier = 2
        else:
            tier = 3
        return (tier, position)
    return [entry for _pos, entry in sorted(hits, key=score)]


def resolve_player_from_index(player_name: str, index: list) -> Optional[tuple]:
    """Name -> (overview_url, display_name) using the sitemap-derived player
    index, for when the local ratings cache cannot answer. Prints the other
    candidates when the name is ambiguous rather than silently guessing --
    the caller can then re-run with a fuller name."""
    needle = _normalize_for_matching(player_name.strip())
    if not needle:
        return None
    hits = [(i, e) for i, e in enumerate(index)
            if needle in _normalize_for_matching(e["name"])]
    if not hits:
        return None
    ranked = _rank_index_matches(needle, hits)
    best = ranked[0]
    if len(ranked) > 1:
        others = ", ".join(e["name"].title() for e in ranked[1:6])
        print(f"  ('{player_name}' also matches: {others}"
              f"{' ...' if len(ranked) > 6 else ''} -- using {best['name'].title()}; "
              f"pass a fuller name to pick another)")
    return best["overview_url"], best["name"].title()


PLAYER_DISPLAY_NAME_RE = re.compile(
    r"\[Players\]\(/players/\)\s*\n\s*/\s*\n\s*(?P<name>[^\n]+)"
)


def parse_player_display_name(text: str) -> Optional[str]:
    """The player's correctly-accented name from their overview page's
    breadcrumb. The sitemap slug is accent-stripped ("ousmane-dembele"), so
    without this a sitemap-resolved player would render as "Ousmane Dembele"
    on any hand-drawn card."""
    m = PLAYER_DISPLAY_NAME_RE.search(text)
    if not m:
        return None
    name = m.group("name").strip()
    return name or None


def resolve_player_overview_url(player_name: str, ratings_pool: list,
                                fallback_index: Optional[list] = None) -> Optional[tuple]:
    """Finds a player's fut.gg overview URL (the FIFA History page) by
    name, using the ALREADY-CACHED ratings_fc27.json -- no new network
    dependency for name lookup. Works by trimming the last path segment
    off a player's existing FC27 detail_url (e.g.
    '.../231443-ousmane-dembele/27-231443/' -> '.../231443-ousmane-dembele/'),
    since every FC27 player's detail_url already has this exact shape.
    Accent-insensitive, case-insensitive substring match on name (see
    _normalize_for_matching); if multiple players match, returns the
    first (callers wanting disambiguation should inspect ratings_pool
    themselves, same spirit as the club-name-ambiguity pattern used
    elsewhere in this project).

    Returns (overview_url, canonical_name) or None. Real bug this fixes:
    an earlier version only returned the URL, so a caller rendering a
    fallback card (for years with no real screenshot -- see
    YearCard.has_real_screenshot_source) had nothing but the RAW search
    string the person originally typed to use as the displayed name --
    confirmed from a real generated card showing "Ousmane Dembele"
    (missing the accent) when the person searched "Dembele" without
    it. Returning the player's own correctly-spelled name from the
    matched ratings_pool entry lets the caller render the real name
    every time, regardless of how the person typed their search."""
    needle = _normalize_for_matching(player_name.strip())
    for p in ratings_pool:
        if needle in _normalize_for_matching(p.name):
            url = p.detail_url.rstrip("/")
            overview_url = url.rsplit("/", 1)[0] + "/"
            return overview_url, p.name
    # Nothing in the local ratings cache. That cache is currently empty for
    # everyone whose build_ratings_db.py run failed, so fall back to the
    # sitemap index, which needs no ratings data to answer.
    if fallback_index:
        return resolve_player_from_index(player_name, fallback_index)
    return None
