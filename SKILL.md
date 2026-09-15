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
  evolution (one player's plain Rare gold card across every FIFA/EA FC
  release, oldest to newest). Use whenever the person asks for a
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
  FC release

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

## Step 3 -- check the local ratings cache

Every scenario needs `ratings_fc27.json` sitting next to the scripts
(topn/comparison/evolution use it directly; price uses it to find real
screenshots by cross-referencing IDs). If it's missing:
```
python scripts/build_ratings_db.py
```
Takes a few minutes (crawls fut.gg's entire ratings list once). If it's
present but the person mentions a player/team that seems obviously
missing (a very recently revealed rating), suggest re-running this --
FC27 ratings come out in batches, so a cache from a while ago can miss
newer ones.

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
- **evolution**: recent years (confirmed FIFA 22 onward, varies by
  player) get real screenshots; older years fut.gg no longer links
  individually get the same real-stats hand-drawn fallback. This is
  expected, not a failure -- the years just look visually different
  from each other, which is inherent to fut.gg's own site structure,
  not something this pipeline can change.
- **price with no --club-id/--league-id** (a fully global search):
  defaults to a 20-page safety cap since an unscoped fetch's real size
  has never been tested at full scale. The log will say if the real
  total is bigger than what got fetched -- pass `--max-pages` higher if
  a search comes back thinner than expected.
- **`--league-id` on the price scenario specifically**: the query
  parameter name on easysbc's side is an educated guess, not confirmed
  -- `--club-id` is the fully confirmed path. If a league search
  returns something that looks wrong, fall back to club-id and mention
  this to the person.
- **Background removal needs `rembg` installed** (`pip install
  "rembg[cpu]"`, downloads a ~100-200MB model on first use, needs
  network once for that). If it's missing, the pipeline still finishes
  and prints a clear note per-image rather than crashing -- but the
  whole point of this mode is transparent backgrounds, so if you see
  that message repeatedly, stop and get `rembg` installed properly
  before delivering the zip, rather than handing over backgrounds that
  didn't actually get removed.

## What NOT to do

- Don't try to add CapCut integration back in -- that's the local
  desktop tool's job, deliberately left out here.
- Don't guess at flag names/values the person didn't give you for
  filters they didn't mention -- omit them rather than inventing a
  default that changes what shows up in the video.
- Don't skip Step 3's ratings-cache check even if you think it's
  probably already there -- the error message if it's missing is
  much less useful mid-run than catching it up front.
