# One-time setup

1. **Install this skill.** Drop this whole `fut-video-engine/` folder
   into wherever Claude Code loads your skills from.

2. **Python dependencies:**
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```
   `rembg[cpu]` (used for background removal) downloads a ~100-200MB
   segmentation model the first time it actually runs -- that needs
   network access once, separate from the pip install itself.

3. **Build the local ratings cache** (one-time, then reusable -- re-run
   it periodically since FC27 ratings get revealed in batches, so a
   stale cache can be missing recently-added players):
   ```
   python scripts/build_ratings_db.py
   ```
   Takes a few minutes -- it crawls fut.gg's entire current ratings list
   once and saves it as `ratings_fc27.json` next to the scripts. Every
   scenario needs this file to exist first.

4. **Test it end to end on one small video before trusting it fully** --
   same advice as building anything new into a working pipeline:
   ```
   python scripts/run_skill.py topn --stat PAC --count 3
   ```
   Confirms the environment (network, Playwright's browser, `rembg`) is
   all working before you ask for a real video.

That's it -- from here, just tell Claude Code what kind of FUT video you
want (see `SKILL.md` for the four scenarios and what each one needs),
and it runs the real pipeline and hands you back a zip.

## If something's missing

- **"No ratings database found"** -- run step 3 above (or its skill
  version, `python scripts/build_ratings_db.py`, is the same thing).
- **Playwright errors about a missing browser** -- re-run
  `playwright install chromium` from step 2.
- **"rembg is not installed"** printed per-image instead of a crash** --
  the pipeline keeps going with backgrounds left on rather than
  failing outright, but re-run `pip install "rembg[cpu]"` before
  trusting a real delivered zip, since removing backgrounds is the
  whole point of this mode.
- **A price-scenario `--league-id` search looks wrong** -- this
  specific parameter is an educated guess, not confirmed working (see
  `SKILL.md`'s "known quirks" section) -- use `--club-id` instead, or
  run `python scripts/build_video.py list-leagues` to find IDs by
  sample club names.
