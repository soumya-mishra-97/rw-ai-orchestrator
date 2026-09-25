"""Report fonts: Times New Roman when it is installed, the PDF built-in Times otherwise.

The built-in fonts only cover Windows-1252, so every string passes through
:meth:`FontSet.clean`, which swaps characters the chosen fonts cannot draw
(arrows, ≥, check marks) for readable ASCII instead of empty boxes.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from reportlab.lib.fonts import addMapping
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# (directory, regular, bold, italic, bold-italic) — first complete family wins.
_SERIF_FAMILIES = (
    (
        "/System/Library/Fonts/Supplemental",
        "Times New Roman.ttf",
        "Times New Roman Bold.ttf",
        "Times New Roman Italic.ttf",
        "Times New Roman Bold Italic.ttf",
    ),
    (
        "/usr/share/fonts/truetype/msttcorefonts",
        "Times_New_Roman.ttf",
        "Times_New_Roman_Bold.ttf",
        "Times_New_Roman_Italic.ttf",
        "Times_New_Roman_Bold_Italic.ttf",
    ),
    ("C:/Windows/Fonts", "times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    (
        "/usr/share/fonts/truetype/liberation",
        "LiberationSerif-Regular.ttf",
        "LiberationSerif-Bold.ttf",
        "LiberationSerif-Italic.ttf",
        "LiberationSerif-BoldItalic.ttf",
    ),
)
_MONO_FAMILIES = (
    ("/System/Library/Fonts/Supplemental", "Courier New.ttf", "Courier New Bold.ttf"),
    ("/usr/share/fonts/truetype/msttcorefonts", "Courier_New.ttf", "Courier_New_Bold.ttf"),
    ("C:/Windows/Fonts", "cour.ttf", "courbd.ttf"),
    (
        "/usr/share/fonts/truetype/liberation",
        "LiberationMono-Regular.ttf",
        "LiberationMono-Bold.ttf",
    ),
)
_FALLBACKS = {
    "→": "->",
    "←": "<-",
    "⇄": "<->",
    "≥": ">=",
    "≤": "<=",
    "≠": "!=",
    "−": "-",
    "✓": "OK",
    "✔": "OK",
    "✗": "x",
    "✘": "x",
    "•": "-",
    "\u00a0": " ",
}


@dataclass(frozen=True)
class FontSet:
    serif: str
    serif_bold: str
    serif_italic: str
    mono: str
    mono_bold: str
    glyphs: frozenset[int] | None  # None: built-in fonts, limited to Windows-1252

    def clean(self, text: str) -> str:
        return "".join(ch if self._drawable(ch) else _FALLBACKS.get(ch, "?") for ch in text)

    def _drawable(self, ch: str) -> bool:
        if ch in "\n\t":
            return True
        if self.glyphs is not None:
            return ord(ch) in self.glyphs
        try:
            ch.encode("cp1252")
        except UnicodeEncodeError:
            return False
        return True


def _family(candidates: tuple[tuple[str, ...], ...]) -> list[Path] | None:
    for directory, *names in candidates:
        paths = [Path(directory) / name for name in names]
        if all(p.is_file() for p in paths):
            return paths
    return None


def _register(name: str, path: Path) -> set[int]:
    font = TTFont(name, str(path))
    pdfmetrics.registerFont(font)
    return set(font.face.charToGlyph)


BUILTIN_FONTS = FontSet(
    "Times-Roman", "Times-Bold", "Times-Italic", "Courier", "Courier-Bold", None
)


@cache
def report_fonts() -> FontSet:
    """Register the report fonts once per process and describe what they can draw."""
    serif, mono = _family(_SERIF_FAMILIES), _family(_MONO_FAMILIES)
    if serif is None or mono is None:
        return BUILTIN_FONTS
    try:
        names = ("RW-Serif", "RW-Serif-Bold", "RW-Serif-Italic", "RW-Serif-BoldItalic")
        coverage = [_register(n, p) for n, p in zip(names, serif, strict=True)]
        coverage += [_register("RW-Mono", mono[0]), _register("RW-Mono-Bold", mono[1])]
    except Exception:  # noqa: BLE001 — an unreadable system font must not break the report
        return BUILTIN_FONTS
    addMapping("RW-Serif", 0, 0, names[0])
    addMapping("RW-Serif", 1, 0, names[1])
    addMapping("RW-Serif", 0, 1, names[2])
    addMapping("RW-Serif", 1, 1, names[3])
    addMapping("RW-Mono", 0, 0, "RW-Mono")
    addMapping("RW-Mono", 1, 0, "RW-Mono-Bold")
    return FontSet(
        "RW-Serif",
        "RW-Serif-Bold",
        "RW-Serif-Italic",
        "RW-Mono",
        "RW-Mono-Bold",
        frozenset(set.intersection(*coverage)),
    )
