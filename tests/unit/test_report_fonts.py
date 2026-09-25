"""The PDF report never draws a character its font lacks."""

from __future__ import annotations

from sdlc_loop.reports.fonts import BUILTIN_FONTS, FontSet


def test_builtin_fonts_fall_back_to_ascii() -> None:
    assert BUILTIN_FONTS.clean("PM→BA · score ≥ 4.0 ✓") == "PM->BA · score >= 4.0 OK"
    assert BUILTIN_FONTS.clean("naïve café\nnext") == "naïve café\nnext"  # Windows-1252 is kept
    assert BUILTIN_FONTS.clean("中") == "?"


def test_embedded_fonts_keep_covered_glyphs() -> None:
    fonts = FontSet("s", "sb", "si", "m", "mb", frozenset(map(ord, "PM→BA")))
    assert fonts.clean("PM→BA≥") == "PM→BA>="
