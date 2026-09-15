"""assets.py -- tiny shared helper for downloading card art / other assets."""
from __future__ import annotations

import os
import requests

from fetch_ratings import USER_AGENT


def download_image(url: str, dest_path: str, session: requests.Session | None = None) -> str:
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    sess = session or requests.Session()
    resp = sess.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
