"""
bg_remove.py
------------
Wraps `rembg` to strip the background off a real card screenshot, so you
can lay your own background image/video underneath the cards in CapCut
instead of the flat navy gradient this pipeline bakes in by default.

SETUP:
    pip install "rembg[cpu]"
(the bare `pip install rembg` does NOT reliably pull in a working
inference backend -- confirmed by a real failure: it left onnxruntime
missing entirely, and rembg's own error message pointed at the [cpu]
extra as the fix. First use downloads a segmentation model, ~100-200MB,
needs network once; if you hit a DLL error mentioning
onnxruntime_pybind11_state on Windows, install the latest Visual C++
Redistributable from Microsoft)

STATUS: rembg's actual segmentation quality on a real fut.gg card
screenshot has NOT been verified -- no network access in the environment
this was built in, so I couldn't install rembg or run it against a real
image myself. The API below matches rembg's documented interface
(confirmed via its PyPI page and GitHub README: `from rembg import
remove`; `remove()` accepts a PIL Image and returns a PIL Image with an
alpha channel), but "matches the documented API" isn't the same as
"produces a clean cutout of this specific kind of image" -- treat your
first successful run as the real test. If edges look rough around the
card's corners, try ALPHA_MATTING = True below (slower, often cleaner
edges).
"""
from __future__ import annotations

from PIL import Image

ALPHA_MATTING = False  # flip to True for slower but often cleaner edges


def remove_background(image: Image.Image) -> Image.Image:
    """Takes a PIL Image (any mode), returns a new PIL Image (RGBA) with
    the background removed -- everything rembg doesn't think is the main
    subject becomes transparent."""
    try:
        from rembg import remove
    except ImportError as e:
        raise ImportError(
            "rembg is not installed. Run:\n"
            "    pip install \"rembg[cpu]\"\n"
            "(first use downloads a ~100-200MB segmentation model, needs network once)"
        ) from e

    kwargs = {}
    if ALPHA_MATTING:
        kwargs.update(
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=10,
        )

    try:
        return remove(image, **kwargs)
    except SystemExit as e:
        # Confirmed with a real failure: when rembg can't find a working
        # onnxruntime backend, it calls sys.exit(1) directly -- not a
        # normal, catchable exception (SystemExit is a BaseException, not
        # an Exception). Left as-is, that kills the ENTIRE build_video.py
        # process instead of letting the caller fall back to another card
        # source. Converting it to a plain RuntimeError here is what
        # makes build_video.py's own try/except (in _cached_bg_removed)
        # actually able to catch it and degrade gracefully.
        raise RuntimeError(
            "rembg couldn't find a working onnxruntime backend. Run:\n"
            "    pip install \"rembg[cpu]\"\n"
            "(the plain `pip install rembg` doesn't always pull in a working backend)"
        ) from e


def remove_background_file(in_path: str, out_path: str) -> None:
    """Loads in_path, removes its background, saves to out_path. out_path
    MUST end in .png -- JPG has no alpha channel and would silently
    throw the transparency away."""
    if not out_path.lower().endswith(".png"):
        raise ValueError(f"out_path must be a .png (got {out_path!r}) -- JPG can't store transparency")
    img = Image.open(in_path)
    result = remove_background(img)
    result.save(out_path)
