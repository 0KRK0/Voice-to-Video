"""Segmented rendering: the split, the checkpoint, and the resume.

The claim being tested is narrow and load-bearing: **a render that dies partway
through does not redo the encoding it already did, and produces the same video
as one that never died.** Everything here is either an arithmetic property of
the split that a long video would otherwise lose a frame to, or a statement
about what happens when a process disappears.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from asyncio import run
from pathlib import Path
from tempfile import TemporaryDirectory

from vtv.adapters.media import ffmpeg
from vtv.adapters.render import segments as seg
from vtv.adapters.render.ffmpeg_renderer import FfmpegRenderer
from vtv.adapters.storage.local import LocalStorageProvider
from vtv.contracts.base import ObjectRef, TimeSpan
from vtv.contracts.errors import Status, VTVError
from vtv.contracts.render import RenderQuality, RenderSettings
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID
from vtv.contracts.timeline import (
    AssetClipSource,
    NarrationTrack,
    ProgrammaticClipSource,
    Timeline,
    VisualClip,
)
from vtv.contracts.visual_language import TypographySpec
from vtv.observability.events import EventSink

PROJECT = "prj_" + "a" * 24
SCENE_GRAPH = "sgr_" + "a" * 24

#: Long enough to split into several segments at `SEGMENT_SECONDS`, short
#: enough that the suite still finishes. The point of the number is that it is
#: more than one segment; anything else about it is arbitrary.
DURATION = 12.0
SHOT = 2.0
SEGMENT_SECONDS = 3.0


def spans(count: int, each: float) -> list[float]:
    return [index * each for index in range(count)]


def plain_timeline(audio: ObjectRef, *, duration: float = DURATION) -> Timeline:
    """Typography only: no assets to download, so a test needs no fixtures and
    every frame is drawn rather than fetched."""
    shots = int(duration / SHOT)
    return Timeline(
        organisation_id=SYSTEM_ORGANISATION_ID,
        project_id=PROJECT,
        scene_graph_id=SCENE_GRAPH,
        narration=NarrationTrack(audio=audio, duration_seconds=duration),
        # No cues, so captions must be off: `is_renderable` refuses a
        # timeline that promises captions and has none, which is correct
        # and not what these tests are about.
        style=StyleProfile(captions_enabled=False),
        clips=[
            VisualClip(
                scene_id="scn_" + chr(ord("a") + index) * 24,
                span=TimeSpan.of(index * SHOT, (index + 1) * SHOT),
                source=ProgrammaticClipSource(
                    spec=TypographySpec(headline=f"Shot {index}")
                ),
            )
            for index in range(shots)
        ],
        captions=[],
        status=Status.READY,
    )


def illustrated_timeline(storage: LocalStorageProvider, root: Path) -> Timeline:
    """Typography *and* a photograph, the picture actually stored.

    Needed because segments are now routed by what is in them: a video of
    nothing but title cards is correctly sent to the processor for every
    segment, so a fallback test built on `plain_timeline` stopped exercising the
    graphics card at all — and kept passing, because two processor renders match
    each other perfectly.

    That is the shape of a test that has quietly stopped testing anything, which
    is why the fallback tests now assert the card drew something before they
    compare the pictures.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (640, 420), (200, 120, 40)).save(buffer, "PNG")
    picture = run(
        storage.put(
            key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/p/picture.png",
            data=buffer.getvalue(),
            content_type="image/png",
        )
    )
    timeline = stored_timeline(storage, root)
    # Alternating, which is what real material looks like and what matters here:
    # every segment then contains a photograph somewhere in it, so every segment
    # routes to the card and a card that fails on one of them actually has
    # something to fail at. Photographs only in the last shot would leave the
    # segment the fixture breaks entirely typographic, and the test would go
    # back to proving nothing.
    clips = [
        clip
        if index % 2 == 0
        else VisualClip(
            scene_id="scn_" + chr(ord("m") + index) * 24,
            span=clip.span,
            source=AssetClipSource(
                asset_id="ast_" + chr(ord("m") + index) * 24, object=picture
            ),
        )
        for index, clip in enumerate(timeline.clips)
    ]
    return timeline.model_copy(update={"clips": clips})


