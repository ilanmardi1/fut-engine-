"""
build_ratings_db.py
--------------------
Run this ONCE (and again whenever EA reveals a new batch of ratings) to
pull fut.gg's entire ratings list -- not just the top 100 -- and cache it
locally as ratings_fc27.json. Every build_video.py run after that reads
from this local file instead of hitting the network, which is both much
faster and the only correct way to do a "top N by <stat>" video (fut.gg
itself is always OVR-sorted; only having the full list lets you re-sort
by any stat locally).

USAGE:
    python build_ratings_db.py                  # full crawl, ~417 pages
    python build_ratings_db.py --max-pages 5     # quick sanity check first
"""
import sys

# Same fix as build_video.py -- Windows' default console codepage
# (often cp1252) can't represent some real player names (Central/Eastern
# European characters like ć, č, š, ž), which would otherwise crash the
# "fastest outfield player" sanity print below.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse

from fetch_ratings import fetch_all_ratings, save_ratings_db


def main():
    parser = argparse.ArgumentParser(description="Crawl fut.gg's full ratings list into a local cache.")
    parser.add_argument("--game-year", default="27")
    parser.add_argument("--out", default="ratings_fc27.json")
    parser.add_argument("--delay", type=float, default=0.4, help="seconds between page requests")
    parser.add_argument("--max-pages", type=int, default=None, help="cap pages (for a quick test run)")
    args = parser.parse_args()

    print("Starting crawl -- this pulls the ENTIRE fut.gg ratings list (hundreds of pages).")
    print("Try --max-pages 5 first if you just want to sanity-check this works before the full run.\n")

    players = fetch_all_ratings(
        game_year=args.game_year,
        delay_seconds=args.delay,
        max_pages=args.max_pages,
    )
    save_ratings_db(players, args.out)

    # Quick sanity print so you can eyeball that the data looks right.
    outfield = [p for p in players if not p.is_gk()]
    if outfield:
        fastest = max(outfield, key=lambda p: p.stat("PAC") or 0)
        print(f"\nSanity check -- fastest outfield player found: {fastest.name} "
              f"({fastest.club}, PAC {fastest.stat('PAC')}, OVR {fastest.ovr})")


if __name__ == "__main__":
    main()
