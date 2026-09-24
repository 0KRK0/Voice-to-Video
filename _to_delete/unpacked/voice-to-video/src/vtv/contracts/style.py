"""Project-level visual style.

Style is decided once, at the project level, and then *constrains* every scene.
This is the difference between a video and a pile of clips: the same typeface,
the same palette, the same motion vocabulary, the same colour grade on generated
images. Consistency is enforced by passing this profile into every visual
decision rather than hoping each independent generation lands in the same place.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from vtv.contracts.base import VTVModel


class VisualStyle(str, Enum):
    """The register of the finished video."""

    DOCUMENTARY = "documentary"
    EXPLAINER = "explainer"
    EDITORIAL = "editorial"
    TECHNICAL = "technical"
    CINEMATIC = "cinematic"
    MINIMAL = "minimal"
    PLAYFUL = "playful"


class MotionIntensity(str, Enum):
    CALM = "calm"
    MODERATE = "moderate"
    ENERGETIC = "energetic"


class AspectRatio(str, Enum):
    """Delivery shape. Chosen up front because it changes composition, not just
    cropping: a portrait explainer wants stacked layouts, not letterboxed ones."""

    LANDSCAPE_16_9 = "16:9"
    PORTRAIT_9_16 = "9:16"
    SQUARE_1_1 = "1:1"

    @property
    def dimensions_1080(self) -> tuple[int, int]:
        return {
            AspectRatio.LANDSCAPE_16_9: (1920, 1080),
            AspectRatio.PORTRAIT_9_16: (1080, 1920),
            AspectRatio.SQUARE_1_1: (1080, 1080),
        }[self]


class ColorPalette(VTVModel):
    """A small, deliberate palette. Hex strings, validated."""

    background: str = Field(default="#0B0D10", pattern=r"^#[0-9a-fA-F]{6}$")
    foreground: str = Field(default="#F5F7FA", pattern=r"^#[0-9a-fA-F]{6}$")
    accent: str = Field(default="#4C8DFF", pattern=r"^#[0-9a-fA-F]{6}$")
    secondary: str = Field(default="#FFB65C", pattern=r"^#[0-9a-fA-F]{6}$")
    muted: str = Field(default="#7A8598", pattern=r"^#[0-9a-fA-F]{6}$")


class StyleProfile(VTVModel):
    """The style contract every scene must honour."""

    style: VisualStyle = VisualStyle.EXPLAINER
    palette: ColorPalette = Field(default_factory=ColorPalette)
    motion: MotionIntensity = MotionIntensity.MODERATE
    aspect_ratio: AspectRatio = AspectRatio.LANDSCAPE_16_9
    #: A generic family class, not a licensed font name. The renderer maps this
    #: to a font we actually have the right to embed.
    typeface: str = Field(default="grotesque", max_length=32)
    captions_enabled: bool = True
    #: Free-text direction from the user, e.g. "warm, hand-drawn, no stock
    #: photos". Passed to the Visual Director as a soft constraint.
    direction: str | None = Field(default=None, max_length=500)

    def as_prompt_fragment(self) -> str:
        """Style guidance appended to every generation prompt, so that separately
        generated images still look like they belong to one film."""
        parts = [f"{self.style.value} style", f"{self.motion.value} motion"]
        if self.direction:
            parts.append(self.direction)
        return ", ".join(parts)


__all__ = [
    "AspectRatio",
    "ColorPalette",
    "MotionIntensity",
    "StyleProfile",
    "VisualStyle",
]
