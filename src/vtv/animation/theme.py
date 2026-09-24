"""Typography, colour and easing for the animation engine.

One theme object is built from the project's `StyleProfile` and passed to every
primitive. That is what makes separately-drawn shots look like one film: the same
type scale, the same palette, the same motion curves, decided once.

Fonts are resolved from what is actually installed, with a documented fallback
chain, because a renderer that silently substitutes a different typeface produces
a video that looks wrong in a way nobody can explain.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from vtv.animation import fonts as font_registry
from vtv.contracts.language import Language, detect_script
from vtv.contracts.style import ColorPalette, MotionIntensity, StyleProfile, VisualStyle

#: Preference order per weight. First existing file wins.
_FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "bold": (
        "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ),
    "medium": (
        "/usr/share/fonts/truetype/google-fonts/Poppins-Medium.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ),
    "regular": (
        "/usr/share/fonts/truetype/google-fonts/Poppins-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ),
    "light": (
        "/usr/share/fonts/truetype/google-fonts/Poppins-Light.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
    "mono": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ),
}

RGBA = tuple[int, int, int, int]


def resolve_font_file(weight: str) -> str | None:
    for candidate in _FONT_CANDIDATES.get(weight, ()):
        if Path(candidate).exists():
            return candidate
    return None


@lru_cache(maxsize=256)
def load_font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a font, cached.

    If no TrueType face is installed the layout code still needs metrics, so
    PIL's built-in face is used. It is ugly, and a deployment that hits this
    path has a packaging problem rather than a rendering one.
    """
    path = resolve_font_file(weight)
    if path is None:  # pragma: no cover - only on a system with no fonts at all
        return ImageFont.load_default_imagefont()  # type: ignore[return-value]
    return ImageFont.truetype(path, size)


def hex_to_rgba(value: str, alpha: int = 255) -> RGBA:
    value = value.lstrip("#")
    return (
        int(value[0:2], 16),
        int(value[2:4], 16),
        int(value[4:6], 16),
        max(0, min(255, alpha)),
    )


def mix(first: RGBA, second: RGBA, amount: float) -> RGBA:
    """Blend two colours. ``amount`` of 0 gives the first, 1 the second."""
    amount = max(0.0, min(1.0, amount))
    return (
        int(first[0] + (second[0] - first[0]) * amount),
        int(first[1] + (second[1] - first[1]) * amount),
        int(first[2] + (second[2] - first[2]) * amount),
        int(first[3] + (second[3] - first[3]) * amount),
    )


def with_alpha(colour: RGBA, alpha: float) -> RGBA:
    return (colour[0], colour[1], colour[2], max(0, min(255, int(255 * alpha))))


