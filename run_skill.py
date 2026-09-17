#!/usr/bin/env python3
"""
run_skill.py -- FUT Video Engine skill entry point (CapCut-free).

Runs the EXACT SAME, already-tested scenario logic as the local FUT
Video Engine tool (build_video.py) -- topn / comparison / price /
evolution -- reusing its functions directly, not reimplementing them.
Skips the CapCut draft-building step entirely (this skill has no
CapCut integration -- the person edits the slides themselves) and
instead:
  1. Always strips card backgrounds (transparent=True) -- this is the
     whole point of this mode, not an optional flag here.
  2. Writes a players.txt describing exactly what's in the video, in
     the same order as the slides.
  3. Zips the slides + players.txt into one deliverable.

USAGE (mirrors build_video.py's CLI, minus --drafts-dir/--draft-name,
minus --transparent since it's always on):
    python run_skill.py topn --stat PAC --count 20
    python run_skill.py comparison --club "Arsenal"
    python run_skill.py price --league-id 13 --min-stats "PAC=90" --max-price 10000
    python run_skill.py evolution --player-name "Dembele"

All scenarios also need a local ratings_fc27.json (built once via
`python build_ratings_db.py`, a few minutes, then reusable across runs
-- re-run it periodically since FC27 ratings get revealed in batches).
"""
import argparse
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compositor import SlideStyle
from build_video import (
    load_ratings_db, rank_by_stat, run_topn,
    resolve_league_players, resolve_club_players, AmbiguousClubError, print_ambiguous_club_help,
    sort_and_limit_club_players, build_comparison_pairs_via_detail_pages, run_comparison,
    fetch_all_easysbc_players, apply_easysbc_filters, sort_easysbc_players, parse_stat_filters, format_price,
    run_price, run_evolution,
)
from fetch_ratings import fetch_leaderboard


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def zip_output(slides_dir: str, players_txt_path: str, out_zip_path: str) -> None:
    with zipfile.ZipFile(out_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(slides_dir)):
            fpath = os.path.join(slides_dir, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, os.path.join("slides", fname))
        zf.write(players_txt_path, "players.txt")
    print(f"\nPackaged: {out_zip_path} ({human_size(os.path.getsize(out_zip_path))})")


def _slugify(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s).strip("_") or "video"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FUT Video Engine skill -- CapCut-free, zip output")
    sub = parser.add_subparsers(dest="scenario", required=True)

    common = dict(
        interval_seconds=("--interval-seconds", float, 6.0),
        watermark=("--watermark", str, None),
        work_dir=("--work-dir", str, None),
        out_zip=("--out-zip", str, None),
    )

    p_topn = sub.add_parser("topn", help="Top N players ranked by a single stat")
    p_topn.add_argument("--stat", required=True)
    p_topn.add_argument("--count", type=int, default=20)
    p_topn.add_argument("--slowest", action="store_true")
    p_topn.add_argument("--ratings-db", default="ratings_fc27.json")
    p_topn.add_argument("--no-screenshots", action="store_true")
    for name, (flag, typ, default) in common.items():
        p_topn.add_argument(flag, type=typ, default=default, dest=name)

    p_cmp = sub.add_parser("comparison", help="FC26 vs FC27 comparison for a club or league")
    p_cmp.add_argument("--club", default=None)
    p_cmp.add_argument("--club-id", default=None)
    p_cmp.add_argument("--league-id", default=None)
    p_cmp.add_argument("--sort", choices=["ovr", "name"], default="ovr")
    p_cmp.add_argument("--count", type=int, default=None)
    p_cmp.add_argument("--ratings-db", default="ratings_fc27.json")
    p_cmp.add_argument("--no-screenshots", action="store_true")
    for name, (flag, typ, default) in common.items():
        p_cmp.add_argument(flag, type=typ, default=default, dest=name)

    p_price = sub.add_parser("price", help="Price-prediction video using easysbc.io market data")
    p_price.add_argument("--club-id", default=None)
    p_price.add_argument("--league-id", default=None)
    p_price.add_argument("--max-pages", type=int, default=None)
    p_price.add_argument("--positions", default=None)
    p_price.add_argument("--min-stats", default=None)
    p_price.add_argument("--max-stats", default=None)
    p_price.add_argument("--min-rating", type=int, default=None)
    p_price.add_argument("--max-rating", type=int, default=None)
    p_price.add_argument("--min-price", type=int, default=None)
    p_price.add_argument("--max-price", type=int, default=None)
    p_price.add_argument("--min-skill-moves", type=int, default=None)
    p_price.add_argument("--min-weak-foot", type=int, default=None)
    p_price.add_argument("--preferred-foot", choices=["Left", "Right"], default=None)
    p_price.add_argument("--sort", choices=["price-asc", "price-desc", "rating", "name"], default="price-asc")
    p_price.add_argument("--count", type=int, default=20)
    p_price.add_argument("--ratings-db", default="ratings_fc27.json")
    p_price.add_argument("--no-screenshots", action="store_true")
    for name, (flag, typ, default) in common.items():
        p_price.add_argument(flag, type=typ, default=default, dest=name)

    p_evo = sub.add_parser("evolution", help="One player's card history, oldest to newest")
    p_evo.add_argument("--player-name", required=True)
    p_evo.add_argument("--ratings-db", default="ratings_fc27.json")
    p_evo.add_argument("--no-screenshots", action="store_true")
    for name, (flag, typ, default) in common.items():
        p_evo.add_argument(flag, type=typ, default=default, dest=name)

    return parser


