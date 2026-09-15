"""
screenshot_card.py
-------------------
Takes a REAL screenshot of a player's card as rendered by fut.gg itself --
not a hand-drawn approximation, not a guessed asset URL. CONFIRMED WORKING
against a real run (Mbappe FC27 card screenshotted correctly by the user).

WHY THIS WORKS: fut.gg's player detail pages are server-rendered HTML (not
JS-injected -- that's how fetch_ratings.py has been reading stats this
whole time), so a real browser screenshot of the card element is, by
construction, pixel-identical to what you'd see screenshotting it
yourself. The FC26 version of any player uses the exact same page
template -- just swap the year prefix in the URL (confirmed via fut.gg's
own "FC 26 Version" link): .../27-{ea_id}/ -> .../26-{ea_id}/.

TWO WAYS TO USE THIS:

1. One-off CLI test (what you already ran successfully):
     python screenshot_card.py <detail_url> <out_prefix>

2. Batch use from build_video.py: the CardScreenshotter class below keeps
   ONE browser instance open across many players instead of relaunching
   Chromium per player (which is what the CLI path above does, and is
   fine for a single test but far too slow for a 20-player video).

SETUP (one time, already done if step 1 above worked for you):
    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional

STAT_SETS = [
    ["PAC", "SHO", "PAS", "DRI", "DEF", "PHY"],
    ["DIV", "HAN", "KIC", "REF", "SPD", "POS"],
]

FIND_CARD_CONTAINER_JS = """
(faceImgSrcSubstr) => {
    function textOf(el) { return (el.innerText || "").toUpperCase(); }

    const imgs = Array.from(document.querySelectorAll("img"));
    const faceImg = imgs.find(i => i.src && i.src.includes(faceImgSrcSubstr));
    if (!faceImg) return { error: "no face image found matching " + faceImgSrcSubstr };

    const statSets = [
        ["PAC", "SHO", "PAS", "DRI", "DEF", "PHY"],
        ["DIV", "HAN", "KIC", "REF", "SPD", "POS"],
    ];

    let el = faceImg;
    let hops = 0;
    let matchedContainer = null;
    let matchedStats = null;
    while (el.parentElement && hops < 15) {
        el = el.parentElement;
        hops += 1;
        const text = textOf(el);
        for (const stats of statSets) {
            if (stats.every(s => text.includes(s))) {
                matchedContainer = el;
                matchedStats = stats;
                break;
            }
        }
        if (matchedContainer) break;
    }

    if (!matchedContainer) {
        return { error: "climbed " + hops + " ancestors, never found a container with all 6 stat abbreviations" };
    }

    const containerRect = matchedContainer.getBoundingClientRect();

    // Anchor the BOTTOM edge to the nation flag icon (every player has
    // one) instead of trusting the matched container's own bottom edge.
    // Real bug found from an actual screenshot: the container's bottom
    // sometimes extends past the visible card to include unrelated page
    // elements just below it (e.g. rating-vote arrow icons), which
    // showed up as stray white shapes at the bottom of a real slide.
    // The nation/league/club icon row is always the true bottom of a
    // real FUT card, so anchoring there instead is far more reliable
    // than trusting wherever the container's box happens to end.
    const nationImgs = Array.from(matchedContainer.querySelectorAll("img"))
        .filter(i => i.src && i.src.includes("/nation/"));
    let bottom = containerRect.bottom;
    let bottomSource = "container";
    if (nationImgs.length > 0) {
        const nationRect = nationImgs[0].getBoundingClientRect();
        // Sanity-check it's plausibly part of THIS card (below the top
        // of the matched container, above the container's own claimed
        // bottom) before trusting it as the crop boundary.
        if (nationRect.bottom > containerRect.top + 50 && nationRect.bottom <= containerRect.bottom) {
            bottom = nationRect.bottom + 20;  // small margin below the icon row
            bottomSource = "nation-icon";
        }
    }

    return {
        hops: hops,
        matchedStats: matchedStats,
        rect: {
            x: containerRect.x,
            y: containerRect.y,
            width: containerRect.width,
            height: bottom - containerRect.y,
        },
        bottomSource: bottomSource,
        textPreview: (matchedContainer.innerText || "").slice(0, 300),
    };
}
"""


def fc_year_url(detail_url: str, ea_id: str, target_year: str) -> str:
    """Derives a different game-year's detail URL for the same player,
    e.g. fc_year_url('.../231747-kylian-mbappe/27-231747/', '231747', '26')
    -> '.../231747-kylian-mbappe/26-231747/'. Confirmed against fut.gg's
    own "FC 26 Version" link, which follows exactly this pattern."""
    return re.sub(rf"/\d+-{re.escape(ea_id)}/?$", f"/{target_year}-{ea_id}/", detail_url)



# Finds one card inside the "FIFA History" archive.
#
# Two keys are needed together, confirmed against real data:
#  * OVR + position + all six stats, because a face image alone is NOT
#    unique -- the same historical-player-face asset is reused across the
#    page, and matching on it unscoped resolves to the "Best Cards" strip
#    (Dembele's FIFA 21 face hit the 91-rated special, not the 83 Rare).
#  * the face image id, because stats alone are NOT unique either -- a
#    promo can carry stats identical to the base Rare. Confirmed: FIFA 18
#    had 2 stat-matches and FIFA 20 had 3, and in both cases the FIRST was
#    a promo design; only the face id picked out the real gold Rare.
# Requiring both yielded exactly one candidate for every year tested.
FIND_HISTORY_CARD_JS = r"""
(spec) => {
  const root = document.querySelector('#history') || document.body;
  const cards = Array.from(root.querySelectorAll('.hc-card-container, .fut-card-container'));
  if (!cards.length) return {error: 'no card containers in history section'};

  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const matches = [];
  cards.forEach((el, idx) => {
    const t = norm(el.innerText).toUpperCase();
    if (!t) return;
    // "<OVR> <POS> <NAME> <val><STAT> ..." -- anchor on OVR + position.
    const head = new RegExp('^' + spec.ovr + '\\s+' + spec.position + '\\b');
    if (!head.test(t)) return;
    const statsOk = Object.entries(spec.stats).every(
      ([k, v]) => new RegExp('\\b' + v + '\\s*' + k + '\\b').test(t));
    if (!statsOk) return;
    if (spec.face_id) {
      const imgs = Array.from(el.querySelectorAll('img')).map(i => i.src);
      const hit = imgs.some(src => src.includes('/' + spec.face_id + '.'));
      if (!hit) return;
    }
    matches.push(idx);
  });

  if (!matches.length) return {error: 'no card matched', ovr: spec.ovr, position: spec.position};
  return {index: matches[0], ambiguous: matches.length > 1, count: matches.length};
}
"""

# Strips the page's own backgrounds off the chosen card and everything
# behind it, so the element screenshot can be taken with a real alpha
# channel (omit_background) instead of being cut out afterwards by rembg.
CLEAR_BACKDROP_JS = r"""
(index) => {
  const root = document.querySelector('#history') || document.body;
  const cards = Array.from(root.querySelectorAll('.hc-card-container, .fut-card-container'));
  const el = cards[index];
  if (!el) return false;
  // Remember each element's own inline style so it can be put back. The
  // ancestors here are SHARED between cards, so leaving them stripped
  // reflows the page for every later capture -- that is what clipped one
  // card's OVR block off in a batch run while it captured fine alone.
  const stash = (n) => {
    if (n.dataset.futPrevStyle === undefined) {
      n.dataset.futPrevStyle = n.getAttribute('style') || '';
    }
  };
  stash(document.documentElement); stash(document.body);
  document.documentElement.style.background = 'transparent';
  document.body.style.background = 'transparent';

  // Hide sticky/fixed chrome. scroll_into_view_if_needed can park a card
  // underneath fut.gg's sticky site header, and an element screenshot
  // still captures whatever paints ON TOP of the element's box -- that is
  // what put a dark strip across one year's OVR/position block.
  document.querySelectorAll('body *').forEach(n => {
    const pos = getComputedStyle(n).position;
    if (pos === 'fixed' || pos === 'sticky') {
      stash(n);
      n.style.visibility = 'hidden';
    }
  });
  let n = el.parentElement;
  while (n) {
    stash(n);
    n.style.background = 'transparent';
    n.style.backgroundImage = 'none';
    n.style.boxShadow = 'none';
    n.style.border = 'none';
    n = n.parentElement;
  }
  return true;
}
"""

RESTORE_BACKDROP_JS = r"""
() => {
  document.querySelectorAll('[data-fut-prev-style]').forEach(n => {
    const prev = n.dataset.futPrevStyle;
    if (prev) { n.setAttribute('style', prev); } else { n.removeAttribute('style'); }
    delete n.dataset.futPrevStyle;
  });
  return true;
}
"""


class CardScreenshotter:
    """Keeps one headless-Chromium instance open across many
    screenshot_card() calls -- use as a context manager so many players in
    one video only pay the browser-launch cost once, not once per player.

        with CardScreenshotter() as shooter:
            shooter.screenshot_card(url1, "231747", "27", "out1.png")
            shooter.screenshot_card(url2, "239085", "27", "out2.png")
    """

    # Hosts whose responses are actually needed to render a card. Anything
    # else (analytics, ads, session replay) is aborted: it cannot affect
    # the card's pixels and every extra request costs a round trip through
    # the fetch-via-Python transport below.
    ALLOWED_HOST_SUFFIXES = ("fut.gg", "fonts.googleapis.com", "fonts.gstatic.com")

    def __init__(self, viewport: tuple[int, int] = (1600, 1200), headless: bool = True,
                 fetch_via_python: bool = True, device_scale_factor: float = 4.0):
        self.viewport = viewport
        # Cards in the FIFA History archive render at only ~174x244 CSS px.
        # Capturing at 1x would upscale badly onto a 1920x1080 slide, so
        # screenshot at a higher device pixel ratio instead.
        self.device_scale_factor = device_scale_factor
        self.headless = headless
        # Route every request through Python's requests instead of letting
        # Chromium open its own TLS connections.
        #
        # Why this exists: in a sandbox whose egress goes through a
        # TLS-terminating proxy, Python is configured to trust the proxy's
        # CA (REQUESTS_CA_BUNDLE etc.) but Chromium is not, so every
        # page.goto() dies with ERR_CERT_AUTHORITY_INVALID before any card
        # logic runs -- and because a failed screenshot is a silent
        # fall-through here, that surfaced as blank cards rather than an
        # error. Fulfilling each request from Python keeps verification
        # fully ON (Python still validates against the proxy CA); it just
        # moves who opens the socket. Set fetch_via_python=False to let
        # Chromium talk to the network directly.
        self.fetch_via_python = fetch_via_python
        self._session = None
        self._pw = None
        self._browser = None
        self._page = None

    def _install_python_transport(self, page) -> None:
        import requests as _requests
        from urllib.parse import urlparse

        self._session = _requests.Session()
        from fetch_ratings import USER_AGENT
        self._session.headers.update({"User-Agent": USER_AGENT})

        def _handler(route, request):
            host = (urlparse(request.url).hostname or "").lower()
            if not any(host == h or host.endswith("." + h) for h in self.ALLOWED_HOST_SUFFIXES):
                route.abort()
                return
            try:
                r = self._session.request(
                    request.method, request.url,
                    data=request.post_data_buffer, timeout=30,
                )
                # Drop hop-by-hop / encoding headers: requests has already
                # decompressed the body, so passing the original
                # content-encoding through would make Chromium try to
                # decode it a second time.
                headers = {k: v for k, v in r.headers.items()
                           if k.lower() not in ("content-encoding", "content-length",
                                                "transfer-encoding", "connection")}
                route.fulfill(status=r.status_code, headers=headers, body=r.content)
            except Exception:
                try:
                    route.abort()
                except Exception:
                    pass

        page.route("**/*", _handler)

    def __enter__(self) -> "CardScreenshotter":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise ImportError(
                "playwright is not installed. Run:\n"
                "    pip install playwright\n"
                "    playwright install chromium"
            ) from e
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page(
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
            device_scale_factor=self.device_scale_factor,
        )
        if self.fetch_via_python:
            self._install_python_transport(self._page)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Routes still in flight when the page goes away raise
        # CancelledError from Playwright's event loop; that is noise from
        # shutdown ordering, not a failure of any screenshot.
        if self._page:
            try:
                self._page.unroute_all(behavior="ignoreErrors")
            except Exception:
                pass
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()
        if self._session:
            self._session.close()

    def screenshot_card(
        self,
        detail_url: str,
        ea_id: str,
        game_year: str,
        out_path: str,
        debug_dir: Optional[str] = None,
    ) -> bool:
        """Navigates to detail_url and saves a cropped screenshot of just
        the card to out_path. Returns True on success. On ANY failure --
        the heuristic couldn't find a matching container, OR the page
        itself failed to load/navigate/timeout -- returns False instead
        of raising, and saves debug info if debug_dir is given. This is
        deliberate: one flaky player's page shouldn't crash a 20-player
        batch. Callers should fall back to another card source when this
        returns False.

        Navigation waits for "load" (all initial resources fetched), not
        "networkidle" -- confirmed via a real failure: the FIRST page
        load in a freshly-launched browser timed out waiting for
        networkidle, almost certainly because fut.gg has ongoing
        background requests (analytics/trackers) that never let the
        network go fully quiet for 500ms straight, which "networkidle"
        requires. Since these pages are server-rendered (the card's HTML,
        including image src attributes, is present in the initial
        response -- confirmed all along by fetch_ratings.py's plain-HTTP
        parsing), "load" is both sufficient and far more reliable. Also
        retries once with a longer timeout on a navigation timeout
        specifically, since a slow cold start on the first page of a
        batch is a real, recurring pattern, not a one-off fluke."""
        page = self._page
        try:
            self._goto_with_retry(page, detail_url)

            face_src_substr = f"/player-item/{game_year}-{ea_id}."
            result = page.evaluate(FIND_CARD_CONTAINER_JS, face_src_substr)

            if "error" in result:
                if debug_dir:
                    os.makedirs(debug_dir, exist_ok=True)
                    base = f"{ea_id}_{game_year}"
                    page.screenshot(path=os.path.join(debug_dir, f"{base}_full.png"), full_page=True)
                    with open(os.path.join(debug_dir, f"{base}_debug.txt"), "w", encoding="utf-8") as f:
                        f.write(f"URL: {detail_url}\nLooking for: {face_src_substr}\nResult: {result}\n")
                return False

            rect = result["rect"]
            pad = 16
            clip = {
                "x": max(0, rect["x"] - pad),
                "y": max(0, rect["y"] - pad),
                "width": rect["width"] + pad * 2,
                "height": rect["height"] + pad * 2,
            }
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            page.screenshot(path=out_path, clip=clip)
            return True
        except Exception as e:
            if debug_dir:
                os.makedirs(debug_dir, exist_ok=True)
                base = f"{ea_id}_{game_year}"
                with open(os.path.join(debug_dir, f"{base}_debug.txt"), "w", encoding="utf-8") as f:
                    f.write(f"URL: {detail_url}\nNavigation/screenshot failed: {type(e).__name__}: {e}\n")
            return False

    def screenshot_historical_cards(
        self,
        overview_url: str,
        requests: list,  # list of (face_image_url, out_path) tuples
        debug_dir: Optional[str] = None,
    ) -> dict:
        """NEW capability, genuinely UNTESTED against a real live page --
        needs verification in a real environment before being trusted.

        For years with no individual detail page to screenshot (the
        player-evolution scenario's older-year fallback case -- see
        player_history.py's module docstring), a real, fully-styled card
        turns out to still exist -- just rendered as part of the
        player's OVERVIEW page's "FIFA History" section (confirmed from
        a real screenshot of that section showing genuine polished
        cards, not placeholders), rather than on its own dedicated URL.
        This reuses the EXACT SAME proven container-finding heuristic
        (FIND_CARD_CONTAINER_JS) already confirmed working for detail
        pages, just anchored on a specific historical face image URL
        instead of a "player-item/{year}-{id}" one.

        Navigates to overview_url ONCE, then attempts every
        (face_image_url, out_path) pair against that SAME loaded page --
        much cheaper than a separate page load per year, and the
        overview page already contains every year's card at once.

        Returns {face_image_url: True/False} per request. A given
        request failing (heuristic didn't find/isolate that specific
        card correctly -- a real risk this hasn't been tested against,
        since the older detail-page method dealt with ONE dominant card
        per page, not several in a row/grid) should fall back to the
        existing hand-drawn-shield path, exactly like a failed
        screenshot_card() call already does elsewhere."""
        page = self._page
        results = {}
        try:
            self._goto_with_retry(page, overview_url)
        except Exception as e:
            if debug_dir:
                os.makedirs(debug_dir, exist_ok=True)
                with open(os.path.join(debug_dir, "historical_overview_debug.txt"), "w", encoding="utf-8") as f:
                    f.write(f"URL: {overview_url}\nNavigation failed: {type(e).__name__}: {e}\n")
            return {face_url: False for face_url, _ in requests}

        for face_image_url, out_path in requests:
            m = re.search(r"historical-player-face/(\d+)\.", face_image_url)
            face_src_substr = f"historical-player-face/{m.group(1)}." if m else face_image_url
            try:
                result = page.evaluate(FIND_CARD_CONTAINER_JS, face_src_substr)
                if "error" in result:
                    if debug_dir:
                        os.makedirs(debug_dir, exist_ok=True)
                        safe_name = re.sub(r"[^\w.-]", "_", face_src_substr)
                        with open(os.path.join(debug_dir, f"historical_{safe_name}_debug.txt"),
                                  "w", encoding="utf-8") as f:
                            f.write(f"Overview URL: {overview_url}\nLooking for: {face_src_substr}\n"
                                    f"Result: {result}\n")
                    results[face_image_url] = False
                    continue

                rect = result["rect"]
                pad = 16
                clip = {
                    "x": max(0, rect["x"] - pad),
                    "y": max(0, rect["y"] - pad),
                    "width": rect["width"] + pad * 2,
                    "height": rect["height"] + pad * 2,
                }
                os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
                page.screenshot(path=out_path, clip=clip)
                results[face_image_url] = True
            except Exception as e:
                if debug_dir:
                    os.makedirs(debug_dir, exist_ok=True)
                    safe_name = re.sub(r"[^\w.-]", "_", face_src_substr)
                    with open(os.path.join(debug_dir, f"historical_{safe_name}_debug.txt"),
                              "w", encoding="utf-8") as f:
                        f.write(f"Overview URL: {overview_url}\nScreenshot failed: {type(e).__name__}: {e}\n")
                results[face_image_url] = False

        return results

    def screenshot_history_cards(
        self,
        overview_url: str,
        requests: list,   # list of dicts: {key, ovr, position, stats, out_path}
        debug_dir: Optional[str] = None,
    ) -> dict:
        """Screenshots each requested card out of the overview page's
        "FIFA History" archive, matched by the OVR/position/stats already
        parsed for that card (see FIND_HISTORY_CARD_JS for why face-image
        matching is not safe here).

        Navigates once, then captures every request against that same
        loaded page. Uses Playwright's element screenshot rather than a
        clipped page screenshot: the archive runs well below the fold, and
        a clip cannot reach outside the viewport (the earlier attempt died
        with "Clipped area is either empty or outside the resulting
        image"), whereas an element screenshot scrolls itself into view.

        Returns {key: True/False}. A False should fall back to the
        hand-drawn card exactly as before."""
        page = self._page
        results = {r["key"]: False for r in requests}

        def _debug(name: str, body: str) -> None:
            if not debug_dir:
                return
            os.makedirs(debug_dir, exist_ok=True)
            safe = re.sub(r"[^\w.-]", "_", name)
            with open(os.path.join(debug_dir, f"history_{safe}_debug.txt"), "w", encoding="utf-8") as f:
                f.write(body)

        try:
            self._goto_with_retry(page, overview_url)
            # The archive is far down the page; scroll so it lays out.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
        except Exception as e:
            _debug("overview", f"URL: {overview_url}\nNavigation failed: {type(e).__name__}: {e}\n")
            return results

        selector = "#history .hc-card-container, #history .fut-card-container"
        for req in requests:
            key = req["key"]
            try:
                found = page.evaluate(FIND_HISTORY_CARD_JS, {
                    "ovr": req["ovr"], "position": req["position"], "stats": req["stats"],
                    "face_id": req.get("face_id"),
                })
                if "error" in found:
                    _debug(key, f"URL: {overview_url}\nLooking for: {req}\nResult: {found}\n")
                    continue
                loc = page.locator(selector).nth(found["index"])
                # Scroll first, THEN clear backdrops: clearing backgrounds on
                # shared ancestors reflows the page, so a card measured
                # before that can end up captured half off its own box.
                os.makedirs(os.path.dirname(req["out_path"]) or ".", exist_ok=True)
                loc.scroll_into_view_if_needed()
                page.wait_for_timeout(200)
                page.evaluate(CLEAR_BACKDROP_JS, found["index"])
                try:
                    page.wait_for_timeout(150)
                    loc.screenshot(path=req["out_path"], omit_background=True)
                finally:
                    # Always put the page back, so the next card in this
                    # batch measures against the original layout.
                    page.evaluate(RESTORE_BACKDROP_JS)
                results[key] = True
                if found.get("ambiguous"):
                    print(f"  ({key}: {found['count']} cards matched those stats -- used the first)")
            except Exception as e:
                _debug(key, f"URL: {overview_url}\nLooking for: {req}\n"
                            f"Screenshot failed: {type(e).__name__}: {e}\n")
        return results

    @staticmethod
    def _goto_with_retry(page, url: str, timeout_ms: int = 30000, retry_timeout_ms: int = 45000) -> None:
        """page.goto() with one retry at a longer timeout if the first
        attempt times out specifically -- other exception types (DNS
        failure, connection refused, etc.) propagate immediately since a
        longer wait won't help those."""
        try:
            page.goto(url, wait_until="load", timeout=timeout_ms)
        except Exception as e:
            if "Timeout" not in type(e).__name__ and "timeout" not in str(e).lower():
                raise
            page.goto(url, wait_until="load", timeout=retry_timeout_ms)


def screenshot_player_card(detail_url: str, out_prefix: str, ea_id: str, game_year: str = "27") -> dict:
    """One-off convenience wrapper (launches and closes its own browser) --
    used by the CLI below for a single test. For more than one player, use
    CardScreenshotter directly instead so the browser is reused."""
    with CardScreenshotter() as shooter:
        page = shooter._page
        CardScreenshotter._goto_with_retry(page, detail_url)
        full_path = f"{out_prefix}_full.png"
        page.screenshot(path=full_path, full_page=True)

        face_src_substr = f"/player-item/{game_year}-{ea_id}."
        result = page.evaluate(FIND_CARD_CONTAINER_JS, face_src_substr)

        card_saved = False
        debug_lines = [f"URL: {detail_url}", f"Looking for: {face_src_substr}", f"\nFull-page screenshot: {full_path}",
                        f"\nHeuristic result:\n{result}"]

        if "error" not in result:
            rect = result["rect"]
            pad = 16
            clip = {"x": max(0, rect["x"] - pad), "y": max(0, rect["y"] - pad),
                    "width": rect["width"] + pad * 2, "height": rect["height"] + pad * 2}
            card_path = f"{out_prefix}_card.png"
            page.screenshot(path=card_path, clip=clip)
            debug_lines.append(f"\nCropped card screenshot: {card_path}\nCrop region: {clip}")
            card_saved = True

        debug_path = f"{out_prefix}_debug.txt"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write("\n".join(debug_lines))

    return {"full_screenshot_saved": True, "card_screenshot_saved": card_saved,
            "debug_path": debug_path, "heuristic_result": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Screenshot a real fut.gg player card.")
    parser.add_argument("detail_url", help="e.g. https://www.fut.gg/players/231747-kylian-mbappe/27-231747/")
    parser.add_argument("out_prefix", help="output filename prefix, e.g. mbappe_test")
    args = parser.parse_args()

    m = re.search(r"/(\d+)-[\w-]+/(\d+)-\1/?$", args.detail_url)
    if not m:
        print("Couldn't parse an ea_id/game_year out of that URL -- expected a shape like "
              ".../players/231747-kylian-mbappe/27-231747/", file=sys.stderr)
        sys.exit(1)
    ea_id, game_year = m.group(1), m.group(2)

    result = screenshot_player_card(args.detail_url, args.out_prefix, ea_id, game_year)
    print(result)