def relative_luminance(colour: RGBA) -> float:
    """WCAG relative luminance, used to keep text legible on any background."""

    def channel(value: int) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.04045 else ((srgb + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(colour[0])
        + 0.7152 * channel(colour[1])
        + 0.0722 * channel(colour[2])
    )


def contrast_ratio(first: RGBA, second: RGBA) -> float:
    a, b = relative_luminance(first), relative_luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------

def ease_out_cubic(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return 3 * t * t - 2 * t * t * t


def ease_out_back(t: float) -> float:
    """Slight overshoot. Used sparingly — it draws attention, which is the point,
    and therefore stops working if everything uses it."""
    t = max(0.0, min(1.0, t))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def stagger(index: int, count: int, progress: float, overlap: float = 0.6) -> float:
    """Per-item progress for a staggered reveal.

    ``overlap`` controls how much items overlap: 0 is strictly sequential, 1 is
    simultaneous. Sequential reveals read as a list; overlapping ones read as a
    single gesture, which is usually what a scene wants.
    """
    if count <= 1:
        return max(0.0, min(1.0, progress))
    window = 1.0 / (count - (count - 1) * overlap)
    start = index * window * (1 - overlap)
    local = (progress - start) / max(window, 1e-6)
    return max(0.0, min(1.0, local))


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Theme:
    """Everything a primitive needs to look like it belongs to this project."""

    width: int
    height: int
    background: RGBA
    foreground: RGBA
    accent: RGBA
    secondary: RGBA
    muted: RGBA
    style: VisualStyle
    motion: MotionIntensity
    #: Multiplier applied to every animation duration.
    tempo: float
    serif: bool
    #: The language on-screen text is drawn in. Decides the default face and
    #: whether layout runs right to left.
    language: Language = field(default_factory=Language)
    #: Entity name (lower-cased) to colour, from the project's Visual Bible.
    #: Empty means "no locks", which is the whole of the previous behaviour.
    entity_colours: dict[str, RGBA] = field(default_factory=dict)

    # -- layout -----------------------------------------------------------

    @property
    def margin(self) -> int:
        """Generous by default. Crowded frames are the most common failure of
        automatically generated video."""
        return int(min(self.width, self.height) * 0.085)

    @property
    def safe_width(self) -> int:
        return self.width - 2 * self.margin

    @property
    def safe_height(self) -> int:
        return self.height - 2 * self.margin

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width

    def scale(self, fraction: float) -> int:
        """A size relative to the frame, so every layout is resolution-independent."""
        reference = self.height if not self.is_portrait else self.width
        return max(1, int(reference * fraction))

    # -- type scale -------------------------------------------------------

    def font(self, role: str, text: str | None = None) -> ImageFont.FreeTypeFont:
        """A face for a role, chosen for the script the text is actually in.

        Passing the text matters: a project may be Latin overall and still have
        one scene quoting Devanagari. Selecting by project language would draw
        that scene as boxes.
        """
        script = detect_script(text) if text else self.language.script
        sizes = {
            "display": self.scale(0.115),
            "headline": self.scale(0.075),
            "title": self.scale(0.052),
            "body": self.scale(0.036),
            "label": self.scale(0.028),
            "caption": self.scale(0.024),
            "micro": self.scale(0.020),
        }
        weights = {
            "display": "bold",
            "headline": "bold",
            "title": "medium",
            "body": "regular",
            "label": "medium",
            "caption": "regular",
            "micro": "regular",
        }
        size = sizes.get(role, self.scale(0.03))
        # CJK needs a little more room at the same nominal size to stay legible.
        if script.is_cjk:
            size = int(size * 0.92)
        return font_registry.load(
            script, size, bold=weights.get(role, "regular") == "bold"
        )

    @property
    def is_rtl(self) -> bool:
        return self.language.is_rtl

    # -- colour -----------------------------------------------------------

    def colour_for(self, name: str | None, fallback: RGBA) -> RGBA:
        """The locked colour for this entity, or the caller's own choice.

        Matching is on the lower-cased name, and a miss is not an error: most
        series and nodes are not entities, and a chart labelled "Revenue" should
        keep its categorical colour rather than being forced to look like
        something it is not.
        """
        if not name or not self.entity_colours:
            return fallback
        return self.entity_colours.get(name.strip().lower(), fallback)

    def series_colour(self, index: int) -> RGBA:
        """Categorical colours, in a fixed order so a chart is stable across renders."""
        wheel = [self.accent, self.secondary, self.foreground, self.muted]
        base = wheel[index % len(wheel)]
        if index >= len(wheel):
            # Wrap by lightening rather than repeating, so an eight-series chart
            # is still readable.
            return mix(base, self.background, 0.35)
        return base

    def legible_on(self, background: RGBA) -> RGBA:
        """Foreground or background, whichever the eye can actually read."""
        if contrast_ratio(self.foreground, background) >= contrast_ratio(
            self.background, background
        ):
            return self.foreground
        return self.background

    @classmethod
    def from_style(
        cls,
        style: StyleProfile,
        *,
        width: int,
        height: int,
        language: Language | None = None,
        entity_colours: dict[str, str] | None = None,
    ) -> Theme:
        palette: ColorPalette = style.palette
        tempo = {
            MotionIntensity.CALM: 1.35,
            MotionIntensity.MODERATE: 1.0,
            MotionIntensity.ENERGETIC: 0.75,
        }[style.motion]
        return cls(
            width=width,
            height=height,
            background=hex_to_rgba(palette.background),
            foreground=hex_to_rgba(palette.foreground),
            accent=hex_to_rgba(palette.accent),
            secondary=hex_to_rgba(palette.secondary),
            muted=hex_to_rgba(palette.muted),
            style=style.style,
            motion=style.motion,
            tempo=tempo,
            serif=style.style in {VisualStyle.EDITORIAL, VisualStyle.DOCUMENTARY},
            language=language or Language(),
            entity_colours={
                name.strip().lower(): hex_to_rgba(value)
                for name, value in (entity_colours or {}).items()
            },
        )


def golden_angle_positions(count: int, radius: float) -> list[tuple[float, float]]:
    """Evenly-spread points on a circle, used for force-ish network layouts."""
    if count <= 0:
        return []
    return [
        (
            radius * math.cos(2 * math.pi * index / count - math.pi / 2),
            radius * math.sin(2 * math.pi * index / count - math.pi / 2),
        )
        for index in range(count)
    ]


__all__ = [
    "RGBA",
    "Theme",
    "contrast_ratio",
    "ease_in_out",
    "ease_out_back",
    "ease_out_cubic",
    "golden_angle_positions",
    "hex_to_rgba",
    "load_font",
    "mix",
    "relative_luminance",
    "resolve_font_file",
    "stagger",
    "with_alpha",
]