def stored_timeline(storage: LocalStorageProvider, root: Path) -> Timeline:
    audio_path = root / "narration.wav"
    ffmpeg.synthesise_tone_audio(
        audio_path, duration=DURATION, segments=[(0.3, 2.5), (3.0, 9.0)]
    )
    audio = run(
        storage.put(
            key=f"orgs/{SYSTEM_ORGANISATION_ID}/projects/p/narration.wav",
            data=audio_path.read_bytes(),
            content_type="audio/wav",
        )
    )
    return plain_timeline(audio)


def picture_of(path: Path) -> str:
    """A hash of the decoded frames, not of the file.

    Comparing bytes would compare the muxer's creation timestamp too, so two
    identical videos made a second apart would differ. `framemd5` hashes what
    is actually on screen, which is the only thing the claim is about.
    """
    result = subprocess.run(
        [ffmpeg.FFMPEG, "-v", "error", "-i", str(path), "-an", "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return "".join(
        line for line in result.stdout.splitlines() if not line.startswith("#")
    )


def probe(path: Path) -> dict:
    return json.loads(
        subprocess.run(
            [
                ffmpeg.FFPROBE, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, text=True, check=True,
        ).stdout
    )


class TheSplitCoversEveryFrameExactlyOnce(unittest.TestCase):
    """A gap here is a black frame in the finished video; an overlap is a
    stutter. Neither is visible in a ten-second test and both accumulate."""

    def partition(self, segments: list[seg.Segment], total: int) -> None:
        self.assertEqual(segments[0].start_frame, 0)
        self.assertEqual(segments[-1].end_frame, total)
        for before, after in zip(segments, segments[1:], strict=False):
            self.assertEqual(before.end_frame, after.start_frame)
        self.assertEqual(sum(item.frames for item in segments), total)

    def test_a_short_video_is_one_segment(self) -> None:
        segments = seg.plan(spans(3, 2.0), fps=30, total_frames=180)
        self.assertEqual(len(segments), 1)
        self.partition(segments, 180)

    def test_a_long_video_is_many(self) -> None:
        # Forty minutes: 736 clips of about 3.3 seconds, at 30fps.
        segments = seg.plan(spans(736, 3.26), fps=30, total_frames=71_970)
        self.assertGreater(len(segments), 100)
        self.partition(segments, 71_970)

    def test_no_segment_is_left_below_target_except_the_last(self) -> None:
        segments = seg.plan(spans(40, 1.0), fps=30, total_frames=1200)
        for item in segments[:-1]:
            self.assertGreaterEqual(item.frames, seg.TARGET_SECONDS * 30)

    def test_boundaries_land_on_clip_starts(self) -> None:
        """Never mid-shot: concat wants segments an encoder produced whole, and
        a clip boundary is where the picture was changing anyway."""
        starts = spans(60, 4.0)
        segments = seg.plan(starts, fps=30, total_frames=7200)
        allowed = {round(value * 30) for value in starts} | {7200}
        for item in segments:
            self.assertIn(item.start_frame, allowed)
            self.assertIn(item.end_frame, allowed)

    def test_frames_are_counted_not_durations(self) -> None:
        """Deriving each segment's length from its own duration would round
        independently, and a two-hundred-segment video would drift seconds away
        from its audio. The partition assertion above is the real test; this
        pins the case where the duration is not a whole number of frames."""
        segments = seg.plan(spans(200, 3.333), fps=30, total_frames=19_998)
        self.partition(segments, 19_998)

    def test_a_video_with_no_clips_still_renders(self) -> None:
        segments = seg.plan([], fps=30, total_frames=300)
        self.assertEqual(len(segments), 1)
        self.partition(segments, 300)

    def test_nothing_to_render_is_no_segments(self) -> None:
        self.assertEqual(seg.plan([1.0], fps=30, total_frames=0), [])


class ASegmentOnDiskIsFinishedByDefinition(unittest.TestCase):
    """The checkpoint is the filename. Not a counter, not a database row —
    which is why resumption is the ordinary path rather than recovery code that
    only runs during the incident it was written for."""

    def test_a_missing_segment_is_not_done(self) -> None:
        with TemporaryDirectory() as tmp:
            self.assertFalse(seg.Segment(0, 0, 30).done(Path(tmp)))

    def test_a_part_file_does_not_count_as_done(self) -> None:
        """This is the whole guarantee: a process killed mid-encode leaves the
        partial name, so the next attempt cannot mistake half a segment for a
        whole one and ship a video with a torn shot in it."""
        with TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            item = seg.Segment(7, 0, 30)
            item.partial(scratch).write_bytes(b"half a segment")
            self.assertFalse(item.done(scratch))

    def test_an_empty_file_does_not_count_as_done(self) -> None:
        with TemporaryDirectory() as tmp:
            scratch = Path(tmp)
            item = seg.Segment(7, 0, 30)
            item.path(scratch).write_bytes(b"")
            self.assertFalse(item.done(scratch))

    def test_names_sort_in_render_order(self) -> None:
        """The concat list is built from the plan, but a human debugging a
        render reads the directory, and `seg-9` before `seg-10` would send them
        looking for a bug that is not there."""
        names = [seg.Segment(index, 0, 1).name for index in (0, 9, 10, 1000)]
        self.assertEqual(names, sorted(names))


class TheScratchDirectoryIsNamedByWhatIsBeingRendered(unittest.TestCase):
    """Named by the render job id, a retry would never find the dead attempt's
    segments — which is how the old renderer managed to be durable everywhere
    except where it mattered."""

    def setUp(self) -> None:
        self.timeline = plain_timeline(
            ObjectRef(bucket="b", key="narration.wav", content_type="audio/wav")
        )

    def settings(self, **kw: object) -> RenderSettings:
        return RenderSettings(**kw)  # type: ignore[arg-type]

    def test_the_same_video_twice_is_the_same_fingerprint(self) -> None:
        first = seg.fingerprint(self.timeline, self.settings())
        second = seg.fingerprint(self.timeline, self.settings())
        self.assertEqual(first, second)

    def test_different_settings_are_a_different_fingerprint(self) -> None:
        from vtv.contracts.render import RenderQuality

        self.assertNotEqual(
            seg.fingerprint(self.timeline, self.settings()),
            seg.fingerprint(self.timeline, self.settings(quality=RenderQuality.HIGH)),
        )

    def test_an_edited_timeline_shares_nothing(self) -> None:
        """Reusing a segment whose clip has changed ships the wrong picture and
        the user has no way to know. Re-rendering something that did not need it
        costs time. The fingerprint is deliberately biased towards the second."""
        edited = self.timeline.model_copy(deep=True)
        edited.clips[0].camera_motion = _other_motion(edited.clips[0].camera_motion)
        self.assertNotEqual(
            seg.fingerprint(self.timeline, self.settings()),
            seg.fingerprint(edited, self.settings()),
        )

    def test_a_timestamp_is_not_part_of_the_video(self) -> None:
        """`updated_at` changes on every save and describes when a record was
        written, never what it looks like. If it entered the fingerprint,
        nothing would ever resume."""
        touched = self.timeline.model_copy(deep=True)
        from datetime import timedelta

        touched.updated_at = touched.created_at + timedelta(minutes=5)
        self.assertEqual(
            seg.fingerprint(self.timeline, self.settings()),
            seg.fingerprint(touched, self.settings()),
        )


def _other_motion(current: object) -> object:
    from vtv.contracts.visual_language import CameraMotion

    return (
        CameraMotion.ZOOM_OUT
        if current is not CameraMotion.ZOOM_OUT
        else CameraMotion.ZOOM_IN
    )


class AbandonedWorkIsCollected(unittest.TestCase):
    """Segments are kept when a render fails — that is the point — so something
    has to collect the ones belonging to renders nobody will retry."""

    def test_a_fresh_directory_is_left_alone(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "recent").mkdir()
            self.assertEqual(seg.sweep(root), 0)
            self.assertTrue((root / "recent").exists())

    def test_an_old_directory_is_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stale").mkdir()
            self.assertEqual(seg.sweep(root, older_than_seconds=-1), 1)
            self.assertFalse((root / "stale").exists())

    def test_sweeping_a_directory_that_does_not_exist_is_not_an_error(self) -> None:
        """This runs at the start of every render. A missing directory, or
        somebody else's unreadable leftovers, must never stop a user's video
        from being made."""
        with TemporaryDirectory() as tmp:
            self.assertEqual(seg.sweep(Path(tmp) / "nothing-here"), 0)


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class APowerCutDoesNotCostTheWorkAlreadyDone(unittest.TestCase):
    """The scenario this was built for, run for real.

    A render dies partway through. The machine comes back. The second attempt
    must (a) not redo the segments the first one finished, and (b) produce the
    same video as a render that never died — because a resume that quietly
    shipped something different would be worse than no resume at all.
    """

    def renderer(self, storage: LocalStorageProvider, work: Path, **kw: object):  # type: ignore[no-untyped-def]
        return FfmpegRenderer(
            storage=storage,
            events=kw.pop("events", None) or EventSink(),
            workdir=work,
            segment_seconds=SEGMENT_SECONDS,
            **kw,  # type: ignore[arg-type]
        )

    def settings(self) -> RenderSettings:
        return RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24)

    def test_the_second_attempt_reuses_the_first_attempts_segments(self) -> None:
        with TemporaryDirectory(prefix="vtv-resume-") as directory:
            root = Path(directory)
            work = root / "work"
            storage = LocalStorageProvider(root / "storage")
            timeline = stored_timeline(storage, root)

            real = seg.encode_segment
            attempted: list[int] = []

            def dies_after_two(scratch: str, index: int, start: int, end: int) -> str:
                attempted.append(index)
                if len(attempted) > 2:
                    return f"segment {index}: the power went out"
                return real(scratch, index, start, end)

            seg.encode_segment = dies_after_two  # type: ignore[assignment]
            try:
                with self.assertRaises(VTVError):
                    run(
                        self.renderer(storage, work, workers=1).render(
                            timeline=timeline, settings=self.settings()
                        )
                    )
            finally:
                seg.encode_segment = real  # type: ignore[assignment]

            # The dead attempt's work is still on disk. If the renderer had
            # cleaned up after itself here — as it used to — there would be
            # nothing to resume from and this whole design would be decoration.
            scratch = work / f"{timeline.timeline_id}-{seg.fingerprint(timeline, self.settings())}"
            survivors = sorted(scratch.glob("seg-*.mp4"))
            self.assertEqual(len(survivors), 2, [p.name for p in survivors])

            events = EventSink()
            started: list[dict] = []
            events.subscribe(
                lambda event: started.append(dict(event.data))
                if event.name.value == "render.started"
                else None
            )
            job = run(
                self.renderer(storage, work, workers=1, events=events).render(
                    timeline=timeline, settings=self.settings()
                )
            )

        self.assertIs(job.status, Status.READY)
        self.assertEqual(job.progress, 1.0)
        # Reported, not inferred: the event stream says how much was inherited.
        self.assertEqual(started[0]["resumed_segments"], 2)
        self.assertGreater(started[0]["segments"], 2)

    def test_a_resumed_render_is_the_same_video(self) -> None:
        """The claim that makes resumption safe rather than merely fast."""
        with TemporaryDirectory(prefix="vtv-resume-same-") as directory:
            root = Path(directory)
            storage = LocalStorageProvider(root / "storage")
            timeline = stored_timeline(storage, root)

            whole = run(
                self.renderer(storage, root / "a", workers=1).render(
                    timeline=timeline, settings=self.settings()
                )
            )

            real = seg.encode_segment
            calls: list[int] = []

            def dies_after_two(scratch: str, index: int, start: int, end: int) -> str:
                calls.append(index)
                if len(calls) > 2:
                    return f"segment {index}: the power went out"
                return real(scratch, index, start, end)

            seg.encode_segment = dies_after_two  # type: ignore[assignment]
            try:
                with self.assertRaises(VTVError):
                    run(
                        self.renderer(storage, root / "b", workers=1).render(
                            timeline=timeline, settings=self.settings()
                        )
                    )
            finally:
                seg.encode_segment = real  # type: ignore[assignment]

            resumed = run(
                self.renderer(storage, root / "b", workers=1).render(
                    timeline=timeline, settings=self.settings()
                )
            )

            assert whole.output is not None and resumed.output is not None
            self.assertEqual(
                picture_of(storage.path_for(whole.output)),
                picture_of(storage.path_for(resumed.output)),
            )

    def test_the_scratch_directory_is_cleaned_up_when_the_render_succeeds(self) -> None:
        """Kept on failure, removed on success. Segments of a finished video are
        the one case where nobody will ever want them back."""
        with TemporaryDirectory(prefix="vtv-clean-") as directory:
            root = Path(directory)
            work = root / "work"
            storage = LocalStorageProvider(root / "storage")
            timeline = stored_timeline(storage, root)
            run(
                self.renderer(storage, work, workers=1).render(
                    timeline=timeline, settings=self.settings()
                )
            )
            self.assertEqual(list(work.glob("*/seg-*.mp4")), [])


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class DrawingInParallelDrawsTheSamePicture(unittest.TestCase):
    """A pool is only worth having if nobody has to think about it.

    Composition is a pure function of the timeline and a timestamp, so which
    process evaluates it cannot matter. That is the argument; this is the
    measurement.
    """

    def test_a_pooled_render_matches_an_inline_one_frame_for_frame(self) -> None:
        with TemporaryDirectory(prefix="vtv-parallel-") as directory:
            root = Path(directory)
            storage = LocalStorageProvider(root / "storage")
            timeline = stored_timeline(storage, root)
            settings = RenderSettings(quality=RenderQuality.PREVIEW, frame_rate=24)

            outputs = []
            for name, workers in (("inline", 1), ("pooled", 2)):
                job = run(
                    FfmpegRenderer(
                        storage=storage,
                        events=EventSink(),
                        workdir=root / name,
                        workers=workers,
                        segment_seconds=SEGMENT_SECONDS,
                    ).render(timeline=timeline, settings=settings)
                )
                self.assertIs(job.status, Status.READY)
                assert job.output is not None
                outputs.append(storage.path_for(job.output))

            self.assertEqual(picture_of(outputs[0]), picture_of(outputs[1]))

    def test_a_segmented_render_is_still_the_right_length_with_audio(self) -> None:
        """Concat plus a single mux, rather than one long pipe. The failure to
        guard against is a video that is a few frames short of its narration —
        which is invisible on a twelve-second test unless the duration is
        actually checked, and unmissable on a four-hour one."""
        with TemporaryDirectory(prefix="vtv-length-") as directory:
            root = Path(directory)
            storage = LocalStorageProvider(root / "storage")
            timeline = stored_timeline(storage, root)
            job = run(
                FfmpegRenderer(
                    storage=storage,
                    events=EventSink(),
                    workdir=root / "work",
                    workers=1,
                    segment_seconds=SEGMENT_SECONDS,
                ).render(
                    timeline=timeline,
                    settings=RenderSettings(
                        quality=RenderQuality.PREVIEW, frame_rate=24
                    ),
                )
            )
            assert job.output is not None
            details = probe(storage.path_for(job.output))

        kinds = {stream["codec_type"] for stream in details["streams"]}
        self.assertEqual(kinds, {"video", "audio"})
        self.assertAlmostEqual(float(details["format"]["duration"]), DURATION, delta=0.3)


class ThePoolIsSafeToStartOnWindows(unittest.TestCase):
    """A process pool means different things on different platforms, and the
    difference is invisible to a suite that only ever runs on Linux.

    Linux forks: the child inherits the parent's memory and never re-imports
    anything. Windows spawns: the child starts a fresh interpreter, imports the
    module holding the work function, and unpickles its arguments. Every
    assumption below is free on the machine these tests run on and fatal on the
    machine this product is being built on.
    """

    def test_the_work_function_is_importable_by_name(self) -> None:
        """A spawned child imports it rather than inheriting it. A closure, a
        lambda or a bound method would pickle as "cannot pickle" and every
        render on Windows would die at the first segment."""
        import pickle

        self.assertIs(
            pickle.loads(pickle.dumps(seg.encode_segment)), seg.encode_segment
        )

    def test_its_arguments_are_plain_data(self) -> None:
        """Only a path and two integers cross the boundary — never a timeline,
        never a PIL image, never a storage handle. The worker reads the rest
        from `plan.json`, which is what stops a four-hour render pickling the
        same megabytes two thousand times."""
        import inspect

        parameters = list(inspect.signature(seg.encode_segment).parameters.values())
        self.assertEqual(
            [p.annotation for p in parameters],
            # A path, a frame range, and the name of a painter. Still only
            # plain data: naming the painter rather than passing one is what
            # keeps a graphics device out of the pickle.
            ["str", "int", "int", "int", "str"],
        )

    def test_the_worker_entrypoint_guards_main(self) -> None:
        """Without `if __name__ == "__main__"`, a spawned child re-runs the
        module it was started from — so every render segment would start
        another job worker, on Windows only."""
        from vtv import worker

        source = Path(worker.__file__).read_text(encoding="utf-8")
        self.assertIn('if __name__ == "__main__":', source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


@unittest.skipUnless(ffmpeg.is_available(), "ffmpeg is required")
class TheFinishedPartIsWatchableWhileTheRestRenders(unittest.TestCase):
    """A four-hour render that shows nothing until the end is a four-hour
    render nobody trusts. Segments make the answer nearly free: the ones
    already on disk are playable mp4s, so a preview is a stream copy."""

    def render(self, root: Path, storage: LocalStorageProvider, timeline: Timeline,
               events: EventSink) -> object:
        return run(
            FfmpegRenderer(
                storage=storage,
                events=events,
                workdir=root / "work",
                workers=1,
                segment_seconds=SEGMENT_SECONDS,
                # Publish as soon as any segment lands, so a twelve-second test
                # exercises the same path a four-hour render does.
                preview_every_seconds=0.0,
            ).render(
                timeline=timeline,
                settings=RenderSettings(
                    quality=RenderQuality.PREVIEW, frame_rate=24
                ),
            )
        )

    def test_a_preview_is_published_before_the_render_finishes(self) -> None:
        with TemporaryDirectory(prefix="vtv-preview-") as directory:
            root = Path(directory)
            storage = LocalStorageProvider(root / "storage")
            timeline = stored_timeline(storage, root)
            seen: list[float] = []
            events = EventSink()
            events.subscribe(
                lambda event: seen.append(event.data["preview_seconds"])
                if event.name.value == "render.progress"
                and "preview_seconds" in event.data
                else None
            )
            job = self.render(root, storage, timeline, events)

        watchable = [value for value in seen if value > 0]
        self.assertTrue(watchable, "no preview was ever published")
        # It grows, and it never claims more than the finished video.
        self.assertEqual(watchable, sorted(watchable))
        self.assertLess(max(watchable), DURATION)
        self.assertIs(job.status, Status.READY)

    def test_the_preview_is_never_mistaken_for_the_output(self) -> None:
        """Two fields, because a preview covers only part of the timeline and
        anything treating it as the deliverable ships a truncated video."""
        with TemporaryDirectory(prefix="vtv-preview-out-") as directory:
            root = Path(directory)
            storage = LocalStorageProvider(root / "storage")
            timeline = stored_timeline(storage, root)
            job = self.render(root, storage, timeline, EventSink())
            self.assertIsNotNone(job.output)
            if job.preview is not None:
                self.assertNotEqual(job.preview.key, job.output.key)
                self.assertLess(job.preview_seconds, DURATION)
            # The finished file is the whole thing regardless.
            self.assertAlmostEqual(
                float(probe(storage.path_for(job.output))["format"]["duration"]),
                DURATION,
                delta=0.3,
            )
