"""The composer describes; the painter draws. Neither may change the picture.

## Why this file exists

Splitting `compose` into `describe` + `paint` is the foundation of a second
compositor, and it is also the easiest possible way to change a video by
accident. Every one of the composer's drawing routines moved, and a refactor of
a renderer that changes one pixel is not a refactor.

The check that matters was run against the *previous implementation*: 256
frames covering every layer type and all three transitions, compared
pixel-for-pixel. It found two real regressions that nothing else could have —
during a dissolve, the caption plate and the credit chip were being mixed into
the transition instead of drawn on top, so they faded up over 0.6 seconds
instead of appearing at full opacity. Every existing render test is typography
with captions off, so all of them passed while it was broken.

That comparison cannot live here — it needs the old code. What lives here is
the property it established, expressed against the description itself: the
things a transition must not touch are in a different list from the things it
must.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.render.ffmpeg_renderer import RenderContext
from vtv.contracts.base import ObjectRef, TimeSpan
from vtv.contracts.display import (
    Background,
    Drawn,
    Label,
    Message,
    Picture,
    Plate,
    Mix,
)
from vtv.contracts.errors import Status
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.timeline import (
    AssetClipSource,
    CaptionCue,
    NarrationTrack,
    PlaceholderClipSource,
    ProgrammaticClipSource,
    Timeline,
)
from vtv.contracts.timeline import Transition as ClipTransition
from vtv.contracts.timeline import TransitionKind, VisualClip
from vtv.contracts.visual_language import CameraMotion, TypographySpec

AUDIO = ObjectRef(bucket="b", key="n.wav", content_type="audio/wav")
ASSET = ObjectRef(bucket="b", key="p.jpg", content_type="image/jpeg")
CUES = [
    CaptionCue(
        span=TimeSpan.of(0.0, 8.0),
        text="A caption long enough to wrap onto more than one line in its box.",
    ),
    CaptionCue(span=TimeSpan.of(8.0, 16.0), text="A second caption."),
]


def timeline(*, captions: bool) -> Timeline:
    """One of each: typography, a still with a camera move and a credit, a
    placeholder, and all three transitions."""
    return Timeline(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id="prj_" + "a" * 24,
        scene_graph_id="sgr_" + "a" * 24,
        narration=NarrationTrack(audio=AUDIO, duration_seconds=16.0),
        style=StyleProfile(captions_enabled=captions),
        clips=[
            VisualClip(
                scene_id="scn_" + "a" * 24,
                span=TimeSpan.of(0, 4),
                source=ProgrammaticClipSource(
                    spec=TypographySpec(headline="Opening line")
                ),
            ),
            VisualClip(
                scene_id="scn_" + "b" * 24,
                span=TimeSpan.of(4, 8),
                camera_motion=CameraMotion.KEN_BURNS,
                transition_in=ClipTransition(
                    kind=TransitionKind.DISSOLVE, duration_seconds=0.6
                ),
                source=AssetClipSource(
                    object=ASSET,
                    asset_id="ast_" + "a" * 24,
                    attribution="Photo by A. Photographer, CC-BY 4.0",
                    illustrative_label=True,
                ),
            ),
            VisualClip(
                scene_id="scn_" + "c" * 24,
                span=TimeSpan.of(8, 12),
                transition_in=ClipTransition(
                    kind=TransitionKind.WIPE, duration_seconds=0.6
                ),
                source=PlaceholderClipSource(message="Visual unavailable"),
            ),
            VisualClip(
                scene_id="scn_" + "d" * 24,
                span=TimeSpan.of(12, 16),
                transition_in=ClipTransition(
                    kind=TransitionKind.PUSH, duration_seconds=0.6
                ),
                source=ProgrammaticClipSource(
                    spec=TypographySpec(headline="Closing line")
                ),
            ),
        ],
        captions=CUES if captions else [],
        status=Status.READY,
    )


class DescribingCostsNothing(unittest.TestCase):
    """`describe` is pure: no pixels, no fonts, no filesystem. That is what
    lets a backend ask "can I draw this frame" without drawing it."""

    def context(self, *, captions: bool = True) -> RenderContext:
        from PIL import Image

        self._dir = TemporaryDirectory(prefix="vtv-display-")
        scratch = Path(self._dir.name)
        story = timeline(captions=captions)
        # A real still on disk, because a clip whose picture is missing is
        # described as a message rather than a picture — correctly, and not
        # what these tests are about.
        still = story.clips[1].clip_id
        Image.new("RGB", (900, 600), (180, 90, 40)).save(scratch / f"asset-{still}.bin", "PNG")
        return RenderContext(
            timeline=story,
            settings=RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24),
            scratch=scratch,
            assets={still: {"still": f"asset-{still}.bin"}},
        )

    def tearDown(self) -> None:
        if hasattr(self, "_dir"):
            self._dir.cleanup()

    def test_a_typography_shot_is_one_drawn_layer(self) -> None:
        frame = self.context(captions=False).describe(1.0)
        self.assertEqual(len(frame.layers), 1)
        self.assertIsInstance(frame.layers[0], Drawn)
        self.assertTrue(frame.is_plain, "the fast path was lost")

    def test_a_still_carries_its_camera_move_as_a_box(self) -> None:
        """Ken Burns, a pan and a zoom differ only in which rectangle the
        picture fills. Computing it in `describe` is what makes every backend
        perform the same move rather than each easing it its own way."""
        context = self.context(captions=False)
        early = context.describe(4.1).layers[0]
        late = context.describe(7.9).layers[0]
        self.assertIsInstance(early, Picture)
        self.assertIsInstance(late, Picture)
        self.assertNotEqual(early.box, late.box, "the camera did not move")

    def test_a_placeholder_says_what_it_says(self) -> None:
        layer = self.context(captions=False).describe(10.0).layers[0]
        self.assertIsInstance(layer, Message)
        self.assertEqual(layer.text, "Visual unavailable")

    def test_a_gap_is_the_background(self) -> None:
        context = self.context(captions=False)
        context.timeline.clips[0].span = TimeSpan.of(2, 4)
        context._starts = [c.clip.span.start for c in context.resolved]
        self.assertIsInstance(context.describe(0.5).layers[0], Background)


class ATransitionMixesThePictureAndNothingElse(DescribingCostsNothing):
    """The regression the golden comparison found.

    A caption is on the glass in front of the shot, not part of it. Mixing it
    into a dissolve makes it fade up over 0.6 seconds — wrong, and invisible to
    every test that renders typography with captions off, which is all of them.
    """

    def test_overlays_are_not_in_the_mixed_layers(self) -> None:
        frame = self.context().describe(4.1)
        self.assertIsNotNone(frame.beneath)
        self.assertIs(frame.transition, Mix.BLEND)
        self.assertTrue(frame.overlays, "the caption and credit went missing")
        for layer in frame.layers:
            self.assertNotIsInstance(layer, Plate | Label)

    def test_the_credit_and_the_badge_are_overlays(self) -> None:
        kinds = [type(layer) for layer in self.context().describe(6.0).overlays]
        # Credit plate + credit label, badge plate + badge label, caption
        # plate + at least one caption line.
        self.assertGreaterEqual(kinds.count(Plate), 3)
        self.assertGreaterEqual(kinds.count(Label), 3)

    def test_the_outgoing_picture_is_named_so_it_is_painted_once(self) -> None:
        """It is the same frame for the whole transition. Without a key the
        painter redraws it twelve times in a 0.6-second dissolve at 24fps."""
        frame = self.context().describe(4.1)
        assert frame.beneath is not None
        self.assertTrue(frame.beneath.key)

    def test_each_transition_kind_reaches_the_painter_as_itself(self) -> None:
        context = self.context(captions=False)
        self.assertIs(context.describe(4.1).transition, Mix.BLEND)
        self.assertIs(context.describe(8.1).transition, Mix.WIPE)
        self.assertIs(context.describe(12.1).transition, Mix.PUSH)

    def test_after_a_transition_ends_nothing_is_beneath(self) -> None:
        self.assertIsNone(self.context(captions=False).describe(7.0).beneath)


class PaintingIsTheOnlyThingThatTouchesPixels(DescribingCostsNothing):
    def test_a_described_frame_paints_at_the_right_size(self) -> None:
        context = self.context()
        for t in (1.0, 4.1, 6.0, 8.1, 10.0, 12.1, 15.0):
            with self.subTest(t=t):
                frame = context.compose(t)
                self.assertEqual(frame.size, (context.size.width, context.size.height))
                self.assertEqual(frame.mode, "RGB")

    def test_composing_the_same_moment_twice_gives_the_same_picture(self) -> None:
        """Purity, checked. A composer with hidden state between calls would
        make a resumed render differ from a whole one."""
        from PIL import ImageChops

        context = self.context()
        for t in (4.2, 8.2, 12.2):
            with self.subTest(t=t):
                one, two = context.compose(t), context.compose(t)
                diff = ImageChops.difference(one, two)
                self.assertEqual(max(band[1] for band in diff.getextrema()), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
