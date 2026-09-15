"""
build_video.py
---------------
The single entry point: give it a scenario + a few parameters, it fetches
ratings data, downloads card art, builds one composited slide per player,
and hands the slide folder off to CapCut.

Two scenarios are wired up:

  topn         "Top N <attribute> players" videos, e.g. Top 20 Pace FC27.
               Data + card art both come from fut.gg (fetch_ratings.py).
               This path is the most solid one -- ranking/filtering is
               plain Python once you have the stat table.

  comparison   "FC26 vs FC27" videos. Uses fut.gg's own upgrades/
               downgrades lists (they've already computed the diff for
               you). NOTE: fetch_ratings.fetch_upgrades_or_downgrades()
               currently reuses the same single-OVR parser as the topn
               scenario -- the upgrades/downgrades page almost certainly
               shows BOTH the old and new OVR (e.g. "82 -> 85"), which
               the current STAT_BLOCK_RE regex doesn't capture yet. That
               regex needs a small extension once you can see the real
               page output (run fetch_ratings.fetch_page_text() against
               the downgrades URL and print it -- the module docstring
               in fetch_ratings.py explains why we can't do that from
               this sandbox). Until then, run_comparison() below accepts
               already-paired (old, new) PlayerRating tuples directly, so
               you can wire in a different data source per-club if you'd
               rather match your current per-club video format.

Both scenarios funnel into the same two steps at the end:
  1. compositor.py builds one MM-SS.jpg slide per player
  2. capcut_build.py adds those slides (+ optional music) as new tracks
     on an existing CapCut draft -- exactly like your original script.

USAGE:
    First, one time (takes a few minutes -- crawls fut.gg's entire ratings
    list so "top N by stat" is correct, not just OVR-biased):

        python build_ratings_db.py

    Then, for any topn video (fast -- reads the local cache, no network
    except for the ~20 card-art downloads):

    python build_video.py topn \\
        --stat PAC --count 20 --game-year 27 \\
        --interval-seconds 6 --last-image-seconds 4 \\
        --drafts-dir "C:\\Users\\ilanm\\AppData\\Local\\CapCut\\User Data\\Projects\\com.lveditor.draft" \\
        --draft-name "Top 20 Pace FC27" \\
        --music "C:\\path\\to\\track.mp3"
"""
from __future__ import annotations

import sys

# Windows' default console codepage (often cp1252) can't represent many
# real player names -- confirmed by a real crash on "Pavlović"/similar
# (any name with a Central/Eastern European character like ć, č, š, ž;
# Western European ones like é are usually fine, cp1252 covers those, so
# this doesn't show up on every non-English name, just some of them).
# Reconfiguring stdout/stderr to UTF-8 up front means every print() in
# this program is safe regardless of which player's name it contains,
# rather than chasing down individual print calls one crash at a time.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Optional

import requests

from fetch_ratings import (
    PlayerRating, load_ratings_db, higher_res_card_url,
    fetch_comparison_card, fetch_social_card_url, club_entity_id, league_entity_id,
)
from assets import download_image
from compositor import (
    SlideStyle, build_topn_slide, build_comparison_slide,
    build_topn_slide_from_photo, build_comparison_slide_from_photos,
    build_price_slide_from_photo, build_price_slide,
    build_evolution_slide_from_photo, build_evolution_slide,
)
from screenshot_card import fc_year_url  # pure regex helper, no playwright needed just to import this
from easysbc_fetch import (
    EasySBCPlayer, fetch_all_easysbc_players, apply_easysbc_filters,
    sort_easysbc_players, parse_stat_filters, format_price,
)
from player_history import resolve_player_overview_url, parse_fifa_history_page