def main():
    args = build_arg_parser().parse_args()

    work_dir = args.work_dir or tempfile.mkdtemp(prefix="fut_video_skill_")
    slides_dir = os.path.join(work_dir, "slides")
    card_cache_dir = os.path.join(work_dir, "cards")
    style = SlideStyle(watermark_text=args.watermark)

    players_lines = []
    contents_label = args.scenario

    if args.scenario == "topn":
        players = []
        if os.path.exists(args.ratings_db):
            pool = load_ratings_db(args.ratings_db)
            players = rank_by_stat(pool, args.stat, args.count, descending=not args.slowest)

        if not players:
            # No usable ratings cache. fut.gg publishes its own ranked
            # leaderboard per attribute, which is authoritative for exactly
            # this question -- but only best-first and only 30 deep.
            if args.slowest:
                raise SystemExit(
                    f"--slowest needs a local ratings cache: fut.gg's leaderboards are "
                    f"ranked best-first only. Run: python build_ratings_db.py")
            try:
                players = fetch_leaderboard(args.stat, limit=args.count)
            except KeyError as e:
                raise SystemExit(str(e).strip('"'))
            if not players:
                raise SystemExit(f"No players found for stat '{args.stat}'.")
            print(f"(no ratings cache at '{args.ratings_db}' -- using fut.gg's "
                  f"'{args.stat}' leaderboard instead)")
            if args.count > len(players):
                print(f"  (that leaderboard is {len(players)} deep, so this video has "
                      f"{len(players)} slides, not {args.count})")

        if not players:
            raise SystemExit(f"No players found for stat '{args.stat}'.")
        for i, p in enumerate(players):
            players_lines.append(f"{i+1}. {p.name} -- {args.stat}: {p.stats.get(args.stat, '?')} (OVR {p.ovr})")
        run_topn(players, args.stat, slides_dir, card_cache_dir, args.interval_seconds, style,
                 use_screenshots=not args.no_screenshots, transparent=True)
        contents_label = f"top{args.count}_{args.stat}"

    elif args.scenario == "comparison":
        if not args.club and not args.club_id and not args.league_id:
            raise SystemExit("Provide --club <name>, --club-id <id>, or --league-id <id>.")
        if not os.path.exists(args.ratings_db):
            raise SystemExit(f"No ratings database found at '{args.ratings_db}'. Run: python build_ratings_db.py")
        pool = load_ratings_db(args.ratings_db)
        if args.league_id:
            club_players = resolve_league_players(pool, args.league_id)
        else:
            try:
                club_players = resolve_club_players(pool, club_name=args.club, club_ids=args.club_id)
            except AmbiguousClubError as e:
                print_ambiguous_club_help(args.club, e)
                raise SystemExit(1)
        club_players = sort_and_limit_club_players(club_players, sort_by=args.sort, count=args.count)
        if not club_players:
            raise SystemExit("No players found for that club/league.")
        pairs = build_comparison_pairs_via_detail_pages(club_players)
        for i, (old, new, _) in enumerate(pairs):
            delta = new.ovr - old.ovr
            sign = "+" if delta > 0 else ""
            players_lines.append(f"{i+1}. {new.name}: {old.ovr} -> {new.ovr} ({sign}{delta})")
        run_comparison(pairs, slides_dir, card_cache_dir, args.interval_seconds, style,
                        use_screenshots=not args.no_screenshots, transparent=True)
        if args.club:
            contents_label = _slugify(args.club)
        elif args.league_id:
            contents_label = f"league_{args.league_id}"
        else:
            contents_label = f"club_{args.club_id}"

    elif args.scenario == "price":
        if not os.path.exists(args.ratings_db):
            raise SystemExit(f"No ratings database found at '{args.ratings_db}'. Run: python build_ratings_db.py")
        ratings_pool = load_ratings_db(args.ratings_db)
        easysbc_players = fetch_all_easysbc_players(club_id=args.club_id, league_id=args.league_id,
                                                      max_pages=args.max_pages)
        if not easysbc_players:
            raise SystemExit("easysbc.io returned no players for that club/league ID.")
        positions = [p.strip() for p in args.positions.split(",")] if args.positions else None
        min_stats = parse_stat_filters(args.min_stats)
        max_stats = parse_stat_filters(args.max_stats)
        filtered = apply_easysbc_filters(
            easysbc_players, positions=positions, min_stats=min_stats, max_stats=max_stats,
            min_rating=args.min_rating, max_rating=args.max_rating,
            min_price=args.min_price, max_price=args.max_price,
            min_skill_moves=args.min_skill_moves, min_weak_foot=args.min_weak_foot,
            preferred_foot=args.preferred_foot,
        )
        if not filtered:
            raise SystemExit("No players matched those filters -- try loosening the stat minimums or position list.")
        sorted_players = sort_easysbc_players(filtered, sort_by=args.sort)
        if args.count:
            sorted_players = sorted_players[:args.count]
        for i, p in enumerate(sorted_players):
            players_lines.append(f"{i+1}. {p.name} ({p.rating} OVR): {format_price(p.price)} coins")
        run_price(sorted_players, ratings_pool, slides_dir, card_cache_dir, args.interval_seconds, style,
                  use_screenshots=not args.no_screenshots, transparent=True)
        contents_label = "price_prediction"

    elif args.scenario == "evolution":
        # Unlike the stat-driven scenarios, evolution only needs the player's
        # PAGE, and can resolve that from fut.gg's sitemap index. So a missing
        # ratings cache is not fatal here -- it just means the slower lookup.
        if os.path.exists(args.ratings_db):
            ratings_pool = load_ratings_db(args.ratings_db)
        else:
            print(f"(no ratings cache at '{args.ratings_db}' -- resolving the player "
                  f"from fut.gg's player index instead)")
            ratings_pool = []
        year_cards = run_evolution(args.player_name, ratings_pool, slides_dir, card_cache_dir,
                                    args.interval_seconds, style,
                                    use_screenshots=not args.no_screenshots, transparent=True)
        for i, c in enumerate(year_cards):
            # Report the source that ACTUALLY produced the slide, not the
            # one we hoped for -- a failed card/screenshot fetch silently
            # falls back, and the contents list must not claim otherwise.
            source = c.source_used or (
                "real screenshot" if c.has_real_screenshot_source
                else "hand-drawn (real stats, no fut.gg detail page for this year)"
            )
            ovr_text = f"{c.ovr} OVR, " if c.ovr else ""
            players_lines.append(f"{i+1}. {c.year_label} -- {ovr_text}{source}")
        contents_label = _slugify(args.player_name)

    else:
        raise SystemExit(f"Unknown scenario: {args.scenario}")

    players_txt_path = os.path.join(work_dir, "players.txt")
    with open(players_txt_path, "w", encoding="utf-8") as f:
        f.write(f"FUT Video Engine -- {args.scenario} scenario\n")
        f.write("=" * 50 + "\n\n")
        f.write("\n".join(players_lines))
        f.write("\n")

    out_zip = args.out_zip or os.path.join(work_dir, f"{contents_label}.zip")
    zip_output(slides_dir, players_txt_path, out_zip)
    print(f"Contents list: {players_txt_path}")


if __name__ == "__main__":
    main()
