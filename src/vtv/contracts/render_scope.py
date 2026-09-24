"""Render scope — rendering the part that changed, not the whole video.

The economics of this product turn on one number: what it costs a user to
dislike one visual. If the answer is "re-render everything", then trying a
second idea for shot seven costs the price of the whole video, and users stop
trying second ideas. The product becomes take-it-or-leave-it, which is not what
anyone wants from an editor.

So a render has a **scope**:

| Scope | Renders | Reached by |
| --- | --- | --- |
| `CLIP` | one clip | regenerating one visual |
| `SCENE` | the clips of one visual unit | a unit with several clips |
| `RANGE` | an explicit window | scrubbing a section |
| `FULL_PROJECT` | everything | export, and the first render |

## The invariant

> **A scoped render must produce exactly what a full render would have produced
> for that region.**

That is not free. A cross-fade spans two clips, so rendering "clip 7" alone
would lose the fade into it and the fade out of it. `RenderRegion.expand()`
widens the requested window to include the transitions that touch it, which is
why the region a scoped render actually covers is usually slightly larger than
the clip that triggered it.

Get that wrong and the seam is visible — a frame of black, a jump in the
cross-fade — and the user reports "the video is broken" rather than "the render
scope is off by 400 milliseconds".

## Concatenation, not compositing

A scoped render produces a segment; the final video is the previous segments
plus the new one, concatenated. Concatenation is exact when the segments share
codec parameters, which is why `RenderSettings` is carried on the scope and a
scoped render with different settings is refused rather than silently
transcoded.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from vtv.contracts.base import Id, Seconds, VTVModel


class RenderScope(str, Enum):
    """How much of the project this render covers."""

    CLIP = "clip"
    SCENE = "scene"
    RANGE = "range"
    FULL_PROJECT = "full_project"


#: Extra seconds included either side of a scoped region so that a transition
#: spanning the boundary is rendered whole. Larger than the longest transition
#: any pacing profile permits, because a region that clips a fade produces a
#: visible seam and the cost of being generous is a fraction of a second of
#: extra rendering.
BOUNDARY_PADDING_SECONDS = 1.0


class RenderRegion(VTVModel):
    """The window a scoped render actually covers."""

    scope: RenderScope = RenderScope.FULL_PROJECT
    start: Seconds = Field(default=0.0, ge=0.0)
    #: `None` means "to the end". Only valid for `FULL_PROJECT`.
    end: Seconds | None = Field(default=None, ge=0.0)

    #: What triggered this. Carried for the audit trail and so a worker can
    #: report progress against something the user recognises.
    clip_ids: list[Id] = Field(default_factory=list, max_length=200)
    visual_unit_ids: list[Id] = Field(default_factory=list, max_length=200)

    @property
    def is_full(self) -> bool:
        return self.scope is RenderScope.FULL_PROJECT

    @property
    def duration(self) -> Seconds | None:
        return None if self.end is None else round(self.end - self.start, 3)

    def expand(self, *, limit: Seconds, padding: Seconds | None = None) -> RenderRegion:
        """Widen to include the transitions that touch this window.

        A cross-fade spans a boundary. Rendering a region that cuts one in half
        produces a seam the user sees as a broken video, so the region grows by
        `BOUNDARY_PADDING_SECONDS` at each end and is then clamped to the
        project.
        """
        if self.is_full:
            return self
        pad = BOUNDARY_PADDING_SECONDS if padding is None else padding
        start = max(0.0, round(self.start - pad, 3))
        end = self.end if self.end is not None else limit
        return self.model_copy(
            update={
                "start": start,
                "end": min(round(limit, 3), round(end + pad, 3)),
            }
        )

    def covers(self, seconds: Seconds) -> bool:
        if self.is_full:
            return True
        return self.start <= seconds < (self.end if self.end is not None else seconds + 1)

    @model_validator(mode="after")
    def _window_is_coherent(self) -> RenderRegion:
        if self.end is not None and self.end <= self.start:
            raise ValueError("a render region must advance")
        if not self.is_full and self.end is None:
            raise ValueError("only a full-project render may omit an end")
        if self.scope is RenderScope.CLIP and not self.clip_ids:
            raise ValueError("a clip-scoped render must name its clip")
        if self.scope is RenderScope.SCENE and not self.visual_unit_ids:
            raise ValueError("a scene-scoped render must name its visual unit")
        return self


def full_project() -> RenderRegion:
    return RenderRegion(scope=RenderScope.FULL_PROJECT)


__all__ = [
    "BOUNDARY_PADDING_SECONDS",
    "RenderRegion",
    "RenderScope",
    "full_project",
]
