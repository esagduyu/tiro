"""Generate PWA icons (M3.1 Task 1) into tiro/frontend/static/icons/.

Regenerates the Tironian-et brand glyph (the same "⁊ on terra cotta"
mark used for the sidebar logo, favicon, and Chrome extension icons since
the Checkpoint-22 UX redesign) at the sizes manifest.webmanifest needs:
192x192 and 512x512. This is a from-scratch re-render rather than an
upscale of logo-128.png -- upscaling a 128px raster to 512px would look
soft, whereas re-rendering vector glyph + shapes at the target size stays
crisp at every resolution.

The original 128px asset was never checked in as a script (it was made
ad hoc, per CLAUDE.md's UX-redesign notes), so this reconstructs the same
recipe by inspection of tiro/frontend/static/logo-128.png: a rounded-square
terra cotta (#C45B3E, --tiro-accent) tile with the Tironian et character
(U+204A) drawn in white using the system "Apple Symbols" font, sized and
positioned at the same proportions the 128px original used (corner radius
~16.4% of the tile, font size 62.5% of the tile, glyph drawn from an origin
~15.6% in from the top-left corner) so the new sizes read as the same mark,
just sharper.

Quality caveat: this depends on "Apple Symbols.ttf" being present (true on
macOS; not guaranteed on Linux CI or other dev machines). If the font can't
be loaded, this script falls back to a high-quality LANCZOS upscale of the
existing logo-128.png -- which WILL look softer at 512px than a from-scratch
render, especially at close zoom (e.g. an iOS home-screen icon). Re-run this
script on a macOS machine to regenerate crisp assets if that fallback path
was used; the printed output says which path ran.

Usage: uv run python scripts/make_icons.py
(Pillow is a dev-only dependency -- see pyproject.toml's dev group -- since
nothing at runtime touches this script; it's a one-off asset generator like
the branding work described in CLAUDE.md's Checkpoint 15/22 notes.)
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "tiro" / "frontend" / "static"
ICONS_DIR = STATIC_DIR / "icons"
LOGO_128 = STATIC_DIR / "logo-128.png"

TERRA_COTTA = (196, 91, 62, 255)  # --tiro-accent, papyrus.css
GLYPH_WHITE = (255, 255, 255, 255)
TIRONIAN_ET = "⁊"  # TIRONIAN SIGN ET, matches the existing brand mark

# Proportions measured off the existing 128px asset (see module docstring).
CORNER_RADIUS_RATIO = 21 / 128
FONT_SIZE_RATIO = 80 / 128
GLYPH_ORIGIN_RATIO = 20 / 128

APPLE_SYMBOLS_FONT = "/System/Library/Fonts/Apple Symbols.ttf"

SIZES = (192, 512)


def _render_from_scratch(size: int) -> Image.Image:
    """Draw the rounded-square + Tironian-et mark natively at `size` px."""
    font = ImageFont.truetype(APPLE_SYMBOLS_FONT, round(size * FONT_SIZE_RATIO))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = round(size * CORNER_RADIUS_RATIO)
    draw.rounded_rectangle([(0, 0), (size - 1, size - 1)], radius=radius, fill=TERRA_COTTA)
    origin = round(size * GLYPH_ORIGIN_RATIO)
    draw.text((origin, origin), TIRONIAN_ET, font=font, fill=GLYPH_WHITE)
    return img


def _render_upscaled_fallback(size: int) -> Image.Image:
    """Fallback when Apple Symbols isn't available: upscale logo-128.png."""
    base = Image.open(LOGO_128).convert("RGBA")
    return base.resize((size, size), Image.LANCZOS)


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        ImageFont.truetype(APPLE_SYMBOLS_FONT, 10)
        renderer = _render_from_scratch
        print(f"Rendering from scratch using {APPLE_SYMBOLS_FONT}")
    except OSError:
        renderer = _render_upscaled_fallback
        print(
            "WARNING: Apple Symbols font not found -- falling back to a "
            f"LANCZOS upscale of {LOGO_128.name}. This will look softer "
            "than a native render, especially at 512px. Re-run on macOS "
            "for crisp icons."
        )

    for size in SIZES:
        img = renderer(size)
        out_path = ICONS_DIR / f"tiro-{size}.png"
        img.save(out_path)
        print(f"Wrote {out_path.relative_to(REPO_ROOT)} ({size}x{size})")


if __name__ == "__main__":
    main()