def _mmss(total_seconds: float) -> str:
    m = int(total_seconds // 60)
    s = int(round(total_seconds % 60))
    return f"{m:02d}-{s:02d}"


def _slide_ext(transparent: bool) -> str:
    """Transparent slides must be PNG (JPG has no alpha channel);
    non-transparent slides stay JPG as before (smaller files, and
    capcut_build.py's filename regex already accepts both)."""
    return ".png" if transparent else ".jpg"


def _cached_bg_removed(source_path: Optional[str], cache_dir: str, prefix: str) -> Optional[str]:
    """Same caching idea as _cached_download/_cached_screenshot, but for
    bg_remove.py -- background removal is slow (ML inference), so a
    second video reusing the same player shouldn't redo it. Returns None
    (not an error) if source_path is None, or if rembg isn't installed /
    fails for this image -- callers should treat that as 'couldn't
    produce a transparent version of this card', not crash the batch."""
    if not source_path:
        return None
    base = os.path.splitext(os.path.basename(source_path))[0]
    out_path = os.path.join(cache_dir, f"{prefix}_{base}_nobg.png")
    if os.path.exists(out_path):
        return out_path
    try:
        from bg_remove import remove_background_file
        remove_background_file(source_path, out_path)
        return out_path
    except Exception as e:
        print(f"  (background removal failed for {os.path.basename(source_path)}: "
              f"{type(e).__name__}: {e} -- using the version with its background still on)")
        return None


def _cached_download(url: Optional[str], cache_dir: str, prefix: str, download_fn=download_image) -> Optional[str]:
    """Downloads a small asset (face cutout, nation flag, club crest) to a
    stable local filename derived from its URL, so repeat runs (or a
    second video reusing the same players) don't re-download it. Returns
    None if there's no URL to fetch (e.g. some 'Free agent' players have
    no club icon) -- callers should treat that as 'skip this icon', not
    an error."""
    if not url:
        return None
    ext = os.path.splitext(url)[1] or ".png"
    fname = f"{prefix}_{hashlib.md5(url.encode()).hexdigest()[:12]}{ext}"
    path = os.path.join(cache_dir, fname)
    if not os.path.exists(path):
        download_fn(url, path)
    return path


def _cached_screenshot(shooter, detail_url: Optional[str], ea_id: str, game_year: str,
                        cache_dir: str, debug_dir: Optional[str] = None) -> Optional[str]:
    """Same caching idea as _cached_download, but for CardScreenshotter --
    a real browser screenshot of one player's card, confirmed to produce
    an exact match to fut.gg's own rendering (verified by hand against
    Mbappe's FC27 card). Returns None (not an error) if there's no
    detail_url to screenshot, or if the crop heuristic couldn't find the
    card on that page -- callers should fall back to another card source."""
    if not shooter or not detail_url:
        return None
    path = os.path.join(cache_dir, f"screenshot_{ea_id}_{game_year}.png")
    if os.path.exists(path):
        return path
    ok = shooter.screenshot_card(detail_url, ea_id, game_year, path, debug_dir=debug_dir)
    return path if ok else None


def _open_screenshotter(use_screenshots: bool):
    """Returns (shooter_or_None, cleanup_fn). Screenshots are the
    confirmed-best card source (a real browser render of fut.gg's actual
    page), but require playwright to be installed AND working -- if
    either isn't true, this degrades gracefully to None so callers fall
    back to the social-card URL or the hand-drawn shield instead of
    crashing. Catches broadly (not just ImportError) on purpose: e.g. in
    the sandbox this was developed in, playwright imports and Chromium
    launches fine, but the environment's network policy blocks fut.gg
    entirely -- a different failure than "not installed", and one a
    narrower except clause would miss."""
    if not use_screenshots:
        return None, lambda: None
    try:
        from screenshot_card import CardScreenshotter
        ctx = CardScreenshotter()
        shooter = ctx.__enter__()
        return shooter, lambda: ctx.__exit__(None, None, None)
    except ImportError:
        print("  (playwright not installed -- run `pip install playwright && playwright install chromium`")
        print("   for real card screenshots; falling back to other card sources for now)")
        return None, lambda: None
    except Exception as e:
        print(f"  (couldn't start the screenshot browser ({type(e).__name__}: {e}) -- "
              f"falling back to other card sources)")
        return None, lambda: None


# ---------------------------------------------------------------------------
# Scenario: Top N by attribute
# ---------------------------------------------------------------------------

def run_topn(
    players: list[PlayerRating],
    stat: str,
    slides_dir: str,
    card_cache_dir: str,
    interval_seconds: float,
    style: SlideStyle,
    download_fn=download_image,
    use_screenshots: bool = True,
    transparent: bool = False,
) -> None:
    """players should already be sorted best-first for `stat` (see
    rank_by_stat below) and truncated to the count you want.

    Card source priority, per player:
      1. Real browser screenshot of fut.gg's own card (CardScreenshotter)
         -- CONFIRMED to produce an exact match by hand (Mbappe FC27).
         Needs playwright installed; set use_screenshots=False to skip.
      2. fut.gg's social-preview card image (fetch_social_card_url) --
         cheap, no browser needed, but not confirmed to visually match.
      3. Our own hand-drawn shield (render_fut_card) -- always available,
         guaranteed consistent, but an approximation of the real design.

    transparent=True: slides get saved as PNG with the background
    stripped, for laying your own background image/video underneath in
    CapCut. For sources 1/2 (a real photo), this runs it through
    bg_remove.py (rembg) -- an extra, slow-ish step; for source 3 (our
    own drawn shield), no ML is needed since we already know exactly
    what's card vs. background.
    """
    import requests
    os.makedirs(slides_dir, exist_ok=True)
    os.makedirs(card_cache_dir, exist_ok=True)
    session = requests.Session()
    ext = _slide_ext(transparent)

    shooter, cleanup = _open_screenshotter(use_screenshots)
    try:
        for i, p in enumerate(players):
            t = i * interval_seconds
            out_path = os.path.join(slides_dir, f"{_mmss(t)}{ext}")

            real_card_path = _cached_screenshot(
                shooter, p.detail_url, p.ea_id, p.game_year or "27", card_cache_dir,
                debug_dir=os.path.join(card_cache_dir, "screenshot_debug"),
            )

            if not real_card_path:
                social_url = fetch_social_card_url(p, session=session)
                if social_url:
                    real_card_path = _cached_download(social_url, card_cache_dir, "realcard", download_fn)

            if real_card_path:
                if transparent:
                    nobg_path = _cached_bg_removed(real_card_path, card_cache_dir, "topn")
                    if nobg_path:
                        build_topn_slide_from_photo(out_path=out_path, rank=i + 1, real_card_path=nobg_path,
                                                     style=style, transparent=True)
                        continue
                    # bg removal failed -- fall through to the drawn-shield
                    # fallback below rather than silently keeping the
                    # background on (which would break transparent=True's
                    # whole point without any indication why).
                else:
                    build_topn_slide_from_photo(out_path=out_path, rank=i + 1, real_card_path=real_card_path, style=style)
                    continue

            face_path = _cached_download(higher_res_card_url(p.card_image_url), card_cache_dir, "face", download_fn)
            nation_path = _cached_download(p.nation_icon_url, card_cache_dir, "nation", download_fn)
            club_path = _cached_download(p.club_icon_url, card_cache_dir, "club", download_fn)
            build_topn_slide(
                out_path=out_path,
                rank=i + 1,
                player_name=p.name,
                position=p.position,
                ovr=p.ovr,
                stats=p.stats,
                face_path=face_path,
                style=style,
                nation_icon_path=nation_path,
                club_icon_path=club_path,
                highlight_stat=stat,
                transparent=transparent,
            )
    finally:
        cleanup()
    print(f"Built {len(players)} topn slides in {slides_dir}")


def rank_by_stat(players: list[PlayerRating], stat: str, count: int, descending: bool = True) -> list[PlayerRating]:
    stat = stat.upper()
    ranked = [p for p in players if stat in p.stats]
    ranked.sort(key=lambda p: p.stat(stat), reverse=descending)
    return ranked[:count]


class AmbiguousClubError(Exception):
    """Raised by resolve_club_players when a club NAME matches more than
    one distinct underlying club entity -- confirmed live: fut.gg shows
    the identical display name "FC Barcelona" for both the men's squad
    (club id 241) and the women's squad (club id 116325), with no
    distinguishing text anywhere in the player listing. Its own "Gender"
    filter/counter can't be trusted to disambiguate either -- it showed
    "Women (0)" on that exact page while visibly listing Bonmatí. Callers
    should catch this and let the PERSON pick, since they can trivially
    recognize player names even though nothing in the data reliably
    labels them."""
    def __init__(self, groups: dict):
        self.groups = groups
        super().__init__(f"{len(groups)} distinct clubs share this name")


def resolve_club_players(
    pool: list[PlayerRating],
    club_name: Optional[str] = None,
    club_ids: Optional[str] = None,
) -> list[PlayerRating]:
    """Resolves which players belong to "the club" the caller means.

    Pass club_ids (comma-separated numeric club entity IDs, e.g.
    "241" or "116325,241" to combine two squads) for an exact,
    unambiguous selection -- this is what you get FROM the
    AmbiguousClubError message below, so the normal flow is: try by
    name first, and if it's ambiguous, re-run with the id(s) it prints.

    Pass club_name for the common case (most clubs aren't ambiguous).
    Raises AmbiguousClubError if the name matches more than one distinct
    club entity -- see that class's docstring for why this can't just
    silently guess."""
    if club_ids:
        wanted = {x.strip() for x in club_ids.split(",") if x.strip()}
        matches = [p for p in pool if club_entity_id(p.club_icon_url) in wanted]
        if not matches:
            raise SystemExit(f"No players found for club-id(s): {club_ids}")
        return matches

    if not club_name:
        raise ValueError("resolve_club_players needs either club_name or club_ids")

    name_matches = [p for p in pool if club_name.lower() in p.club.lower()]
    if not name_matches:
        raise SystemExit(f"No players found for club matching '{club_name}'.")

    groups: dict[str, list[PlayerRating]] = {}
    for p in name_matches:
        cid = club_entity_id(p.club_icon_url) or "unknown"
        groups.setdefault(cid, []).append(p)

    if len(groups) > 1:
        raise AmbiguousClubError(groups)

    return name_matches


def sort_and_limit_club_players(
    players: list[PlayerRating],
    sort_by: str = "ovr",
    count: Optional[int] = None,
) -> list[PlayerRating]:
    """Sorts a resolved club roster (by OVR descending, or name) and
    optionally truncates to the top N -- pulled out as its own function
    so it's testable without going through argparse."""
    sorted_players = sorted(
        players,
        key=(lambda p: p.ovr) if sort_by == "ovr" else (lambda p: p.name),
        reverse=(sort_by == "ovr"),
    )
    if count:
        sorted_players = sorted_players[:count]
    return sorted_players


def print_ambiguous_club_help(club_name: str, err: AmbiguousClubError) -> None:
    print(f"Found {len(err.groups)} different squads matching '{club_name}' -- fut.gg currently")
    print("shares this display name across them (often a men's/women's split), and its own")
    print("gender labels can't be trusted to tell them apart right now, so pick by ID instead:\n")
    ids_sorted = sorted(err.groups.items(), key=lambda kv: -len(kv[1]))
    for cid, players in ids_sorted:
        sample = ", ".join(p.name for p in players[:5])
        print(f"  --club-id {cid:<8} {len(players)} players -- e.g. {sample}")
    if len(ids_sorted) >= 2:
        combined = ",".join(cid for cid, _ in ids_sorted[:2])
        print(f"\nRe-run with one --club-id above for a single squad, or combine them")
        print(f"(e.g. --club-id {combined}) for both.")


def resolve_league_players(pool: list[PlayerRating], league_ids: str) -> list[PlayerRating]:
    """Filters to every player in one or more leagues (comma-separated
    numeric IDs, e.g. '13' for Premier League, '53,2222' to combine
    La Liga and Liga F). Unlike resolve_club_players, there's no
    name-based path or ambiguity to resolve -- we never had a readable
    league name to begin with, so ID is the only way in. Use
    summarize_leagues()/print_league_summary() (or the `list-leagues`
    CLI command) to discover which ID you want first."""
    wanted = {x.strip() for x in league_ids.split(",") if x.strip()}
    matches = [p for p in pool if league_entity_id(p.league_icon_url) in wanted]
    if not matches:
        raise SystemExit(f"No players found for league-id(s): {league_ids}")
    return matches


def summarize_leagues(pool: list[PlayerRating]) -> dict[str, list[PlayerRating]]:
    """Groups the whole ratings pool by league entity ID -- the
    self-service discovery step, since we can't show league NAMES (fut.gg
    doesn't expose one anywhere in the scraped data, only this numeric
    ID). Sample club names per group are usually enough to recognize a
    league instantly (e.g. a group full of Real Madrid/Barcelona/Atletico
    is obviously La Liga)."""
    groups: dict[str, list[PlayerRating]] = {}
    for p in pool:
        lid = league_entity_id(p.league_icon_url) or "unknown"
        groups.setdefault(lid, []).append(p)
    return groups


def print_league_summary(groups: dict[str, list[PlayerRating]]) -> None:
    print(f"Found {len(groups)} distinct leagues in the ratings database.")
    print("fut.gg doesn't expose league NAMES in this data, only this numeric ID -- the")
    print("sample clubs below are usually enough to recognize which one is which:\n")
    for lid, players in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        clubs = []
        seen = set()
        for p in players:
            if p.club and p.club not in seen:
                clubs.append(p.club)
                seen.add(p.club)
            if len(clubs) >= 5:
                break
        print(f"  --league-id {lid:<8} {len(players)} players -- e.g. clubs: {', '.join(clubs)}")
    print("\nConfirmed from real data already gathered on this project: 13 = Premier League, "
          "53 = La Liga.\nEverything else above, identify from the sample clubs.")
    print("\nUse with the comparison scenario, e.g.:")
    print("    python build_video.py comparison --league-id 13 --count 20 ...")


# ---------------------------------------------------------------------------
# Scenario: FC26 vs FC27 comparison
# ---------------------------------------------------------------------------

def run_comparison(
    pairs: list[tuple[PlayerRating, PlayerRating, Optional[str]]],  # (old, new, new_social_card_url)
    slides_dir: str,
    card_cache_dir: str,
    interval_seconds: float,
    style: SlideStyle,
    download_fn=download_image,
    use_screenshots: bool = True,
    transparent: bool = False,
) -> None:
    """Each pair is (old, new, new_social_card_url) -- see
    build_comparison_pairs_via_detail_pages for how these are built.

    Card source priority, per side (old/new), same as run_topn:
      1. Real browser screenshot (CardScreenshotter) -- old.detail_url and
         new.detail_url are both fut.gg detail pages (same template,
         different year), confirmed to screenshot correctly.
      2. new_social_card_url for the new side / old.card_image_url
         (fut.gg's real player-item-card render) for the old side.
      3. Hand-drawn shields for both, as a last resort.

    transparent=True: same idea as run_topn -- both cards get their
    backgrounds removed (rembg) before compositing onto a transparent
    canvas, or drawn directly on transparent when falling back to the
    hand-drawn shields.
    """
    os.makedirs(slides_dir, exist_ok=True)
    os.makedirs(card_cache_dir, exist_ok=True)
    ext = _slide_ext(transparent)

    shooter, cleanup = _open_screenshotter(use_screenshots)
    try:
        for i, entry in enumerate(pairs):
            old, new, new_social_url = entry if len(entry) == 3 else (*entry, None)
            t = i * interval_seconds
            out_path = os.path.join(slides_dir, f"{_mmss(t)}{ext}")
            debug_dir = os.path.join(card_cache_dir, "screenshot_debug")

            old_real = _cached_screenshot(shooter, old.detail_url, old.ea_id, old.game_year, card_cache_dir, debug_dir)
            new_real = _cached_screenshot(shooter, new.detail_url, new.ea_id, new.game_year, card_cache_dir, debug_dir)

            if not new_real and new_social_url:
                new_real = _cached_download(new_social_url, card_cache_dir, "realcard_new", download_fn)
            if not old_real:
                old_real = _cached_download(higher_res_card_url(old.card_image_url, width=512),
                                             card_cache_dir, "realcard_old", download_fn)

            if old_real and new_real:
                if transparent:
                    old_nobg = _cached_bg_removed(old_real, card_cache_dir, "cmp_old")
                    new_nobg = _cached_bg_removed(new_real, card_cache_dir, "cmp_new")
                    if old_nobg and new_nobg:
                        build_comparison_slide_from_photos(
                            out_path=out_path, player_name=new.name,
                            card_old_path=old_nobg, card_new_path=new_nobg,
                            ovr_old=old.ovr, ovr_new=new.ovr, style=style, transparent=True,
                        )
                        continue
                    # bg removal failed for one/both -- fall through to
                    # the drawn-shield fallback below.
                else:
                    build_comparison_slide_from_photos(
                        out_path=out_path, player_name=new.name,
                        card_old_path=old_real, card_new_path=new_real,
                        ovr_old=old.ovr, ovr_new=new.ovr, style=style,
                    )
                    continue

            face_old = _cached_download(higher_res_card_url(old.card_image_url), card_cache_dir, "face_old", download_fn)
            face_new = _cached_download(higher_res_card_url(new.card_image_url), card_cache_dir, "face_new", download_fn)
            nation_path = _cached_download(new.nation_icon_url, card_cache_dir, "nation", download_fn)
            club_path = _cached_download(new.club_icon_url, card_cache_dir, "club", download_fn)
            build_comparison_slide(
                out_path=out_path,
                player_name=new.name,
                position=new.position,
                stats_old=old.stats,
                stats_new=new.stats,
                face_old_path=face_old,
                face_new_path=face_new,
                ovr_old=old.ovr,
                ovr_new=new.ovr,
                style=style,
                nation_icon_path=nation_path,
                club_icon_path=club_path,
                transparent=transparent,
            )
    finally:
        cleanup()
    print(f"Built {len(pairs)} comparison slides in {slides_dir}")


UT_COIN_ICON_URL = "https://pngdownload.io/wp-content/uploads/2025/06/FIFA-Coins-FC-Ultimate-Team-Currency.webp"
# The real FUT coins icon, used as-is (not hand-drawn) on every price
# tag -- per direct request, after two rounds of a hand-drawn
# approximation not looking close enough. Downloaded once per run and
# cached (see run_price below), not re-fetched per player.


def run_price(
    easysbc_players: list,  # list[EasySBCPlayer], already filtered/sorted/limited by the caller
    ratings_pool: list[PlayerRating],  # local fut.gg cache, cross-referenced by ID for real screenshots
    slides_dir: str,
    card_cache_dir: str,
    interval_seconds: float,
    style: SlideStyle,
    download_fn=download_image,
    use_screenshots: bool = True,
    transparent: bool = False,
) -> None:
    """Price-prediction scenario (no FC26 comparison) driven by
    easysbc.io's market data. Card source priority, per player:
      1. Real browser screenshot of the player's fut.gg card -- found by
         cross-referencing easysbc's resource_id against the LOCAL
         ratings cache's ea_id. Confirmed these IDs match across both
         sites for real (Raphinha: 233419 on both).
      2. fut.gg's social-preview card, if the player is in the local
         cache but the screenshot itself failed for some reason.
      3. Hand-drawn shield using easysbc's OWN face image and OWN stats
         directly -- for a player easysbc knows about but our local
         fut.gg cache doesn't (e.g. not yet crawled), no fut.gg data
         needed at all for this path.
    """
    import requests
    os.makedirs(slides_dir, exist_ok=True)
    os.makedirs(card_cache_dir, exist_ok=True)
    ext = _slide_ext(transparent)

    fut_gg_by_id = {p.ea_id: p for p in ratings_pool}
    session = requests.Session()

    # Downloaded ONCE for the whole run, not per player -- it's the same
    # static icon on every slide. Wrapped in try/except deliberately:
    # _cached_download doesn't catch download failures itself, and this
    # URL is a random third-party site (not fut.gg's own CDN), so it's
    # less reliable than everything else this pipeline downloads -- a
    # failure here must not crash the whole batch before it even starts.
    # draw_price_tag() falls back to its own hand-drawn version whenever
    # coin_icon_path is None.
    try:
        coin_icon_path = _cached_download(UT_COIN_ICON_URL, card_cache_dir, "ut_coin_icon", download_fn)
    except Exception as e:
        print(f"  (couldn't download the coin icon ({type(e).__name__}: {e}) -- "
              f"using the hand-drawn version instead)")
        coin_icon_path = None

    shooter, cleanup = _open_screenshotter(use_screenshots)
    try:
        for i, ep in enumerate(easysbc_players):
            t = i * interval_seconds
            out_path = os.path.join(slides_dir, f"{_mmss(t)}{ext}")
            price_text = format_price(ep.price)

            fut_gg_player = fut_gg_by_id.get(ep.resource_id)
            real_card_path = None
            if fut_gg_player:
                real_card_path = _cached_screenshot(
                    shooter, fut_gg_player.detail_url, fut_gg_player.ea_id, fut_gg_player.game_year or "27",
                    card_cache_dir, debug_dir=os.path.join(card_cache_dir, "screenshot_debug"),
                )
                if not real_card_path:
                    social_url = fetch_social_card_url(fut_gg_player, session=session)
                    if social_url:
                        real_card_path = _cached_download(social_url, card_cache_dir, "realcard_price", download_fn)

            if real_card_path:
                if transparent:
                    nobg_path = _cached_bg_removed(real_card_path, card_cache_dir, "price")
                    if nobg_path:
                        build_price_slide_from_photo(out_path=out_path, rank=i + 1, real_card_path=nobg_path,
                                                      price_text=price_text, style=style, transparent=True,
                                                      coin_icon_path=coin_icon_path)
                        continue
                    # bg removal failed -- fall through to the hand-drawn fallback below.
                else:
                    build_price_slide_from_photo(out_path=out_path, rank=i + 1, real_card_path=real_card_path,
                                                  price_text=price_text, style=style, coin_icon_path=coin_icon_path)
                    continue

            # Fallback: easysbc's own face image + easysbc's own data, no
            # fut.gg lookup needed for this player at all.
            face_path = _cached_download(ep.player_url, card_cache_dir, "easysbc_face", download_fn)
            build_price_slide(
                out_path=out_path,
                rank=i + 1,
                player_name=ep.name,
                position=ep.preferred_position or (ep.positions[0] if ep.positions else "?"),
                ovr=ep.rating,
                stats=ep.attributes,
                face_path=face_path,
                price_text=price_text,
                style=style,
                transparent=transparent,
                coin_icon_path=coin_icon_path,
            )
    finally:
        cleanup()
    print(f"Built {len(easysbc_players)} price slides in {slides_dir}")


YEAR_DETAIL_URL_RE = re.compile(r"/(\d+)-(\d+)/?$")


def run_evolution(
    player_name: str,
    ratings_pool: list[PlayerRating],
    slides_dir: str,
    card_cache_dir: str,
    interval_seconds: float,
    style: SlideStyle,
    download_fn=download_image,
    use_screenshots: bool = True,
    transparent: bool = False,
) -> list:
    """Player-evolution scenario: one slide per FIFA/EA FC year the
    player has a plain "Rare" (base gold) card in, oldest to newest,
    each labeled with its year/version -- no FC26 comparison, this is
    a whole career's worth of "Rare" cards in order. See
    player_history.py's module docstring for the confirmed two-tier
    card-source design this relies on: a real screenshot for years
    with their own fut.gg detail page, a hand-drawn fallback (using
    real scraped face/stats/OVR/position, just not fut.gg's own visual
    design) for years that don't have one.

    Returns the list of YearCard objects actually built, in
    chronological order -- lets a caller (e.g. a packaging step that
    writes a player/contents list alongside the slides) know exactly
    what ended up in the video without needing to re-fetch or
    re-parse anything itself."""
    overview_url = resolve_player_overview_url(player_name, ratings_pool)
    if not overview_url:
        raise SystemExit(
            f"No player matching '{player_name}' found in your local ratings_fc27.json. "
            f"Name matching is accent-insensitive and a substring match, but the player still needs "
            f"to be a current FC27 player in your local cache -- try re-running build_ratings_db.py "
            f"if they were only recently revealed."
        )
    print(f"Found player page: {overview_url}")

    from fetch_ratings import fetch_page_text
    text = fetch_page_text(overview_url)
    year_cards = parse_fifa_history_page(text)
    if not year_cards:
        raise SystemExit(
            "Found the player's page but couldn't parse any 'Rare' cards from its FIFA History "
            "section -- fut.gg may have changed its page layout again."
        )
    print(f"Found {len(year_cards)} years: {', '.join(c.year_label for c in year_cards)}")

    os.makedirs(slides_dir, exist_ok=True)
    os.makedirs(card_cache_dir, exist_ok=True)
    ext = _slide_ext(transparent)

    shooter, cleanup = _open_screenshotter(use_screenshots)
    try:
        for i, card in enumerate(year_cards):
            t = i * interval_seconds
            out_path = os.path.join(slides_dir, f"{_mmss(t)}{ext}")

            real_card_path = None
            if card.has_real_screenshot_source:
                m = YEAR_DETAIL_URL_RE.search(card.detail_url)
                if m:
                    game_year, item_id = m.group(1), m.group(2)
                    real_card_path = _cached_screenshot(
                        shooter, card.detail_url, item_id, game_year,
                        card_cache_dir, debug_dir=os.path.join(card_cache_dir, "screenshot_debug"),
                    )
                    if not real_card_path:
                        # Screenshot disabled or failed for this linked
                        # year -- unlike the older/unlinked years, a
                        # linked year carries no scraped stats/face of
                        # its own to fall back to (the parser only
                        # captured its detail_url), so the social-preview
                        # card (same middle-tier fallback run_price
                        # already uses) is the difference between a
                        # real card image and a near-blank placeholder.
                        fake_player = PlayerRating(
                            ea_id=item_id, name=player_name, club="", position="",
                            ovr=card.ovr or 0, stats={}, detail_url=card.detail_url,
                        )
                        social_url = fetch_social_card_url(fake_player)
                        if social_url:
                            real_card_path = _cached_download(social_url, card_cache_dir,
                                                               "evolution_social", download_fn)

            if real_card_path:
                if transparent:
                    nobg_path = _cached_bg_removed(real_card_path, card_cache_dir, "evolution")
                    if nobg_path:
                        build_evolution_slide_from_photo(out_path=out_path, year_label=card.year_label,
                                                          real_card_path=nobg_path, style=style, transparent=True)
                        continue
                    # bg removal failed -- fall through to the hand-drawn fallback below.
                else:
                    build_evolution_slide_from_photo(out_path=out_path, year_label=card.year_label,
                                                      real_card_path=real_card_path, style=style)
                    continue

            # Fallback: real face/OVR/position/stats scraped directly
            # from the FIFA History page for this year -- no fut.gg
            # detail page needed for this path at all.
            face_path = None
            if card.face_image_url:
                face_path = _cached_download(card.face_image_url, card_cache_dir, "evolution_face", download_fn)
            build_evolution_slide(
                out_path=out_path,
                year_label=card.year_label,
                player_name=player_name,
                position=card.position or "?",
                ovr=card.ovr or 0,
                stats=card.stats,
                face_path=face_path,
                style=style,
                transparent=transparent,
            )
    finally:
        cleanup()
    print(f"Built {len(year_cards)} evolution slides in {slides_dir}")
    return year_cards


def match_by_ea_id(old_list: list[PlayerRating], new_list: list[PlayerRating]) -> list[tuple[PlayerRating, PlayerRating, None]]:
    old_by_id = {p.ea_id: p for p in old_list}
    pairs = []
    for new in new_list:
        old = old_by_id.get(new.ea_id)
        if old:
            pairs.append((old, new, None))
    return pairs


def build_comparison_pairs_via_detail_pages(
    players: list[PlayerRating],
    progress: bool = True,
) -> list[tuple[PlayerRating, PlayerRating, Optional[str]]]:
    """The real data source for a club-roster comparison video: for each
    player, fetches THEIR OWN fut.gg detail page (already known via
    player.detail_url from the ratings cache) -- which bakes in their
    prior-year card image, their OWN current-year social-preview card,
    and exact stat deltas, all server-side (see
    fetch_ratings.fetch_comparison_card). One request per player, no
    separate FC26 crawl needed. Players with no prior-year card (new to
    the game) are skipped. Returns (old, new, new_social_card_url)
    triples -- see run_comparison for how the third element is used.

    old.detail_url is derived from new.detail_url via fc_year_url() --
    same URL template, year prefix swapped -- confirmed against fut.gg's
    own "FC 26 Version" link. This is what lets run_comparison screenshot
    the OLD card too, not just the new one."""
    session = requests.Session()
    pairs = []
    for p in players:
        info = fetch_comparison_card(p, session=session)
        if info is None:
            if progress:
                print(f"  {p.name}: no prior-year card found, skipping")
            continue
        old_detail_url = fc_year_url(p.detail_url, p.ea_id, info["old_year"]) if p.detail_url else ""
        old = PlayerRating(
            ea_id=p.ea_id, name=p.name, club=p.club, position=p.position,
            ovr=info["old_ovr"], stats=info["old_stats"],
            card_image_url=info["old_card_image_url"],
            nation_icon_url=p.nation_icon_url, club_icon_url=p.club_icon_url,
            detail_url=old_detail_url,
            game_year=info["old_year"],
        )
        pairs.append((old, p, info.get("new_social_card_url")))
        if progress:
            sign = "+" if info["ovr_delta"] >= 0 else ""
            print(f"  {p.name}: {info['old_ovr']} -> {p.ovr} ({sign}{info['ovr_delta']})")
    return pairs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Build a FUT ratings video end to end.")
    sub = parser.add_subparsers(dest="scenario", required=True)

    common = dict(
        interval_seconds=("--interval-seconds", float, 6.0),
        last_image_seconds=("--last-image-seconds", float, 4.0),
        drafts_dir=("--drafts-dir", str, None),
        draft_name=("--draft-name", str, None),
        music=("--music", str, None),
        watermark=("--watermark", str, None),
        work_dir=("--work-dir", str, None),
    )

    p_topn = sub.add_parser("topn", help="Top N <attribute> players video")
    p_topn.add_argument("--stat", required=True, help="PAC, SHO, PAS, DRI, DEF, PHY (or OVR)")
    p_topn.add_argument("--count", type=int, default=20)
    p_topn.add_argument("--game-year", default="27")
    p_topn.add_argument("--ratings-db", default="ratings_fc27.json",
                         help="local cache built by build_ratings_db.py -- run that first")
    p_topn.add_argument("--slowest", action="store_true", help="rank ascending instead of descending")
    p_topn.add_argument("--no-screenshots", action="store_true",
                         help="skip real browser screenshots (faster, but falls back to a lower-fidelity card source)")
    p_topn.add_argument("--transparent", action="store_true",
                         help="strip the background from each card (needs rembg) and save as PNG, so you can "
                              "lay your own background image/video underneath in CapCut")
    for name, (flag, typ, default) in common.items():
        p_topn.add_argument(flag, type=typ, default=default, dest=name)

    p_cmp = sub.add_parser("comparison", help="FC26 vs FC27 comparison video for one club's roster or a whole league")
    p_cmp.add_argument("--club", default=None, help="club name (substring match), e.g. 'Arsenal'")
    p_cmp.add_argument("--club-id", default=None,
                        help="exact club entity ID(s), comma-separated to combine multiple squads "
                             "(e.g. '241' for men-only, '116325,241' for men+women) -- use this when "
                             "--club matches more than one squad sharing the same display name; the "
                             "tool will print the right IDs to use when that happens")
    p_cmp.add_argument("--league-id", default=None,
                        help="filter to an entire league instead of one club -- comma-separate to combine "
                             "leagues. Confirmed: 13 = Premier League, 53 = La Liga. For anything else, run "
                             "`python build_video.py list-leagues` first to find the right ID (fut.gg doesn't "
                             "expose league names, only this numeric ID, so that command shows sample club "
                             "names per league instead)")
    p_cmp.add_argument("--ratings-db", default="ratings_fc27.json",
                        help="local cache built by build_ratings_db.py -- run that first")
    p_cmp.add_argument("--sort", choices=["ovr", "name"], default="ovr")
    p_cmp.add_argument("--count", type=int, default=None,
                        help="limit to the top N players (by --sort order) instead of building the whole "
                             "squad/league -- omit for everything found, same as before this option existed. "
                             "Strongly recommended with --league-id -- a whole league can be 300-600+ players.")
    p_cmp.add_argument("--no-screenshots", action="store_true",
                        help="skip real browser screenshots (faster, but falls back to a lower-fidelity card source)")
    p_cmp.add_argument("--transparent", action="store_true",
                        help="strip the background from each card (needs rembg) and save as PNG, so you can "
                             "lay your own background image/video underneath in CapCut")
    for name, (flag, typ, default) in common.items():
        p_cmp.add_argument(flag, type=typ, default=default, dest=name)

    p_leagues = sub.add_parser("list-leagues", help="Discover league IDs for the comparison scenario's --league-id")
    p_leagues.add_argument("--ratings-db", default="ratings_fc27.json",
                            help="local cache built by build_ratings_db.py -- run that first")

    p_price = sub.add_parser("price", help="FC27 price-prediction video (no FC26 comparison) using easysbc.io market data")
    p_price.add_argument("--club-id", default=None,
                          help="easysbc club ID -- confirmed to be the SAME numeric ID as fut.gg's (e.g. 241 = "
                               "Barcelona men's). Use build_video.py list-leagues / your existing club-id "
                               "knowledge from the comparison scenario to find others.")
    p_price.add_argument("--league-id", default=None,
                          help="easysbc league ID -- the query parameter name for this is an UNCONFIRMED guess "
                               "(see easysbc_fetch.py's module docstring); verify before trusting a real run")
    p_price.add_argument("--max-pages", type=int, default=None,
                          help="cap how many pages to fetch from easysbc -- mainly relevant when NEITHER "
                               "--club-id nor --league-id is given (a global/unscoped video, e.g. 'top "
                               "attackers by price' with no club/league restriction). Untested at real scale, "
                               "so an unscoped fetch defaults to 20 pages for safety unless you set this "
                               "explicitly; scoped (club/league) fetches are normally small enough not to need it.")
    p_price.add_argument("--positions", default=None,
                          help="comma-separated, OR-matched against each player's possible positions, "
                               "e.g. 'ST,LW,RW'. Omit for all positions.")
    p_price.add_argument("--min-stats", default=None,
                          help="e.g. 'PAC=90,DRI=80,SHO=82' -- only players meeting ALL of these minimums")
    p_price.add_argument("--max-stats", default=None, help="e.g. 'DEF=50' -- same syntax as --min-stats")
    p_price.add_argument("--min-rating", type=int, default=None)
    p_price.add_argument("--max-rating", type=int, default=None)
    p_price.add_argument("--min-price", type=int, default=None)
    p_price.add_argument("--max-price", type=int, default=None)
    p_price.add_argument("--min-skill-moves", type=int, default=None)
    p_price.add_argument("--min-weak-foot", type=int, default=None)
    p_price.add_argument("--preferred-foot", choices=["Left", "Right"], default=None)
    p_price.add_argument("--sort", choices=["price-asc", "price-desc", "rating", "name"], default="price-asc",
                          help="price-asc = cheapest first (e.g. 'cheap beasts'), price-desc = most expensive first")
    p_price.add_argument("--count", type=int, default=20)
    p_price.add_argument("--ratings-db", default="ratings_fc27.json",
                          help="local fut.gg cache -- used to find each player's real card for screenshotting, "
                               "by cross-referencing easysbc's player ID against it (same ID scheme on both sites)")
    p_price.add_argument("--no-screenshots", action="store_true",
                          help="skip real browser screenshots (faster, falls back to easysbc's own face + our drawn shield)")
    p_price.add_argument("--transparent", action="store_true",
                          help="strip the background from each card (needs rembg) and save as PNG")
    for name, (flag, typ, default) in common.items():
        p_price.add_argument(flag, type=typ, default=default, dest=name)

    p_evo = sub.add_parser("evolution", help="One player's card history, oldest to newest -- FIFA 17 through EA FC 27, "
                                              "the plain 'Rare' (base gold) card only, never TOTS/Icons/promos")
    p_evo.add_argument("--player-name", required=True,
                        help="matched against your local fut.gg cache (ratings_fc27.json) -- accent-insensitive "
                             "and a substring match, e.g. 'Dembele' finds 'Ousmane Dembélé'")
    p_evo.add_argument("--ratings-db", default="ratings_fc27.json",
                        help="local fut.gg cache -- used both to look up the player by name and (for recent "
                             "years) to find real screenshots")
    p_evo.add_argument("--no-screenshots", action="store_true",
                        help="skip real browser screenshots -- falls back to fut.gg's social-preview card for "
                             "recent years (still a real card image, just not a full page screenshot), and the "
                             "existing hand-drawn shield + real scraped stats for years with no detail page at all")
    p_evo.add_argument("--transparent", action="store_true",
                        help="strip the background from each card (needs rembg) and save as PNG")
    for name, (flag, typ, default) in common.items():
        p_evo.add_argument(flag, type=typ, default=default, dest=name)

    args = parser.parse_args()

    if args.scenario == "list-leagues":
        if not os.path.exists(args.ratings_db):
            raise SystemExit(f"No ratings database found at '{args.ratings_db}'.\nRun this first: python build_ratings_db.py")
        pool = load_ratings_db(args.ratings_db)
        print_league_summary(summarize_leagues(pool))
        return

    work_dir = args.work_dir or tempfile.mkdtemp(prefix="fut_video_")
    slides_dir = os.path.join(work_dir, "slides")
    card_cache_dir = os.path.join(work_dir, "cards")

    style = SlideStyle(watermark_text=args.watermark)

    if args.scenario == "topn":
        if not os.path.exists(args.ratings_db):
            raise SystemExit(
                f"No ratings database found at '{args.ratings_db}'.\n"
                f"Run this first (one-time, takes a few minutes):\n"
                f"    python build_ratings_db.py\n"
                f"That crawls fut.gg's ENTIRE ratings list and caches it locally --\n"
                f"required for a correct 'top N by <stat>' ranking, since fut.gg's own\n"
                f"listing pages are always sorted by overall rating, not by individual stats."
            )
        pool = load_ratings_db(args.ratings_db)
        print(f"Loaded {len(pool)} players from {args.ratings_db}")
        players = rank_by_stat(pool, args.stat, args.count, descending=not args.slowest)
        run_topn(players, args.stat, slides_dir, card_cache_dir, args.interval_seconds, style,
                 use_screenshots=not args.no_screenshots, transparent=args.transparent)

    elif args.scenario == "comparison":
        if not args.club and not args.club_id and not args.league_id:
            raise SystemExit("Provide --club <name>, --club-id <id>, or --league-id <id>.")
        if not os.path.exists(args.ratings_db):
            raise SystemExit(
                f"No ratings database found at '{args.ratings_db}'.\n"
                f"Run this first: python build_ratings_db.py"
            )
        pool = load_ratings_db(args.ratings_db)

        if args.league_id:
            club_players = resolve_league_players(pool, args.league_id)
            label = f"league-id {args.league_id}"
        else:
            try:
                club_players = resolve_club_players(pool, club_name=args.club, club_ids=args.club_id)
            except AmbiguousClubError as e:
                print_ambiguous_club_help(args.club, e)
                raise SystemExit(1)
            label = args.club or f"club-id {args.club_id}"

        club_players = sort_and_limit_club_players(club_players, sort_by=args.sort, count=args.count)

        if args.league_id and not args.count and len(club_players) > 50:
            print(f"Note: {len(club_players)} players found for this league and no --count was set -- "
                  f"this will screenshot ALL of them (roughly {len(club_players) * 3 // 60}+ minutes at "
                  f"~2-4s each). Ctrl+C now if that's not what you meant, or use the Stop button in the GUI.")

        print(f"Found {len(club_players)} players for '{label}'. Fetching each player's prior-year card...")
        pairs = build_comparison_pairs_via_detail_pages(club_players)
        run_comparison(pairs, slides_dir, card_cache_dir, args.interval_seconds, style,
                        use_screenshots=not args.no_screenshots, transparent=args.transparent)

    elif args.scenario == "price":
        if not os.path.exists(args.ratings_db):
            raise SystemExit(
                f"No ratings database found at '{args.ratings_db}'.\n"
                f"Run this first: python build_ratings_db.py\n"
                f"(needed so real screenshots can be found by cross-referencing easysbc's player IDs against it)"
            )
        ratings_pool = load_ratings_db(args.ratings_db)

        easysbc_players = fetch_all_easysbc_players(club_id=args.club_id, league_id=args.league_id,
                                                      max_pages=args.max_pages)
        if not easysbc_players:
            raise SystemExit("easysbc.io returned no players for that club/league ID.")

        positions = [p.strip() for p in args.positions.split(",")] if args.positions else None
        min_stats = parse_stat_filters(args.min_stats)
        max_stats = parse_stat_filters(args.max_stats)

        filtered = apply_easysbc_filters(
            easysbc_players,
            positions=positions,
            min_stats=min_stats,
            max_stats=max_stats,
            min_rating=args.min_rating,
            max_rating=args.max_rating,
            min_price=args.min_price,
            max_price=args.max_price,
            min_skill_moves=args.min_skill_moves,
            min_weak_foot=args.min_weak_foot,
            preferred_foot=args.preferred_foot,
        )
        if not filtered:
            raise SystemExit("No players matched those filters -- try loosening the stat minimums or position list.")

        sorted_players = sort_easysbc_players(filtered, sort_by=args.sort)
        if args.count:
            sorted_players = sorted_players[:args.count]

        print(f"{len(easysbc_players)} players fetched, {len(filtered)} matched filters, "
              f"showing {len(sorted_players)}.")
        for p in sorted_players:
            print(f"  {p.name} ({p.rating} OVR): {format_price(p.price)}")
        run_price(sorted_players, ratings_pool, slides_dir, card_cache_dir, args.interval_seconds, style,
                  use_screenshots=not args.no_screenshots, transparent=args.transparent)

    elif args.scenario == "evolution":
        if not os.path.exists(args.ratings_db):
            raise SystemExit(
                f"No ratings database found at '{args.ratings_db}'.\n"
                f"Run this first: python build_ratings_db.py\n"
                f"(needed to look up the player by name and to find real screenshots for recent years)"
            )
        ratings_pool = load_ratings_db(args.ratings_db)
        run_evolution(args.player_name, ratings_pool, slides_dir, card_cache_dir, args.interval_seconds, style,
                      use_screenshots=not args.no_screenshots, transparent=args.transparent)

    else:
        raise SystemExit(f"Unknown scenario: {args.scenario}")

    if args.drafts_dir and args.draft_name:
        import capcut_build  # lazy: only needed for this CapCut-writing step, so
        # callers that just want the run_* functions (e.g. the CapCut-free skill
        # variant of this tool) don't need pycapcut installed at all
        capcut_build.build_draft(
            capcut_drafts_dir=args.drafts_dir,
            draft_name=args.draft_name,
            images_folder=slides_dir,
            audio_path=args.music,
            last_image_seconds=args.last_image_seconds,
        )
    else:
        print(f"\nSlides ready in: {slides_dir}")
        print("(pass --drafts-dir and --draft-name to also build the CapCut draft)")


if __name__ == "__main__":
    main()
