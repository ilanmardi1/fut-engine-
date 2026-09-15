"""
player_history.py
------------------
Fetches a player's full FIFA/EA FC version history from fut.gg's
overview page (e.g. https://www.fut.gg/players/231443-ousmane-dembele/)
and picks out just the plain "Rare" (base gold) card for each year --
never TOTS/Icons/promos/TOTY/etc. Built and tested against a real,
saved copy of that page (sample_futgg_history_fixture.txt), not
synthetic data.

CONFIRMED from a real fetch:
- The overview page is server-rendered (same reliable pattern as every
  other fut.gg page this project already depends on) and its "FIFA
  History" section lists EVERY card ever released for that player,
  grouped by year, each with an explicit rarity label.
- The exact rarity label to match is "Rare" -- confirmed there are
  confusable rarity names that CONTAIN the word "rare" but are NOT the
  base card (e.g. "Champions League Rare" in FIFA 21), so matching
  must be an EXACT match (case-insensitive), never a substring check.
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

YEAR_HEADER_RE = re.compile(r"^###\s+.+?\s+in\s+(EA FC (\d+)|FIFA (\d+))\s*$")
LINK_ENTRY_RE = re.compile(r"^\[(.+?)\]\((https://www\.fut\.gg/players/[^)]+)\)$")
IMAGE_ENTRY_RE = re.compile(
    r"^!\[.*?\]\((https://game-assets\.fut\.gg/cdn-cgi/image/[^)]*?/historical-player-face/[^)]+)\)$"
)
STAT_NAMES = {"PAC", "SHO", "PAS", "DRI", "DEF", "PHY"}
COMBINED_STAT_RE = re.compile(r"(\d{1,3})\s*(PAC|SHO|PAS|DRI|DEF|PHY)", re.IGNORECASE)


@dataclass
class YearCard:
    year_label: str          # "FIFA 17", "EA FC 27", etc. -- exactly as shown on the page
    year_sort_key: int       # 17, 18, ..., 24, 25, 26, 27 -- for chronological ordering
    detail_url: Optional[str] = None       # present for years with their own detail page
    face_image_url: Optional[str] = None   # present for years without one (the fallback path)
    ovr: Optional[int] = None
    position: Optional[str] = None
    stats: dict = field(default_factory=dict)

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


def parse_fifa_history_page(text: str) -> list:
    """Returns one YearCard per year found, for the card explicitly
    labeled "Rare" in that year (exact match, case-insensitive -- never
    a substring match, since e.g. "Champions League Rare" is a real,
    different, confusable rarity name that must NOT match)."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    year_results = {}  # year_label -> YearCard, replaced on each "Rare" match within that year
    current_year_label = None

    entry_kind = None   # "link" or "image" or None
    entry_url = None
    entry_face_url = None
    entry_lines: list = []

    def make_card_for_current_entry():
        if entry_kind == "link":
            return YearCard(
                year_label=current_year_label,
                year_sort_key=_year_label_to_sort_key(current_year_label),
                detail_url=entry_url,
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

    def flush_if_rare(rarity_text: str):
        # Deliberately does NOT stop after the first match within a year:
        # a real, confirmed edge case (FIFA 17, at least for one real
        # player) has TWO entries both labeled exactly "Rare" in the same
        # year -- keeping the LAST match, not the first, matches the
        # pattern seen in every OTHER year on the same page, where the
        # "Rare" entry is consistently the lowest-rated, final entry in
        # that year's list (every other entry is strictly a higher-rated
        # promo/upgrade above it).
        if rarity_text.strip().lower() != "rare":
            return
        if entry_kind is None:
            return
        year_results[current_year_label] = make_card_for_current_entry()

    for line in lines:
        year_m = YEAR_HEADER_RE.match(line)
        if year_m:
            current_year_label = year_m.group(1)
            entry_kind, entry_url, entry_face_url, entry_lines = None, None, None, []
            continue

        if current_year_label is None:
            continue  # haven't reached the FIFA History section yet

        link_m = LINK_ENTRY_RE.match(line)
        if link_m:
            entry_kind, entry_url, entry_face_url, entry_lines = "link", link_m.group(2), None, []
            continue

        img_m = IMAGE_ENTRY_RE.match(line)
        if img_m:
            entry_kind, entry_url, entry_face_url, entry_lines = "image", None, img_m.group(1), []
            continue

        # A standalone rarity-label line closes out the current entry.
        # (Works for both "Rare"/"RARE" and every other rarity name --
        # only "Rare" itself triggers flush_if_rare's actual capture.)
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
            flush_if_rare(line)
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


def resolve_player_overview_url(player_name: str, ratings_pool: list) -> Optional[str]:
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
    elsewhere in this project)."""
    needle = _normalize_for_matching(player_name.strip())
    for p in ratings_pool:
        if needle in _normalize_for_matching(p.name):
            url = p.detail_url.rstrip("/")
            overview_url = url.rsplit("/", 1)[0] + "/"
            return overview_url
    return None
