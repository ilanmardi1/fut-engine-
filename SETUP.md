# One-time setup

1. **Install this skill.** Drop this whole `fut-video-engine/` folder
   into wherever Claude Code loads your skills from.

2. **Python dependencies:**
   ```
   pip install -r requirements.txt
   ```
   Do NOT run `playwright install chromium` blindly. requirements.txt
   pins `playwright==1.56.0` because its bundled Chromium is the build
   this pipeline was verified against; an unpinned install pulls a
   Playwright whose Chromium may not be present, and every page load
   then fails.
   `rembg[cpu]` (used for background removal) downloads a ~100-200MB
   segmentation model the first time it actually runs -- that needs
   network access once, separate from the pip install itself.

3. **The ratings cache is optional for most scenarios, and its builder
   is currently broken.** `build_ratings_db.py` exits 0 while saving
   zero players, because fut.gg moved its ratings listing. evolution
   and topn do not need it (they use fut.gg's sitemap and per-stat
   leaderboards instead); comparison and price do, and are blocked
   until it is rebuilt.

4. **Test it end to end on one small video before trusting it fully** --
   same advice as building anything new into a working pipeline:
   ```
   python scripts/run_skill.py topn --stat PAC --count 3
   python scripts/run_skill.py evolution --player-name "Dembele"
   ```
   The second one exercises the browser path (the history-archive
   screenshots) which the first does not.
   Confirms the environment (network, Playwright's browser, `rembg`) is
   all working before you ask for a real video.

That's it -- from here, just tell Claude Code what kind of FUT video you
want (see `SKILL.md` for the four scenarios and what each one needs),
and it runs the real pipeline and hands you back a zip.

## If something's missing

- **"No ratings database found"** -- only comparison and price still
  require it, and its builder is broken (see step 3). evolution and
  topn no longer ask for it at all.

- **Every page load fails with ERR_CERT_AUTHORITY_INVALID** -- Chromium
  does not trust your egress proxy's CA. The screenshotter routes
  requests through Python instead (`fetch_via_python=True`, the
  default), which keeps TLS verification on; pass False only when
  Chromium can reach the network directly.
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
