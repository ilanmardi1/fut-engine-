---
name: fut-video-engine
description: >
  Generates FUT/EA FC 27 card slideshow videos as a zip of
  transparent-background slide images plus a players.txt contents list
  -- no CapCut integration, the person edits the result themselves.
  Four scenarios: topn (rank by a single stat), comparison (FC26 vs
  FC27 for a club/league), price (market-price prediction via
  easysbc.io, with filters like position, per-stat min/max,
  price/rating range, skill moves, weak foot, preferred foot), and
  evolution (one player's plain base card -- gold, silver or bronze --
  across every FIFA/EA FC release, oldest to newest). Use whenever the
  person asks for a
  FUT/FIFA/EA FC video, card slideshow, or names this skill. Always
  fetches live data from fut.gg and easysbc.io, aside from a local
  ratings_fc27.json crawl cache the person should refresh periodically.
  Requires Claude Code (real network access plus a real browser for
  screenshots) -- shells out to scripts/run_skill.py, which fetches
  live pages and runs Playwright; a browser-only chat session cannot
  do this.
---

# FUT Video Engine (skill)

This is the CapCut-free sibling of a local desktop tool by the same
name (the same `scripts/` are lifted directly from it, unmodified,
minus the CapCut-writing step) -- built and genuinely tested over a long
back-and-forth against real fut.gg and easysbc.io data, not written
from assumptions. Every scenario, every fallback path, and the specific
edge cases called out below were each found from a real run, not
guessed at.

## What this produces

One command builds:
- `slides/` -- one image per player/year, in order, background always
  stripped (this mode always uses `--transparent`, not just as an
  option)
- `players.txt` -- exactly who/what appears in the video, in the same
  order as the slides
- Both zipped together into one deliverable

No CapCut draft, no video file -- the person edits the slides into
whatever tool they use themselves.

## Step 1 -- figure out which scenario

If the person hasn't already said which of the four they want, ask.
Use natural language, not a jargon dump of flag names:

- **"Top N"** -- rank the whole player pool by one stat (fastest,
  best shooters, etc.)
- **"Comparison"** -- FC26 -> FC27 rating changes for one club or a
  whole league
- **"Price"** -- market-price prediction video, with real filtering
  (e.g. "La Liga cheap beasts: min 90 pace, min 80 dribble, max 10,000
  coins")
- **"Evolution"** -- one player's card history across every FIFA/EA
  FC release. Covers their base card each year whatever its quality:
  a gold once they're rated for it, a silver or bronze before that
  (fut.gg labels those "Common" rather than "Rare", and both count)

## Step 2 -- gather the inputs that scenario needs

Ask only for what's actually needed, in plain language, and translate
it into the right flags yourself -- don't make the person learn the
CLI. Reasonable examples per scenario:

- **topn**: which stat, how many players, fastest-first or
  slowest-first, roughly how long each slide should stay on screen.
- **comparison**: a club name, or a whole league (if they don't know
  the league ID, run `python scripts/build_video.py list-leagues`
  first and show them the options with sample clubs), sort order,
  how many players.
- **price**: club/league/neither (a totally global search is
  supported too -- e.g. "cheapest attackers in the whole game"), any
  combination of: positions (multiple, e.g. "ST or LW or RW"), minimum
  and/or maximum for any of PAC/SHO/PAS/DRI/DEF/PHY, min/max rating,
  min/max price, min skill moves, min weak foot, preferred foot, sort
  order, how many players. Don't require all of these -- most requests
  only specify 2-4 filters.
- **evolution**: just a player name (accent-insensitive, e.g. "Dembele"
  finds "Ousmane Dembélé" -- don't ask the person to type accents).

## Step 3 -- the local ratings cache (only some scenarios need it)

`build_ratings_db.py` is CURRENTLY BROKEN against the live site: fut.gg
moved its ratings listing (the old URL now redirects to a differently
structured page) and the crawl completes with exit code 0 having saved
ZERO players. Check the file has real content before trusting it --
an empty `ratings_fc27.json` is the expected result right now, not a
working cache.

What that means per scenario:

- **evolution** -- does NOT need the cache. It resolves the player from
  fut.gg's sitemap index (~20k players, cached as
  `players_index_fc27.json`, rebuilt automatically).
- **topn** -- does NOT need the cache. It falls back to fut.gg's own
  per-stat leaderboard, which is already ranked best-first. Limits: 30
  players deep, best-first only (so `--slowest` still needs the cache).
- **comparison** and **price** -- still need the cache, so both are
  blocked until the crawler is rebuilt.

If the cache DOES exist and has real players, every scenario prefers it.

## Step 4 -- run it

Translate the gathered inputs into one `run_skill.py` call. Examples:
```
python scripts/run_skill.py topn --stat PAC --count 20
python scripts/run_skill.py comparison --club "Arsenal" --count 15
python scripts/run_skill.py price --league-id 13 --positions "ST,LW,RW" \
    --min-stats "PAC=88,DRI=80,SHO=78" --max-price 10000 --sort price-asc --count 15
python scripts/run_skill.py evolution --player-name "Dembele"
```
Full flag reference: `python scripts/run_skill.py <scenario> --help`.

One sentence before running it ("Building a price-prediction video for
La Liga, filtering to attackers under 10,000 coins with strong pace and
dribbling...") -- not a paragraph, not a flag dump.

## Step 5 -- hand over the result

The command prints the zip's path at the end (also visible via
`--out-zip <path>` if the person wants it somewhere specific). Give
them that file to download. Don't re-describe `players.txt`'s contents
in the chat -- it's already in the zip for them to open.

## Known real fallbacks and quirks -- don't treat these as bugs

Confirmed from real runs, all handled automatically already:

- **price/comparison**: if a real fut.gg screenshot isn't available for
  a player, falls back to fut.gg's social-preview card, then a
  hand-drawn card using real scraped stats -- never fake/placeholder
  data, just a different visual source. A note prints when this
  happens; no action needed.
- **evolution**: every year now uses real fut.gg artwork. Recent years
  come from fut.gg's standalone card asset (a plain HTTP fetch, already
  transparent); older years have no downloadable asset at all, so they
  are screenshotted out of the overview page's FIFA History archive in a
  single page load. A year only falls back to the hand-drawn card if
  both of those fail -- if you see "hand-drawn" in players.txt for a
  modern player, something is wrong, not degraded.
