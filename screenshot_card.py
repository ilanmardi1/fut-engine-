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


class CardScreenshotter:
    """Keeps one headless-Chromium instance open across many
    screenshot_card() calls -- use as a context manager so many players in
    one video only pay the browser-launch cost once, not once per player.

        with CardScreenshotter() as shooter:
            shooter.screenshot_card(url1, "231747", "27", "out1.png")
            shooter.screenshot_card(url2, "239085", "27", "out2.png")
    """

    def __init__(self, viewport: tuple[int, int] = (1600, 1200), headless: bool = True):
        self.viewport = viewport
        self.headless = headless
        self._pw = None
        self._browser = None
        self._page = None

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
        self._page = self._browser.new_page(viewport={"width": self.viewport[0], "height": self.viewport[1]})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

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
