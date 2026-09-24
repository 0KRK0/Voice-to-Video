"""One test per defect found by the 2026-08-18 adversarial audit.

Kept as its own module rather than scattered through the scenario suite, for a
reason worth stating: every test here corresponds to a guarantee that was
*written down and believed* while the code did the opposite. A test that only
exists in the file where the feature lives is a test somebody deletes along with
a refactor of that feature. These are the load-bearing ones.

Each class names the guarantee. Each test names the specific way it was broken.

| Defect | Guarantee it broke |
| --- | --- |
| Re-plan dropped a locked unit | "a lock is a rule, not a hope" |
| `replace_source` ignored a clip lock | the one operation that changes the picture |
| `_retarget` repainted a locked clip | automatic processes step around locks |
| Idempotency keys collided | "we do not charge you twice" |
| Client keys were not tenant-scoped | tenant isolation |
| A client-supplied object was not checked | tenant isolation |
| A `FILLED` plan could not be rendered | target duration could plan but not deliver |
| Narration truncated at 20 000 characters | "we never silently cut your content" |
| A failed regeneration cleared a stale marker | "stale is shown, not hidden" |
| A non-`VTVError` aborted a batch | "one failed visual does not destroy a project" |
| Un-muting corrupted `current_text` | the script document is internally consistent |
| `source_text` was mutable | there is always something to compare against |
| A malformed operation returned 500 | a client error is the client's to fix |
"""

from __future__ import annotations

import asyncio
import json
import unittest
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError as PydanticValidationError
from starlette.testclient import TestClient

from vtv.api.app import create_app
from vtv.api.product import SCRIPT_DOC
from vtv.config import Settings
from vtv.contracts.base import IdPrefix, ObjectRef, TimeSpan, new_id
from vtv.contracts.errors import ProviderError, ValidationFailed, VTVError
from vtv.contracts.pacing import DurationVerdict, PacingMode
from vtv.contracts.script import BlockStatus, Script
from vtv.contracts.tenancy import (
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Role,
    User,
)
from vtv.contracts.timeline import AspectRatio, StyleProfile
from vtv.contracts.tracks import ClipSourceKind as ClipSourceKindForTest
from vtv.contracts.tracks import EditTimeline, Track, TrackKind
from vtv.contracts.transcript import Transcript, TranscriptSegment
from vtv.contracts.visual_plan import VisualStrategy
from vtv.contracts.visual_unit import (
    GroundingStatus,
    RegenerationIntent,
    VisualUnit,
    VisualUnitStatus,
    VisualVersion,
)
from vtv.observability.events import EventSink
from vtv.pipeline.editing import TimelineEditor
from vtv.pipeline.pacing import PaceableUnit, PacingPlanner
from vtv.pipeline.regeneration import RegenerationService
from vtv.pipeline.scripting import ScriptService
from vtv.pipeline.units import TimelineBuilder, VisualUnitPlanner, flatten
from vtv.security.keys import mint_api_key
from vtv.wiring import build

#: The smallest real PNG. Identified by its magic prefix, which is the only
#: identification the upload path trusts.
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SCRIPT = """Before computer science was born, mathematical methods were used to solve complex problems. These methods were slow and prone to error.

Mechanical computation eventually emerged. Charles Babbage designed engines of brass and steel. Nothing was completed in his lifetime.

The transistor transformed computing in 1947. It replaced the vacuum tube almost everywhere within twenty years.

Integrated circuits followed. Millions of transistors were placed on a single wafer of silicon.
"""


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


#: Real ids. The contracts reject anything that is not a prefixed Crockford
#: base32 identifier, which is exactly what they are for.
ORG = new_id(IdPrefix.PROJECT)
PRJ = new_id(IdPrefix.PROJECT)
SCG = new_id(IdPrefix.SCENE_GRAPH)


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

class ALockedVisualSurvivesEveryRePlan(unittest.TestCase):
    """The defect: a re-grouping could merge two shots and drop one unit.

    The dropped unit was locked, carried the picture the user chose, and the
    caller persisted the returned list wholesale — so the lock, the versions and
    the money spent on them left storage without a word. Reachable by editing a
    line and re-planning, which the editor must do after every script change.
    """

    def setUp(self) -> None:
        self.scripts = ScriptService()
        self.planner = VisualUnitPlanner()

    def build(self) -> tuple[Script, list[VisualUnit]]:
        script = self.scripts.from_text(
            SCRIPT, organisation_id=ORG, project_id=PRJ
        )
        units = self.planner.plan(script)
        return script, units

    def test_shortening_lines_cannot_delete_a_locked_unit(self) -> None:
        script, units = self.build()
        self.assertGreaterEqual(len(units), 2)

        for unit in units:
            unit.locked = True
            unit.status = VisualUnitStatus.LOCKED

        # Shorten every line. Under the old segmentation this collapsed several
        # groups into one and orphaned the units that lost the match.
        for block in list(script.blocks):
            self.scripts.replace_block_text(
                script, block_id=block.block_id, text="Short."
            )

        after = self.planner.plan(script, existing=units)
        surviving = {unit.visual_unit_id for unit in after}
        for unit in units:
            self.assertIn(
                unit.visual_unit_id,
                surviving,
                f"locked unit {unit.index} was dropped by a re-plan",
            )

    def test_a_locked_unit_keeps_the_lines_it_claimed(self) -> None:
        script, units = self.build()
        pinned = units[1]
        pinned.locked = True
        pinned.status = VisualUnitStatus.LOCKED
        claimed = list(pinned.script_block_ids)

        after = self.planner.plan(script, existing=units, mode=PacingMode.FAST)
        kept = next(u for u in after if u.visual_unit_id == pinned.visual_unit_id)
        self.assertEqual(kept.script_block_ids, claimed)

    def test_a_locked_unit_whose_lines_all_vanished_may_go(self) -> None:
        """The one legitimate disappearance, and it must not raise."""
        script, units = self.build()
        doomed = units[-1]
        doomed.locked = True
        script.blocks = [
            block
            for block in script.blocks
            if block.block_id not in doomed.script_block_ids
        ]
        after = self.planner.plan(script, existing=units)
        self.assertNotIn(
            doomed.visual_unit_id, {unit.visual_unit_id for unit in after}
        )


class RePlanningKeepsTracksTheUserAdded(unittest.TestCase):
    """The defect: a rebuild returned three tracks and discarded the rest.

    Adding a music track and then editing one line lost the music track, with no
    warning and nothing to undo.
    """

    def test_a_music_track_survives_a_rebuild(self) -> None:
        scripts, planner, builder = (
            ScriptService(),
            VisualUnitPlanner(),
            TimelineBuilder(),
        )
        script = scripts.from_text(
            SCRIPT, organisation_id=ORG, project_id=PRJ
        )
        units = planner.plan(script)
        plan = PacingPlanner().plan(
            units=[
                PaceableUnit(visual_unit_id=u.visual_unit_id, narration_seconds=4.0)
                for u in units
            ],
            narration_seconds=4.0 * len(units),
            mode=PacingMode.NATURAL,
        )
        first = builder.build(
            script=script,
            units=units,
            pacing=plan,
            organisation_id=ORG,
            project_id=PRJ,
        )
        first.tracks = [*first.tracks, Track(kind=TrackKind.MUSIC, name="Score")]

        rebuilt = builder.build(
            script=script,
            units=units,
            pacing=plan,
            organisation_id=ORG,
            project_id=PRJ,
            existing=first,
        )
        self.assertIsNotNone(rebuilt.track_of_kind(TrackKind.MUSIC))


# ---------------------------------------------------------------------------
# Pacing that can actually be rendered
# ---------------------------------------------------------------------------

class AFilledPlanCanBeRendered(unittest.TestCase):
    """The defect: the planner produced fill the render contract refused.

    `Timeline` defined the video as exactly as long as the voice and rejected
    any clip past that, so every `FILLED` plan — the entire point of asking for
    a longer video — raised inside the render job. The feature could plan and
    could not deliver.
    """

    def test_a_longer_target_flattens_into_a_valid_render_timeline(self) -> None:
        from vtv.contracts.timeline import NarrationTrack

        scripts, planner, builder = (
            ScriptService(),
            VisualUnitPlanner(),
            TimelineBuilder(),
        )
        script = scripts.from_text(
            SCRIPT, organisation_id=ORG, project_id=PRJ
        )
        units = planner.plan(script)
        narration_seconds = round(
            sum(block.duration_seconds for block in script.narrated_blocks), 3
        )

        plan = PacingPlanner().plan(
            units=[
                PaceableUnit(
                    visual_unit_id=u.visual_unit_id,
                    narration_seconds=round(
                        sum(
                            block.duration_seconds
                            for block in script.narrated_blocks
                            if block.block_id in u.script_block_ids
                        ),
                        3,
                    ),
                )
                for u in units
            ],
            narration_seconds=narration_seconds,
            mode=PacingMode.NATURAL,
            target_seconds=narration_seconds * 1.8,
        )
        self.assertIs(
            plan.verdict,
            DurationVerdict.FILLED,
            "this test is meaningless unless the plan actually fills",
        )

        timeline = builder.build(
            script=script,
            units=units,
            pacing=plan,
            organisation_id=ORG,
            project_id=PRJ,
        )
        rendered = flatten(
            timeline,
            narration=NarrationTrack(
                audio=ObjectRef(
                    bucket="b",
                    key=f"orgs/{ORG}/projects/{PRJ}/narration.wav",
                    content_type="audio/wav",
                ),
                duration_seconds=narration_seconds,
            ),
            style=StyleProfile(),
            aspect_ratio=AspectRatio.LANDSCAPE_16_9,
            scene_graph_id=SCG,
        )
        self.assertGreater(rendered.fill_seconds, 0.0)
        self.assertGreater(rendered.duration_seconds, narration_seconds)
        self.assertAlmostEqual(
            rendered.narration_end_seconds,
            rendered.narration_start_seconds + narration_seconds,
            places=3,
        )

    def test_the_default_video_is_still_exactly_as_long_as_the_voice(self) -> None:
        """The property the change must not have cost us."""
        from vtv.contracts.timeline import NarrationTrack, Timeline

        timeline = Timeline(
            organisation_id=ORG,
            project_id=PRJ,
            scene_graph_id=SCG,
            narration=NarrationTrack(
                audio=ObjectRef(
                    bucket="b",
                    key=f"orgs/{ORG}/projects/{PRJ}/n.wav",
                    content_type="audio/wav",
                ),
                duration_seconds=42.0,
            ),
        )
        self.assertEqual(timeline.duration_seconds, 42.0)
        self.assertEqual(timeline.narration_end_seconds, 42.0)


# ---------------------------------------------------------------------------
# Never a silent cut
# ---------------------------------------------------------------------------

class NarrationIsRefusedRatherThanTruncated(unittest.TestCase):
    """The defect: a long script was sliced to 20 000 characters in silence.

    The video simply stopped talking part way through, with no error, no event
    and nothing in any response to say so.
    """

    def transcript_of(self, segments: list[str]) -> Transcript:
        return Transcript(
            organisation_id=ORG,
            project_id=PRJ,
            recording_id=new_id(IdPrefix.RECORDING),
            language="en",
            segments=[
                TranscriptSegment(text=text, span=TimeSpan.of(float(i), float(i) + 1.0))
                for i, text in enumerate(segments)
            ],
        )

    def service(self):  # type: ignore[no-untyped-def]
        from vtv.pipeline.generation import GenerationRouter
        from vtv.pipeline.narration import NarrationService

        return NarrationService(
            router=GenerationRouter(events=EventSink()), events=EventSink()
        )

    def test_an_over_long_transcript_raises_with_a_usable_message(self) -> None:
        """Sized from the constant, not from a number that was once above it.

        This test used to build exactly 30 000 characters, which was over the
        20 000 cap of the day. When the cap moved to four hours the test stopped
        testing anything at all — it asserted that 29 970 was greater than
        216 000 and failed, which is the lucky version; a test that silently
        stops exercising its defect is the usual one.
        """
        from vtv.contracts.scale import MAX_SEGMENT_CHARS
        from vtv.pipeline.narration import MAX_SPEECH_CHARS

        line = "word " * ((MAX_SEGMENT_CHARS // 5) - 1)
        transcript = self.transcript_of(
            [line] * ((MAX_SPEECH_CHARS // len(line)) + 2)
        )
        with self.assertRaises(ValidationFailed) as caught:
            run(self.service().synthesise(transcript))
        message = str(caught.exception.info.user_message)
        self.assertIn("split", message.lower())
        # The number a person can act on, not a byte count.
        self.assertIn("hours", message.lower())

    def test_one_impossibly_long_line_names_the_line(self) -> None:
        """The only real vendor limit here is per *sentence* — each one is its
        own request since the caption-drift fix.

        "One line of your script is too long, and it starts like this" is a
        fixable problem. "Your script is too long" is not, when the script is
        two paragraphs.
        """
        from vtv.contracts.scale import MAX_SEGMENT_CHARS

        transcript = self.transcript_of(
            ["Short and fine.", "x" * (MAX_SEGMENT_CHARS + 1), "Also fine."]
        )
        with self.assertRaises(ValidationFailed) as caught:
            run(self.service().synthesise(transcript))
        message = str(caught.exception.info.user_message)
        self.assertIn("one line", message.lower())
        self.assertIn(str(MAX_SEGMENT_CHARS), message.replace(",", ""))

    def test_a_thirty_minute_narration_is_accepted(self) -> None:
        """The case that sent a user a 400 and a toast reading "Something in
        that request did not look right"."""
        from vtv.pipeline.narration import MAX_SPEECH_CHARS

        # About 740 lines of real narration.
        transcript = self.transcript_of(["A sentence of narration here."] * 740)
        total = sum(len(s.text) for s in transcript.segments)
        self.assertLess(total, MAX_SPEECH_CHARS)
        with self.assertRaises(ProviderError):
            # No synthesiser is registered in this test, so it fails at the
            # router — *past* the length check, which is the point.
            run(self.service().synthesise(transcript))


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

class _Exploding:
    """A producer that raises something nobody translated into a `VTVError`."""

    async def produce(self, *, unit, intent, narration):  # type: ignore[no-untyped-def]
        raise TimeoutError("provider socket timed out")


class _WorkingProducer:
    async def produce(self, *, unit, intent, narration):  # type: ignore[no-untyped-def]
        from vtv.contracts.visual_language import TypographySpec
        from vtv.contracts.visual_plan import VisualStrategy

        return VisualVersion(
            version=unit.next_version_number,
            strategy=VisualStrategy.PROGRAMMATIC,
            spec=json.loads(TypographySpec(headline="hello").model_dump_json()),
        )


class _PassValidator:
    def validate(self, *, unit, version, narration):  # type: ignore[no-untyped-def]
        return GroundingStatus.SUPPORTED, version.consistency, ""


class _ExplodingValidator:
    def validate(self, *, unit, version, narration):  # type: ignore[no-untyped-def]
        raise KeyError("bible lookup blew up")


def _unit(index: int = 0, *, with_version: bool = True) -> VisualUnit:
    from vtv.contracts.visual_language import TypographySpec
    from vtv.contracts.visual_plan import VisualStrategy

    unit = VisualUnit(
        organisation_id=ORG,
        project_id=PRJ,
        index=index,
        script_block_ids=[new_id(IdPrefix.SCRIPT_BLOCK)],
    )
    if with_version:
        unit.add_version(
            VisualVersion(
                version=1,
                strategy=VisualStrategy.PROGRAMMATIC,
                spec=json.loads(TypographySpec(headline="hi").model_dump_json()),
            ),
            select=True,
        )
        unit.status = VisualUnitStatus.READY
    return unit


class OneBadVisualDoesNotDestroyAProject(unittest.TestCase):
    """The defect: every guard was `except VTVError`.

    An adapter that let a `TimeoutError` through took the whole batch with it —
    discarding the results of every unit already regenerated *and* paid for.
    """

    def test_an_untranslated_exception_fails_one_unit_not_the_batch(self) -> None:
        service = RegenerationService(
            producer=_Exploding(), validator=_PassValidator(), events=EventSink()
        )
        units = [_unit(0), _unit(1), _unit(2)]
        out, results = run(
            service.regenerate_project(units, narrations={u.visual_unit_id: "x" for u in units})
        )
        self.assertEqual(len(out), 3)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(not result.accepted for result in results))

    def test_a_validator_that_throws_does_not_escape(self) -> None:
        service = RegenerationService(
            producer=_WorkingProducer(),
            validator=_ExplodingValidator(),
            events=EventSink(),
        )
        result = run(service.regenerate(_unit(0), narration="something"))
        self.assertFalse(result.accepted)
        # The previous version is still what plays.
        self.assertTrue(result.unit.is_deliverable)


class AFailedRegenerationDoesNotClearAWarning(unittest.TestCase):
    """The defect: any survivable failure reset the status to `READY`.

    A unit marked `TIMING_INVALIDATED` came back looking fine after an operation
    that achieved nothing — the timeline was still wrong and no longer said so.
    """

    def test_a_stale_unit_stays_stale(self) -> None:
        unit = _unit(0)
        unit.status = VisualUnitStatus.TIMING_INVALIDATED
        unit.detail = "the lines under this visual changed"

        service = RegenerationService(
            producer=_Exploding(), validator=_PassValidator(), events=EventSink()
        )
        result = run(service.regenerate(unit, narration="x"))
        self.assertIs(result.unit.status, VisualUnitStatus.TIMING_INVALIDATED)
        self.assertEqual(result.unit.detail, "the lines under this visual changed")


# ---------------------------------------------------------------------------
# The script document stays consistent with itself
# ---------------------------------------------------------------------------

class TheScriptDocumentAgreesWithItself(unittest.TestCase):
    def setUp(self) -> None:
        self.scripts = ScriptService()
        self.script = self.scripts.from_text(
            SCRIPT, organisation_id=ORG, project_id=PRJ
        )

    def test_unmuting_restores_the_line_to_current_text(self) -> None:
        """The defect: `current_text` was re-rendered on mute but not unmute."""
        block = self.script.blocks[1]
        original = self.script.current_text

        self.scripts.set_block_status(
            self.script, block_id=block.block_id, status=BlockStatus.MUTED
        )
        self.assertNotIn(block.text, self.script.current_text)

        self.scripts.set_block_status(
            self.script, block_id=block.block_id, status=BlockStatus.DRAFT
        )
        self.assertIn(block.text, self.script.current_text)
        self.assertEqual(
            self.script.current_text.count(block.text),
            original.count(block.text),
        )

    def test_muting_advances_the_version(self) -> None:
        """Otherwise a proposal computed before the mute still passes accept."""
        before = self.script.version
        self.scripts.set_block_status(
            self.script,
            block_id=self.script.blocks[0].block_id,
            status=BlockStatus.MUTED,
        )
        self.assertEqual(self.script.version, before + 1)

    def test_source_text_cannot_be_overwritten(self) -> None:
        """The defect: the docstring said immutable and nothing enforced it."""
        with self.assertRaises(PydanticValidationError):
            self.script.source_text = "the system rewrote this"


# ---------------------------------------------------------------------------
# Tenancy and idempotency, through the real application
# ---------------------------------------------------------------------------

class ApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-audit-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
            signing_key="test-signing-key-not-a-real-secret",
        )
        self.assembly = build(self.settings)
        self.app = create_app(self.settings, assembly=self.assembly)
        self.client = TestClient(self.app)
        self.repository = self.app.state.vtv.repository

    def tearDown(self) -> None:
        self.client.close()
        self._dir.cleanup()

    def tenant(self, slug: str) -> tuple[str, str]:
        organisation = self.assembly.directory.create_organisation(
            Organisation(name=slug.title(), slug=slug, plan=PlanTier.BUSINESS)
        )
        user = self.assembly.directory.create_user(
            User(email=f"o@{slug}.example", sso_subject=f"sso|{slug}")
        )
        self.assembly.directory.add_member(
            Membership(
                user_id=user.user_id,
                organisation_id=organisation.organisation_id,
                role=Role.OWNER,
            )
        )
        minted = mint_api_key(
            organisation_id=organisation.organisation_id,
            name="key",
            role=Role.ADMIN,
            scopes=list(Capability),
        )
        self.assembly.directory.store_key(minted.record)
        return organisation.organisation_id, minted.secret

    def auth(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def project(self, key: str) -> str:
        response = self.client.post(
            "/v1/projects", json={"title": "audit"}, headers=self.auth(key)
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["project_id"])

    def paste(self, project_id: str, key: str) -> None:
        response = self.client.post(
            f"/v1/projects/{project_id}/script",
            json={"text": SCRIPT},
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 201, response.text)

    def job_context(self):  # type: ignore[no-untyped-def]
        """The context a worker would build, without running a worker.

        Handlers are tested by calling them, not by sleeping until a background
        process might have. Same object, same assembly, same repository — only
        the scheduling is removed.
        """
        from vtv.jobs import JobContext

        scratch = Path(self._dir.name) / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        return JobContext(
            assembly=self.assembly,
            repository=self.repository,
            usage=self.assembly.usage,
            audit=self.assembly.audit,
            events=self.assembly.events,
            scratch=scratch,
        )

    def plan(self, project_id: str, key: str) -> dict:  # type: ignore[type-arg]
        response = self.client.post(
            f"/v1/projects/{project_id}/visual-units",
            json={},
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 200, response.text)
        return dict(response.json())


class IdempotencyKeysDoNotCollide(ApiTestCase):
    """The defect: derived keys omitted fields that distinguish real attempts.

    Two different revision kinds, two different regeneration intents, or two
    different render ranges produced one key — and the second caller was handed
    the first one's job, silently, forever (the queue's uniqueness index has no
    expiry).
    """

    def setUp(self) -> None:
        super().setUp()
        _org, self.key = self.tenant("acme")
        self.project_id = self.project(self.key)
        self.paste(self.project_id, self.key)
        self.units = self.plan(self.project_id, self.key)["units"]

    def revise(self, **body: object) -> str:
        response = self.client.post(
            f"/v1/projects/{self.project_id}/script/revisions",
            json=body,
            headers=self.auth(self.key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["job_id"])

    def test_two_target_languages_are_two_jobs(self) -> None:
        first = self.revise(kind="translate", target_language="fr")
        second = self.revise(kind="translate", target_language="es")
        self.assertNotEqual(first, second)

    def test_two_scopes_of_the_same_revision_are_two_jobs(self) -> None:
        blocks = [block["block_id"] for block in self._script()["blocks"]]
        first = self.revise(kind="shorten", block_ids=blocks[:1])
        second = self.revise(kind="shorten", block_ids=blocks[1:2])
        self.assertNotEqual(first, second)

    def test_two_intents_for_one_visual_are_two_jobs(self) -> None:
        unit_id = self.units[0]["visual_unit_id"]
        first = self._regenerate(unit_id, RegenerationIntent.MORE_CINEMATIC)
        second = self._regenerate(unit_id, RegenerationIntent.SIMPLER)
        self.assertNotEqual(first, second)

    def test_two_render_ranges_are_two_jobs(self) -> None:
        first = self._render({"scope": "range", "start": 0.0, "end": 10.0})
        second = self._render({"scope": "range", "start": 0.0, "end": 20.0})
        self.assertNotEqual(first, second)

    def test_a_repeated_request_with_one_key_is_one_job(self) -> None:
        """The property all of the above must not have cost us."""
        headers = {**self.auth(self.key), "Idempotency-Key": "retry-1"}
        one = self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json={"scope": "full_project"},
            headers=headers,
        )
        two = self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json={"scope": "full_project"},
            headers=headers,
        )
        self.assertEqual(one.json()["job_id"], two.json()["job_id"])

    def _script(self) -> dict:  # type: ignore[type-arg]
        response = self.client.get(
            f"/v1/projects/{self.project_id}/script", headers=self.auth(self.key)
        )
        return dict(response.json())

    def _regenerate(self, unit_id: str, intent: RegenerationIntent) -> str:
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/{unit_id}/regenerate",
            json={"intent": intent.value},
            headers=self.auth(self.key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["job_id"])

    def _render(self, body: dict) -> str:  # type: ignore[type-arg]
        response = self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json=body,
            headers=self.auth(self.key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["job_id"])


class IdempotencyKeysAreTenantScoped(ApiTestCase):
    """The defect: a client key was stored verbatim in a global unique index.

    Two organisations both sending `Idempotency-Key: retry-1` deduplicated
    against each other. The second one's work silently never ran and it was
    handed the *first one's* job id — a correctness bug and a cross-tenant leak
    in the same line.
    """

    def test_the_same_client_key_from_two_tenants_is_two_jobs(self) -> None:
        _a, key_a = self.tenant("acme")
        _b, key_b = self.tenant("beta")

        jobs = []
        for key in (key_a, key_b):
            project_id = self.project(key)
            self.paste(project_id, key)
            self.plan(project_id, key)
            response = self.client.post(
                f"/v1/projects/{project_id}/render",
                json={"scope": "full_project"},
                headers={**self.auth(key), "Idempotency-Key": "retry-1"},
            )
            self.assertEqual(response.status_code, 202, response.text)
            jobs.append(response.json()["job_id"])

        self.assertNotEqual(jobs[0], jobs[1])


class AnotherTenantsAssetCannotBeSplicedIn(ApiTestCase):
    """The defect: a client-supplied `object` was trusted.

    Storage keys are tenant-namespaced and the provider enforced that on write
    but not on read, so a principal who learned another organisation's key could
    put that organisation's asset into their own render — and the only trace
    would be in the rendered video.
    """

    def test_an_object_outside_the_tenant_prefix_is_refused(self) -> None:
        _a, key_a = self.tenant("acme")
        org_b, _key_b = self.tenant("beta")

        project_id = self.project(key_a)
        self.paste(project_id, key_a)
        self.plan(project_id, key_a)

        timeline = self.client.get(
            f"/v1/projects/{project_id}/timeline", headers=self.auth(key_a)
        ).json()
        clip = next(
            clip
            for track in timeline["tracks"]
            if track["kind"] == "visual"
            for clip in track["clips"]
        )

        response = self.client.patch(
            f"/v1/projects/{project_id}/timeline",
            json={
                "operations": [
                    {
                        "kind": "replace_source",
                        "clip_id": clip["clip_id"],
                        "source_kind": "object",
                        "object": {
                            "bucket": self.settings.storage_bucket,
                            "key": f"orgs/{org_b}/projects/prj_other/asset.png",
                            "content_type": "image/png",
                        },
                    }
                ]
            },
            headers=self.auth(key_a),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("yours", response.json()["error"]["message"].lower())

    def test_the_tenants_own_object_is_accepted(self) -> None:
        """The property the check must not have cost us."""
        org_a, key_a = self.tenant("acme")
        project_id = self.project(key_a)
        self.paste(project_id, key_a)
        self.plan(project_id, key_a)

        timeline = self.client.get(
            f"/v1/projects/{project_id}/timeline", headers=self.auth(key_a)
        ).json()
        clip = next(
            clip
            for track in timeline["tracks"]
            if track["kind"] == "visual"
            for clip in track["clips"]
        )
        response = self.client.patch(
            f"/v1/projects/{project_id}/timeline",
            json={
                "expected_version": timeline["version"],
                "operations": [
                    {
                        "kind": "replace_source",
                        "clip_id": clip["clip_id"],
                        "source_kind": "object",
                        "object": {
                            "bucket": self.settings.storage_bucket,
                            "key": f"orgs/{org_a}/projects/{project_id}/a.png",
                            "content_type": "image/png",
                        },
                    }
                ],
            },
            headers=self.auth(key_a),
        )
        self.assertEqual(response.status_code, 200, response.text)


class ClientMistakesAreNotServerErrors(ApiTestCase):
    """The defect: a malformed operation escaped as a raw pydantic error.

    It missed the API's `VTVError` handler and returned 500 — telling the client
    that a request only it could fix was our fault.
    """

    def test_a_malformed_operation_is_a_400(self) -> None:
        _org, key = self.tenant("acme")
        project_id = self.project(key)
        self.paste(project_id, key)
        self.plan(project_id, key)

        response = self.client.patch(
            f"/v1/projects/{project_id}/timeline",
            json={"operations": [{"kind": "insert", "start": 9.0, "end": 4.0}]},
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_an_oversized_json_body_is_refused(self) -> None:
        """A JSON field is not object storage."""
        from vtv.api.app import MAX_JSON_BODY_BYTES

        _org, key = self.tenant("acme")
        project_id = self.project(key)
        response = self.client.post(
            f"/v1/projects/{project_id}/script",
            content=json.dumps({"text": "x" * (MAX_JSON_BODY_BYTES + 10)}),
            headers={**self.auth(key), "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400, response.text)


class AProposalCanBeReadBeforeItIsAccepted(ApiTestCase):
    """The defect: there was no route to fetch a proposal.

    A client could accept a revision it had no way to display — which makes "we
    show you the change before we make it" unreachable over HTTP, whatever the
    services do.
    """

    def test_a_stored_proposal_is_readable(self) -> None:
        from vtv.contracts.script import RevisionKind, RevisionProposal, TextChange

        _org, key = self.tenant("acme")
        project_id = self.project(key)
        self.paste(project_id, key)

        script = Script.model_validate(
            run(self.repository.get_document(project_id=project_id, kind=SCRIPT_DOC))
        )
        proposal = RevisionProposal(
            organisation_id=script.organisation_id,
            project_id=project_id,
            script_id=script.script_id,
            based_on_version=script.version,
            kind=RevisionKind.SHORTEN,
            changes=[
                TextChange(
                    block_id=script.blocks[0].block_id,
                    original=script.blocks[0].text,
                    proposed="Shorter.",
                    reason="tightened",
                )
            ],
            estimated_duration_before=10.0,
            estimated_duration_after=4.0,
        )
        run(
            self.repository.put_document(
                project_id=project_id,
                kind=f"revision:{proposal.revision_id}",
                document_id=proposal.revision_id,
                payload=json.loads(proposal.model_dump_json()),
            )
        )

        response = self.client.get(
            f"/v1/projects/{project_id}/script/revisions/{proposal.revision_id}",
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["duration_delta_seconds"], -6.0)
        self.assertEqual(body["changes"][0]["proposed"], "Shorter.")
        # Which vendor produced it is internal.
        self.assertNotIn("provider", body)
        self.assertNotIn("model", body)

    def test_another_tenant_cannot_read_it(self) -> None:
        _a, key_a = self.tenant("acme")
        _b, key_b = self.tenant("beta")
        project_id = self.project(key_a)
        response = self.client.get(
            f"/v1/projects/{project_id}/script/revisions/rev_whatever",
            headers=self.auth(key_b),
        )
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# ---------------------------------------------------------------------------
# Second round: defects found verifying the first round's fixes
# ---------------------------------------------------------------------------

class TheVideoNeverDriftsAwayFromTheVoice(unittest.TestCase):
    """The defect the first fix introduced, and the worst one in this file.

    Making the render contract accept a longer-than-narration timeline let a
    `FILLED` plan through — and the plan put its fill *between* narration
    segments, which one continuous recording cannot honour. The video came out
    the right length and several seconds out of sync, which is worse than the
    refusal it replaced.
    """

    def setUp(self) -> None:
        self.scripts = ScriptService()
        self.planner = VisualUnitPlanner()
        self.builder = TimelineBuilder()
        self.script = self.scripts.from_text(
            SCRIPT, organisation_id=ORG, project_id=PRJ
        )
        self.units = self.planner.plan(self.script)
        self.narration_seconds = round(
            sum(b.duration_seconds for b in self.script.narrated_blocks), 3
        )

    def plan_for(self, target: float | None):  # type: ignore[no-untyped-def]
        return PacingPlanner().plan(
            units=[
                PaceableUnit(
                    visual_unit_id=u.visual_unit_id,
                    narration_seconds=round(
                        sum(
                            b.duration_seconds
                            for b in self.script.narrated_blocks
                            if b.block_id in u.script_block_ids
                        ),
                        3,
                    ),
                )
                for u in self.units
            ],
            narration_seconds=self.narration_seconds,
            target_seconds=target,
        )

    def test_a_filled_plan_leaves_no_gap_in_the_narration(self) -> None:
        timeline = self.builder.build(
            script=self.script,
            units=self.units,
            pacing=self.plan_for(self.narration_seconds * 2.0),
            organisation_id=ORG,
            project_id=PRJ,
        )
        track = timeline.track_of_kind(TrackKind.NARRATION)
        assert track is not None
        clips = sorted(track.clips, key=lambda c: c.start)
        for earlier, later in pairwise(clips):
            self.assertAlmostEqual(earlier.end, later.start, places=3)

    def test_the_narration_track_spans_exactly_the_voice(self) -> None:
        timeline = self.builder.build(
            script=self.script,
            units=self.units,
            pacing=self.plan_for(self.narration_seconds * 2.0),
            organisation_id=ORG,
            project_id=PRJ,
        )
        track = timeline.track_of_kind(TrackKind.NARRATION)
        assert track is not None
        clips = sorted(track.clips, key=lambda c: c.start)
        spanned = round(clips[-1].end - clips[0].start, 3)
        self.assertAlmostEqual(spanned, self.narration_seconds, places=2)

    def test_flatten_refuses_a_timeline_whose_narration_has_a_gap(self) -> None:
        """The chokepoint, tested by breaking the thing it guards."""
        from vtv.contracts.timeline import NarrationTrack

        timeline = self.builder.build(
            script=self.script,
            units=self.units,
            pacing=self.plan_for(None),
            organisation_id=ORG,
            project_id=PRJ,
        )
        track = timeline.track_of_kind(TrackKind.NARRATION)
        assert track is not None
        # Shove the tail of the narration five seconds later, as an interior
        # hold used to.
        track.clips = [
            track.clips[0],
            *(
                clip.model_copy(
                    update={
                        "start": round(clip.start + 5.0, 3),
                        "end": round(clip.end + 5.0, 3),
                    }
                )
                for clip in track.clips[1:]
            ),
        ]

        with self.assertRaises(VTVError) as caught:
            flatten(
                timeline,
                narration=NarrationTrack(
                    audio=ObjectRef(
                        bucket="b",
                        key=f"orgs/{ORG}/projects/{PRJ}/n.wav",
                        content_type="audio/wav",
                    ),
                    duration_seconds=self.narration_seconds,
                ),
                style=StyleProfile(),
                aspect_ratio=AspectRatio.LANDSCAPE_16_9,
                scene_graph_id=SCG,
            )
        self.assertIn("drifting", str(caught.exception.info.user_message))


class AStatusNeverClaimsMoreThanTheUnitHas(unittest.TestCase):
    """The defect: "a previous version exists" was used as "something plays".

    `selected` returns the newest version even when the newest was refused by
    the grounding gate and deliberately not selected, so a second failure turned
    a genuinely-`FAILED` unit into a `READY` one with nothing to show.
    """

    def test_a_unit_whose_only_versions_were_refused_stays_failed(self) -> None:
        from vtv.contracts.visual_language import TypographySpec
        from vtv.contracts.visual_plan import VisualStrategy

        unit = VisualUnit(
            organisation_id=ORG,
            project_id=PRJ,
            index=0,
            script_block_ids=[new_id(IdPrefix.SCRIPT_BLOCK)],
        )
        unit.add_version(
            VisualVersion(
                version=1,
                strategy=VisualStrategy.PROGRAMMATIC,
                spec=json.loads(TypographySpec(headline="hi").model_dump_json()),
                grounding=GroundingStatus.REFUSED,
            ),
            select=False,
        )
        unit.status = VisualUnitStatus.FAILED

        service = RegenerationService(
            producer=_Exploding(), validator=_PassValidator(), events=EventSink()
        )
        result = run(service.regenerate(unit, narration="x"))
        self.assertIs(result.unit.status, VisualUnitStatus.FAILED)
        self.assertFalse(result.unit.is_deliverable)


class ClientKeysAreNamespacedByOperationToo(ApiTestCase):
    """The defect: one client key reused across two calls deduplicated.

    The queue's uniqueness index does not consider the job kind, so a client
    that sends one key per user action had its second call silently absorbed and
    was handed a job of the wrong kind to poll.
    """

    def test_one_key_across_two_operations_is_two_jobs(self) -> None:
        _org, key = self.tenant("acme")
        project_id = self.project(key)
        self.paste(project_id, key)
        self.plan(project_id, key)

        headers = {**self.auth(key), "Idempotency-Key": "action-1"}
        revise = self.client.post(
            f"/v1/projects/{project_id}/script/revisions",
            json={"kind": "fix_grammar"},
            headers=headers,
        )
        render = self.client.post(
            f"/v1/projects/{project_id}/render",
            json={"scope": "full_project"},
            headers=headers,
        )
        self.assertEqual(revise.status_code, 202, revise.text)
        self.assertEqual(render.status_code, 202, render.text)
        self.assertNotEqual(revise.json()["job_id"], render.json()["job_id"])


class LocksSurviveTheirContainer(unittest.TestCase):
    """The defect: removing a track deleted the locked clips on it.

    A lock that a neighbouring operation can delete around is a lock that means
    "until somebody removes the lane".
    """

    def setUp(self) -> None:
        self.editor = TimelineEditor()
        self.track = Track(kind=TrackKind.MUSIC, name="Score")
        self.timeline = EditTimeline(
            organisation_id=ORG,
            project_id=PRJ,
            tracks=[
                Track(kind=TrackKind.VISUAL, name="Visuals"),
                Track(kind=TrackKind.NARRATION, name="Narration"),
                self.track,
            ],
        )

    def op(self, **fields):  # type: ignore[no-untyped-def]
        from vtv.pipeline.editing import TimelineOperation

        return TimelineOperation(**fields)

    def add_locked_clip(self) -> None:
        from vtv.pipeline.editing import OperationKind

        inserted = self.editor.apply(
            self.timeline,
            self.op(
                kind=OperationKind.INSERT,
                track_id=self.track.track_id,
                start=0.0,
                end=4.0,
            ),
        )
        clip_id = inserted.changed_clip_ids[0]
        self.timeline = self.editor.apply(
            inserted.timeline, self.op(kind=OperationKind.LOCK, clip_id=clip_id)
        ).timeline

    def test_removing_a_track_holding_a_locked_clip_is_refused(self) -> None:
        from vtv.contracts.errors import PolicyViolation
        from vtv.pipeline.editing import OperationKind

        self.add_locked_clip()
        with self.assertRaises(PolicyViolation) as caught:
            self.editor.apply(
                self.timeline,
                self.op(
                    kind=OperationKind.REMOVE_TRACK, track_id=self.track.track_id
                ),
            )
        self.assertIn("locked", str(caught.exception.info.user_message).lower())

    def test_removing_a_derived_track_is_refused(self) -> None:
        from vtv.contracts.errors import PolicyViolation
        from vtv.pipeline.editing import OperationKind

        caption = Track(kind=TrackKind.CAPTION, name="Captions")
        timeline = EditTimeline(
            organisation_id=ORG,
            project_id=PRJ,
            tracks=[
                Track(kind=TrackKind.VISUAL, name="Visuals"),
                Track(kind=TrackKind.NARRATION, name="Narration"),
                caption,
            ],
        )
        with self.assertRaises(PolicyViolation) as caught:
            self.editor.apply(
                timeline,
                self.op(
                    kind=OperationKind.REMOVE_TRACK, track_id=caption.track_id
                ),
            )
        self.assertIn("script", str(caught.exception.info.user_message).lower())


class TheScriptHistoryNeverBreaksAnEdit(unittest.TestCase):
    """The defect: the history cap raised *after* mutating the script.

    At entry 201 the text, the version and the block had already changed, and
    the caller got a 500 holding a half-applied document.
    """

    def test_editing_past_the_history_cap_still_works(self) -> None:
        from vtv.contracts.script import MAX_HISTORY

        scripts = ScriptService()
        script = scripts.from_text(SCRIPT, organisation_id=ORG, project_id=PRJ)
        block_id = script.blocks[0].block_id

        for index in range(MAX_HISTORY + 20):
            scripts.replace_block_text(
                script, block_id=block_id, text=f"Revision number {index}."
            )

        self.assertLessEqual(len(script.history), MAX_HISTORY)
        self.assertEqual(script.history[-1].version, script.version)
        self.assertEqual(script.block(block_id).text, f"Revision number {MAX_HISTORY + 19}.")


class AMalformedAnimationIsRefusedAtTheDoor(ApiTestCase):
    """The defect: a `spec` was stored unvalidated and crashed the render job."""

    def test_nonsense_spec_is_a_400(self) -> None:
        _org, key = self.tenant("acme")
        project_id = self.project(key)
        self.paste(project_id, key)
        self.plan(project_id, key)

        timeline = self.client.get(
            f"/v1/projects/{project_id}/timeline", headers=self.auth(key)
        ).json()
        track = next(t for t in timeline["tracks"] if t["kind"] == "visual")

        response = self.client.patch(
            f"/v1/projects/{project_id}/timeline",
            json={
                "operations": [
                    {
                        "kind": "insert",
                        "track_id": track["track_id"],
                        "start": 10_000.0,
                        "end": 10_004.0,
                        "source_kind": "programmatic",
                        "spec": {"nonsense": True},
                    }
                ]
            },
            headers=self.auth(key),
        )
        self.assertEqual(response.status_code, 400, response.text)


class AScriptProjectCanActuallyBeRendered(ApiTestCase):
    """The defect: the product lane had no first render, so it had no render.

    Every export from a pasted-script project failed with *"render the project
    once before rendering an edit — we need the narration audio"*, and there was
    no way to do that first render: the only routes that produce narration take
    an uploaded recording or document, and a project created by typing has
    neither. The golden path ended one step before its last step, and the
    frontend's Export button was wired to an endpoint that could only ever fail.

    The end-to-end run is what found it. Nothing in the unit suite noticed,
    because every test that rendered had gone through the pipeline lane first.
    """

    def test_render_produces_output_from_a_pasted_script(self) -> None:
        from vtv.jobs import HANDLERS

        _org, key = self.tenant("acme")
        project_id = self.project(key)
        self.paste(project_id, key)
        self.plan(project_id, key)

        accepted = self.client.post(
            f"/v1/projects/{project_id}/render", json={}, headers=self.auth(key)
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)

        # Run the handler in-process rather than waiting on a worker: this test
        # is about the handler's behaviour, and a sleep-and-poll would make it
        # slow and flaky for no extra coverage.
        context = self.job_context()
        render_job_id = run(
            HANDLERS["render_scope"](
                context,
                {
                    "organisation_id": _org,
                    "project_id": project_id,
                    "region": {"scope": "full_project"},
                },
            )
        )
        self.assertTrue(render_job_id)

        history = self.client.get(
            f"/v1/projects/{project_id}/renders", headers=self.auth(key)
        ).json()
        self.assertGreaterEqual(len(history["renders"]), 1, history)
        self.assertTrue(history["renders"][0]["has_output"], history)

    def test_the_voice_is_synthesised_once_and_reused(self) -> None:
        """A second render must not pay a synthesiser again for the same words."""
        from vtv.jobs import HANDLERS
        from vtv.product_jobs import NARRATION_DOC

        _org, key = self.tenant("acme")
        project_id = self.project(key)
        self.paste(project_id, key)
        self.plan(project_id, key)

        context = self.job_context()
        payload = {
            "organisation_id": _org,
            "project_id": project_id,
            "region": {"scope": "full_project"},
        }
        run(HANDLERS["render_scope"](context, payload))
        first = run(
            self.repository.get_document(project_id=project_id, kind=NARRATION_DOC)
        )
        self.assertIsNotNone(first)

        calls: list[object] = []
        service = self.assembly.pipeline.narration
        original = service.synthesise

        async def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(args)
            return await original(*args, **kwargs)

        service.synthesise = counted  # type: ignore[method-assign]
        try:
            run(HANDLERS["render_scope"](context, payload))
        finally:
            service.synthesise = original  # type: ignore[method-assign]

        self.assertEqual(calls, [], "the second render re-synthesised the voice")

    def test_the_measured_timings_are_written_back_to_the_script(self) -> None:
        """Estimates built the timeline; measurement is what a re-plan must use."""
        from vtv.jobs import HANDLERS

        _org, key = self.tenant("acme")
        project_id = self.project(key)
        self.paste(project_id, key)
        self.plan(project_id, key)

        context = self.job_context()
        run(
            HANDLERS["render_scope"](
                context,
                {
                    "organisation_id": _org,
                    "project_id": project_id,
                    "region": {"scope": "full_project"},
                },
            )
        )

        stored = run(
            self.repository.get_document(project_id=project_id, kind=SCRIPT_DOC)
        )
        script = Script.model_validate(stored)
        narrated = [block for block in script.blocks if block.is_narrated]
        self.assertTrue(narrated)
        self.assertTrue(
            all(block.measured_end is not None for block in narrated),
            "no block carries a measured timing",
        )
        self.assertIsNotNone(script.measured_duration_seconds)


class AnOverLongVoiceIsRefusedRatherThanCutOff(unittest.TestCase):
    """The defect class: a voice longer than its lane ships as a silent cut.

    The renderer places one continuous audio file at one offset and encodes to
    the shorter stream. Narration four seconds longer than the pictures
    therefore produces a video that stops talking mid-sentence, with nothing in
    the output, the log or the API to say a sentence went missing.
    """

    def test_a_voice_longer_than_the_lane_raises(self) -> None:
        from vtv.product_jobs import _require_fits

        track = Track(kind=TrackKind.NARRATION, name="Narration")
        timeline = EditTimeline(
            organisation_id=ORG, project_id=PRJ, tracks=[track]
        )
        track.clips.append(
            _narration_clip(track.track_id, 0.0, 10.0)
        )

        _require_fits(9.5, timeline)
        _require_fits(10.0, timeline)

        with self.assertRaises(VTVError) as caught:
            _require_fits(14.0, timeline)
        message = str(caught.exception.info.user_message)
        self.assertIn("longer", message)
        self.assertIn("Re-plan", message)


def _narration_clip(track_id: str, start: float, end: float):  # type: ignore[no-untyped-def]
    from vtv.contracts.tracks import ClipSourceKind, TimelineClip

    return TimelineClip(
        track_id=track_id,
        start=start,
        end=end,
        source_kind=ClipSourceKind.EMPTY,
        label="Narration",
    )


class ADeletedEndpointAnswersNotFound(ApiTestCase):
    """The defect: the SPA fallback turned removed routes into 405.

    The single-page fallback was registered for GET only. Starlette answers a
    path that matches a route but not its methods with *405 Method Not Allowed*,
    so `POST /internal/retention/sweep` — a route deleted precisely because it
    was unauthenticated — started replying "wrong verb" instead of "no such
    thing". To a scanner that reads as an endpoint worth finding the verb for.
    """

    def test_posting_to_a_removed_route_is_a_404(self) -> None:
        response = self.client.post("/internal/retention/sweep", json={})
        self.assertEqual(response.status_code, 404, response.text)

    def test_posting_to_a_nonsense_path_is_a_404_not_an_html_shell(self) -> None:
        response = self.client.post("/definitely/not/a/route", json={})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("<html", response.text.lower())

    def test_a_page_request_still_gets_the_shell(self) -> None:
        response = self.client.get("/projects")
        self.assertEqual(response.status_code, 200, response.text)


class ChangingAVisualChangesTheVideo(ApiTestCase):
    """The defect: three ways to change a visual, one of them told the renderer.

    `run_render_scope` flattens the **edit timeline** and never reads a visual
    unit. So a clip left pointing at the previous version is what the customer's
    file actually contains — and only `regenerate` repointed it. Choosing a
    different version and giving a visual a file of your own both changed the
    unit, left the clip alone, and reported success.

    The inspector's "Switching a version is free and instant" was therefore true
    of the inspector and false of the export: pick v1, the interface agrees, and
    the download contains v2. Handing somebody a video that is not the one they
    approved is the worst failure this product can have, and it was reachable by
    clicking the control the interface most encourages you to click.
    """

    def setUp(self) -> None:
        super().setUp()
        _org, self.key = self.tenant("acme")
        self.project_id = self.project(self.key)
        self.paste(self.project_id, self.key)
        self.plan(self.project_id, self.key)

    def visual_clip(self, unit_id: str) -> dict:  # type: ignore[type-arg]
        timeline = self.client.get(
            f"/v1/projects/{self.project_id}/timeline", headers=self.auth(self.key)
        ).json()
        track = next(t for t in timeline["tracks"] if t["kind"] == "visual")
        return next(c for c in track["clips"] if c["visual_unit_id"] == unit_id)

    def test_using_your_own_file_repoints_the_clip_the_renderer_reads(self) -> None:
        units = self.client.get(
            f"/v1/projects/{self.project_id}/visual-units", headers=self.auth(self.key)
        ).json()["units"]
        unit = units[0]

        uploaded = self.client.post(
            f"/v1/projects/{self.project_id}/media",
            files={"file": ("photo.png", PNG_BYTES, "image/png")},
            headers=self.auth(self.key),
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        asset_id = uploaded.json()["media_asset_id"]

        before = self.visual_clip(unit["visual_unit_id"])
        applied = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/{unit['visual_unit_id']}/media",
            json={"media_asset_id": asset_id, "lock": True},
            headers=self.auth(self.key),
        )
        self.assertEqual(applied.status_code, 200, applied.text)

        after = self.visual_clip(unit["visual_unit_id"])
        self.assertEqual(after["clip_id"], before["clip_id"], "identity must survive")
        self.assertEqual(
            after["source_kind"],
            "object",
            "the clip still points at the picture your file replaced",
        )

    def test_switching_a_version_repoints_the_clip(self) -> None:
        """The clip shows the version the unit says is selected.

        Asserted on the clip's *content*, not on a version bump. Both matter,
        but only one is the guarantee: on an install with no image generator
        every version is the same typography, so a bump-based assertion would
        pass or fail on whether two versions happened to differ rather than on
        whether the repointing works. The two versions here are made to differ
        precisely so the assertion has something to see.
        """
        from vtv.contracts.visual_language import TypographySpec

        units = [
            VisualUnit.model_validate(item)
            for item in (
                run(
                    self.repository.get_document(
                        project_id=self.project_id, kind="visual_units"
                    )
                )
                or {}
            ).get("units", [])
        ]
        unit = units[0]
        for headline in ("The first idea", "A different idea entirely"):
            unit.add_version(
                VisualVersion(
                    version=unit.next_version_number,
                    strategy=VisualStrategy.PROGRAMMATIC,
                    spec=json.loads(
                        TypographySpec(headline=headline).model_dump_json()
                    ),
                ),
                select=True,
            )
        run(
            self.repository.put_document(
                project_id=self.project_id,
                kind="visual_units",
                document_id="visual_units",
                payload={"units": [json.loads(u.model_dump_json()) for u in units]},
            )
        )

        first, second = unit.versions[0], unit.versions[1]
        self.assertEqual(unit.selected_version_id, second.version_id)

        chosen = self.client.patch(
            f"/v1/projects/{self.project_id}/visual-units/{unit.visual_unit_id}",
            json={"version_id": first.version_id},
            headers=self.auth(self.key),
        )
        self.assertEqual(chosen.status_code, 200, chosen.text)
        self.assertEqual(chosen.json()["selected_version_id"], first.version_id)

        clip = self.visual_clip(unit.visual_unit_id)
        self.assertEqual(clip["source_kind"], "programmatic")

        stored = run(
            self.repository.get_document(
                project_id=self.project_id, kind="edit_timeline"
            )
        )
        track = next(t for t in stored["tracks"] if t["kind"] == "visual")
        repointed = next(
            c for c in track["clips"] if c["visual_unit_id"] == unit.visual_unit_id
        )
        self.assertEqual(
            repointed["spec"]["headline"],
            "The first idea",
            "the clip the renderer reads still shows the version you switched away from",
        )

    def test_a_no_op_store_does_not_bump_the_timeline_version(self) -> None:
        """A version nobody caused turns the next save into a spurious conflict."""
        units = self.client.get(
            f"/v1/projects/{self.project_id}/visual-units", headers=self.auth(self.key)
        ).json()["units"]
        before = self.client.get(
            f"/v1/projects/{self.project_id}/timeline", headers=self.auth(self.key)
        ).json()["version"]

        # Locking changes the unit's state and nothing about what it shows.
        self.client.patch(
            f"/v1/projects/{self.project_id}/visual-units/{units[1]['visual_unit_id']}",
            json={"locked": True},
            headers=self.auth(self.key),
        )
        after = self.client.get(
            f"/v1/projects/{self.project_id}/timeline", headers=self.auth(self.key)
        ).json()["version"]
        self.assertEqual(before, after)

    def test_a_locked_clip_is_never_repainted(self) -> None:
        """The unit may be unlocked while one of its clips is not."""
        from vtv.contracts.tracks import EditTimeline
        from vtv.pipeline.units import retarget

        units = [
            VisualUnit.model_validate(item)
            for item in (
                run(
                    self.repository.get_document(
                        project_id=self.project_id, kind="visual_units"
                    )
                )
                or {}
            ).get("units", [])
        ]
        stored = run(
            self.repository.get_document(
                project_id=self.project_id, kind="edit_timeline"
            )
        )
        timeline = EditTimeline.model_validate(stored)

        visual = next(t for t in timeline.tracks if t.kind.value == "visual")
        # Both fields in one update: `TimelineClip` validates on assignment and
        # refuses a text clip with no text.
        visual.clips[0] = visual.clips[0].model_copy(
            update={
                "locked": True,
                "source_kind": ClipSourceKindForTest.TEXT,
                "text": "a card the user typed",
            }
        )

        result = retarget(timeline, units)
        kept = next(t for t in result.tracks if t.kind.value == "visual").clips[0]
        self.assertEqual(kept.text, "a card the user typed")
        self.assertTrue(kept.locked)


class ApprovalSurvivesARePlan(unittest.TestCase):
    """The defect: `is_user_owned` said one thing and the planner tested another.

    `VisualUnit.is_user_owned` reads *"the user has expressed a preference that
    must survive a re-plan"* and includes `APPROVED`. `USER_OWNED_STATES` reads
    *"automatic processes must not move a unit out of one of these without being
    told to"*. Both correct, both written down — and the planner pinned on
    `unit.locked`, so approving a visual and then editing a line silently
    deleted it.

    The exact shape the audit keeps finding: the rule is on the object, and the
    call site has its own narrower copy of it.
    """

    def units_after_replan(self, first_status, first_lock, edit: str):  # type: ignore[no-untyped-def]
        from vtv.contracts.visual_unit import VisualUnitStatus

        scripts = ScriptService()
        script = scripts.from_text(SCRIPT, organisation_id=ORG, project_id=PRJ)
        planner = VisualUnitPlanner()
        units = planner.plan(script)

        subject = units[0].model_copy(deep=True)
        subject.status = first_status
        subject.locked = first_lock
        units[0] = subject

        scripts.replace_block_text(
            script, block_id=script.blocks[1].block_id, text=edit
        )
        del VisualUnitStatus
        return subject, planner.plan(script, existing=units)

    def test_an_approved_visual_is_not_dropped(self) -> None:
        from vtv.contracts.visual_unit import VisualUnitStatus

        subject, after = self.units_after_replan(
            VisualUnitStatus.APPROVED,
            False,
            "Mechanical computation emerged from brass, steel and patience.",
        )
        self.assertIn(
            subject.visual_unit_id,
            {unit.visual_unit_id for unit in after},
            "an approved visual was discarded by a re-plan",
        )

    def test_a_locked_visual_is_still_not_dropped(self) -> None:
        from vtv.contracts.visual_unit import VisualUnitStatus

        subject, after = self.units_after_replan(
            VisualUnitStatus.LOCKED,
            True,
            "Mechanical computation emerged from brass, steel and patience.",
        )
        self.assertIn(subject.visual_unit_id, {unit.visual_unit_id for unit in after})

    def test_an_unlocked_upload_keeps_its_binding(self) -> None:
        """Unlocking is about regeneration, not about a regroup deleting it.

        Unlocking says "the system may propose something else". It is not
        permission for a re-grouping to drop the binding, leaving a fresh empty
        unit and no trace that a file was ever there.
        """
        from vtv.contracts.visual_unit import VisualUnitStatus

        scripts = ScriptService()
        script = scripts.from_text(SCRIPT, organisation_id=ORG, project_id=PRJ)
        planner = VisualUnitPlanner()
        units = planner.plan(script)

        subject = units[0].model_copy(deep=True)
        subject.add_version(
            VisualVersion(
                version=subject.next_version_number,
                strategy=VisualStrategy.EXISTING_ASSET,
                object=ObjectRef(
                    bucket="vtv",
                    key=f"orgs/{ORG}/projects/{PRJ}/media/mine.png",
                    content_type="image/png",
                ),
                user_owned=True,
                grounding=GroundingStatus.NOT_APPLICABLE,
            ),
            select=True,
        )
        subject.locked = False
        subject.status = VisualUnitStatus.READY
        units[0] = subject
        self.assertTrue(subject.shows_user_media)

        scripts.replace_block_text(
            script,
            block_id=script.blocks[1].block_id,
            text="Mechanical computation emerged from brass, steel and patience.",
        )
        after = planner.plan(script, existing=units)

        kept = next(
            (u for u in after if u.visual_unit_id == subject.visual_unit_id), None
        )
        self.assertIsNotNone(kept, "an unlocked upload lost its binding")
        assert kept is not None
        self.assertTrue(
            kept.shows_user_media, "the unit survived but the file did not"
        )


class CaptionsFollowTheRenderNotTheVoiceLane(ApiTestCase):
    """The defect: `/captions.vtt` 404'd for every project the Studio makes.

    The handler read the `timeline` document, which only the voice pipeline
    writes. A project authored as a *script* keeps its timeline under
    `edit_timeline`, so the lookup missed and the endpoint answered "not found"
    — while the render history sitting next to it reported
    ``has_captions: true`` for the very same render.

    That combination is the shape of bug this repository cares most about: the
    product told the customer an artefact existed and then refused to hand it
    over. The render had produced captions all along; only this endpoint could
    not find them.
    """

    def setUp(self) -> None:
        super().setUp()
        _, self.key = self.tenant("captions")
        self.project_id = self.project(self.key)
        self.paste(self.project_id, self.key)
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units",
            json={},
            headers=self.auth(self.key),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _render_now(self) -> None:
        """Run the handler directly rather than waiting on a worker."""
        from vtv.product_jobs import run_render_scope

        response = self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json={"scope": "full_project"},
            headers=self.auth(self.key),
        )
        self.assertEqual(response.status_code, 202, response.text)
        project = run(
            self.repository.get_project(project_id=self.project_id)
        )
        run(
            run_render_scope(
                self.job_context(),
                {
                    "organisation_id": project.organisation_id,
                    "project_id": self.project_id,
                    "region": {},
                },
            )
        )

    def test_a_script_authored_project_serves_its_captions(self) -> None:
        self._render_now()
        response = self.client.get(
            f"/v1/projects/{self.project_id}/captions.vtt", headers=self.auth(self.key)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("WEBVTT", response.text)
        self.assertIn("-->", response.text, "a cue file with no cues is not captions")

    def test_the_history_and_the_endpoint_agree(self) -> None:
        """Neither number is interesting alone. Their disagreement was the bug."""
        self._render_now()
        history = self.client.get(
            f"/v1/projects/{self.project_id}/renders", headers=self.auth(self.key)
        ).json()["renders"]
        self.assertTrue(history)
        claimed = history[0]["has_captions"]
        served = (
            self.client.get(
                f"/v1/projects/{self.project_id}/captions.vtt",
                headers=self.auth(self.key),
            ).status_code
            == 200
        )
        self.assertEqual(
            claimed,
            served,
            "the render history and the captions endpoint disagree about "
            "whether captions exist",
        )

    def test_before_any_render_there_are_honestly_none(self) -> None:
        """The fallback must not invent captions for a project with no render."""
        response = self.client.get(
            f"/v1/projects/{self.project_id}/captions.vtt", headers=self.auth(self.key)
        )
        self.assertEqual(response.status_code, 404)


class TheRoutesTheStudioActuallyCalls(ApiTestCase):
    """The contract the browser depends on, walked in order.

    Written after a live debugging session where `GET /script`, `GET /timeline`
    and `POST /render` all returned 404 and it looked like a routing bug. It was
    not: those are honest domain 404s for documents that do not exist yet, and
    `visual-units` and `media` return 200 from the very same router beside them.

    The distinction is worth a test, because "404 means the route is missing"
    and "404 means the thing is not there yet" lead to opposite repairs — and
    the wrong one adds a duplicate endpoint to a system that already had the
    right one.
    """

    def setUp(self) -> None:
        super().setUp()
        _, self.key = self.tenant("studio")
        self.project_id = self.project(self.key)

    def get(self, path: str):  # type: ignore[no-untyped-def]
        return self.client.get(
            f"/v1/projects/{self.project_id}{path}", headers=self.auth(self.key)
        )

    def test_an_empty_project_answers_404_for_documents_it_has_not_got(self) -> None:
        """Not a missing route. A missing document."""
        for path in ("/script", "/timeline"):
            with self.subTest(path):
                response = self.get(path)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_the_same_router_answers_200_beside_them(self) -> None:
        """Proves the router is mounted, which is what a routing bug would deny."""
        for path in ("/visual-units", "/media"):
            with self.subTest(path):
                self.assertEqual(self.get(path).status_code, 200)

    def test_render_refuses_a_project_with_no_timeline(self) -> None:
        """And refuses it as not-found rather than accepting an empty render."""
        response = self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json={},
            headers=self.auth(self.key),
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_the_whole_studio_walk_in_order(self) -> None:
        """create → script → visual units → timeline → render → job status.

        Every hop is the route the shipped frontend calls, at the method it
        calls it with. If any of these move, this fails before a user finds out.
        """
        self.paste(self.project_id, self.key)
        self.assertEqual(self.get("/script").status_code, 200)

        planned = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units",
            json={},
            headers=self.auth(self.key),
        )
        self.assertEqual(planned.status_code, 200, planned.text)
        self.assertTrue(planned.json()["units"])

        timeline = self.get("/timeline")
        self.assertEqual(timeline.status_code, 200, timeline.text)
        self.assertIn("tracks", timeline.json())

        accepted = self.client.post(
            f"/v1/projects/{self.project_id}/render",
            json={},
            headers=self.auth(self.key),
        )
        self.assertEqual(accepted.status_code, 202, accepted.text)
        job_id = accepted.json()["job_id"]

        # The API enqueued and did not render. That is the rule, so assert it:
        # the job must be waiting for a worker, not already finished.
        status = self.client.get(f"/v1/jobs/{job_id}", headers=self.auth(self.key))
        self.assertEqual(status.status_code, 200, status.text)
        self.assertIn(status.json()["status"], {"pending", "running"})

    def test_a_failed_job_reports_why_and_not_merely_that(self) -> None:
        """What the Studio needs in order to stop saying 'Something went wrong.'

        The browser showed a generic toast while this payload carried the real
        sentence the whole time. Driven through the queue's own execution path —
        a registered handler that raises, exactly as the transcription stage
        does when no provider is configured — so this proves the message
        survives the queue rather than that a stub returns what it was handed.
        """
        from vtv.contracts.errors import ErrorCode, VTVError

        queue = self.app.state.vtv.queue

        async def refuses(_payload):  # type: ignore[no-untyped-def]
            raise VTVError(
                "no speech-to-text provider is registered",
                code=ErrorCode.TRANSCRIPTION_FAILED,
                user_message="Transcription is not configured.",
            )

        queue.register("render_scope", refuses)
        handle = run(
            queue.enqueue(
                kind="render_scope",
                payload={
                    "organisation_id": "org_x",
                    "project_id": self.project_id,
                    "region": {},
                },
            )
        )
        run(queue.run_once())

        status = self.client.get(
            f"/v1/jobs/{handle.job_id}", headers=self.auth(self.key)
        ).json()
        self.assertEqual(status["error"]["code"], "transcription_failed")
        self.assertEqual(
            status["error"]["message"],
            "Transcription is not configured.",
            "the specific reason was lost between the handler and the client",
        )
        self.assertNotEqual(status["error"]["message"], "Something went wrong.")

    def test_a_terminal_failure_is_not_retried_three_times(self) -> None:
        """Budget, licence, validation and missing objects fail once.

        The queue retried every failure until `max_attempts` ran out, whatever
        the failure was. For a category that cannot succeed on a second attempt
        that is the same verdict reached three times — and, where the attempt
        reached a metered provider, charged three times to learn it.
        """
        from vtv.contracts.errors import ErrorCategory, ErrorCode, VTVError

        queue = self.app.state.vtv.queue
        calls = {"n": 0}

        async def over_budget(_payload):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise VTVError(
                "project budget exhausted",
                code=ErrorCode.BUDGET_EXCEEDED,
                category=ErrorCategory.POLICY,
                user_message="This project has used its budget.",
            )

        queue.register("render_scope", over_budget)
        handle = run(
            queue.enqueue(
                kind="render_scope",
                payload={
                    "organisation_id": "org_x",
                    "project_id": self.project_id,
                    "region": {},
                },
            )
        )
        run(queue.run_once())
        # A retry would leave the row claimable again. Nothing should be.
        self.assertIsNone(run(queue.run_once()))
        self.assertEqual(calls["n"], 1, "a terminal failure was attempted twice")

        status = self.client.get(
            f"/v1/jobs/{handle.job_id}", headers=self.auth(self.key)
        ).json()
        self.assertEqual(status["status"], "failed", status)
        self.assertEqual(status["error"]["message"], "This project has used its budget.")


class OneRenderKeyPerProject(ApiTestCase):
    """The defect: every project's first render deduplicated against the others.

    The Studio sends an idempotency key so a double-click resolves to one job.
    The key was built from the timeline version and the scope — and the server
    namespaces a supplied key by tenant and operation, but not by project.

    Every new project starts at timeline version 1. So `render:1:full` was one
    key for the whole organisation: the second project's render was handed the
    *first* project's job id, and polling it answered 404, because that job
    belongs to a project the poller is not asking about. If the first project
    had since been deleted, 404 for certain. The user saw "Something went
    wrong." on a render that the server had accepted with a 202.
    """

    def setUp(self) -> None:
        super().setUp()
        _, self.key = self.tenant("renderkeys")

    def _ready_project(self) -> str:
        project_id = self.project(self.key)
        self.paste(project_id, self.key)
        planned = self.client.post(
            f"/v1/projects/{project_id}/visual-units",
            json={},
            headers=self.auth(self.key),
        )
        self.assertEqual(planned.status_code, 200, planned.text)
        return project_id

    def _render(self, project_id: str, key: str) -> str:
        response = self.client.post(
            f"/v1/projects/{project_id}/render",
            json={"scope": "full_project"},
            headers={**self.auth(self.key), "Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 202, response.text)
        return str(response.json()["job_id"])

    def test_two_projects_at_the_same_version_get_their_own_jobs(self) -> None:
        first, second = self._ready_project(), self._ready_project()
        # The key the shipped client now sends: project, version, scope.
        one = self._render(first, f"render:{first}:1:full")
        two = self._render(second, f"render:{second}:1:full")
        self.assertNotEqual(
            one, two, "two projects were handed the same render job"
        )

    def test_the_job_a_render_returns_can_be_polled(self) -> None:
        """The 404 the user actually saw, asserted end to end."""
        first, second = self._ready_project(), self._ready_project()
        self._render(first, f"render:{first}:1:full")
        job = self._render(second, f"render:{second}:1:full")
        status = self.client.get(f"/v1/jobs/{job}", headers=self.auth(self.key))
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["project_id"], second)

    def test_the_same_project_twice_is_still_one_job(self) -> None:
        """The property the key exists for, which must not be lost."""
        project_id = self._ready_project()
        key = f"render:{project_id}:1:full"
        self.assertEqual(self._render(project_id, key), self._render(project_id, key))


class ASilentNarrationIsNotAVideo(unittest.TestCase):
    """A forty-minute render ran for three hours and produced a file whose
    audio track measured **-91 dB** — digital silence — with every visual a
    title card. The job reported success and the cost was $0.00.

    Nothing raised. `NarrationService` has a silent fallback for when no
    synthesiser can be reached, it used it, and it recorded `has_speech: False`
    in the narration document *and* in the `narration.synthesised` event. Both
    were correct. Nothing read either.
    """

    def spoken(self, has_speech: bool):  # type: ignore[no-untyped-def]
        class Spoken:
            pass

        s = Spoken()
        s.has_speech = has_speech  # type: ignore[attr-defined]
        return s

    def context(self, has_provider: bool):  # type: ignore[no-untyped-def]
        from vtv.observability.events import EventSink

        class Router:
            def providers_for(self, kind):  # type: ignore[no-untyped-def]
                del kind
                return [object()] if has_provider else []

        class Assembly:
            router = Router()

        class Context:
            assembly = Assembly()
            events = EventSink()

        return Context()

    def test_a_deployment_with_no_synthesiser_is_warned_not_refused(self) -> None:
        """Silent by construction, and possibly deliberate. Refusing would
        break every credential-free deployment to prevent a problem they do
        not have."""
        from vtv.product_jobs import _require_audible

        _require_audible(self.spoken(False), self.context(False))

    def test_it_refuses_before_the_encode(self) -> None:
        """Something was configured, it was asked, and nothing came back."""
        from vtv.product_jobs import _require_audible

        with self.assertRaises(VTVError) as caught:
            _require_audible(self.spoken(False), self.context(True))
        message = str(caught.exception.info.user_message)
        self.assertIn("voice", message.lower())
        # The user has to be able to act on it. "Something went wrong" is what
        # this replaced.
        self.assertIn("quota", message.lower())

    def test_a_narrated_track_passes(self) -> None:
        from vtv.product_jobs import _require_audible

        _require_audible(self.spoken(True), self.context(True))

    def test_an_object_without_the_flag_is_not_refused(self) -> None:
        """A stand-in in a test, or an older document. Refusing on absence
        would turn a missing field into a failed render."""
        from vtv.product_jobs import _require_audible

        _require_audible(object(), self.context(True))


class AFinishedRenderSaysSo(unittest.TestCase):
    """After the three-hour render the stored project still read
    `status: pending`, every stage `pending`, and `cost_usd: 0.0`.

    Not cosmetic: `Project.progress` is computed from the stages, so the Studio
    had no honest number to show; `current_stage` was "capture" forever on a
    finished video; and the cost quoted before the render could never be
    compared with the cost charged.
    """

    def project(self):  # type: ignore[no-untyped-def]
        from vtv.contracts.project import Project

        return Project(
            organisation_id=ORG, owner_id=new_id(IdPrefix.PROJECT), title="t"
        )

    def result(self):  # type: ignore[no-untyped-def]
        class Result:
            duration_seconds = 12.0

        return Result()

    def ledger(self, total: float):  # type: ignore[no-untyped-def]
        class Ledger:
            def total_usd(self) -> float:
                return total

        return Ledger()

    def test_every_stage_is_marked_done(self) -> None:
        from vtv.product_jobs import _record_progress

        project = self.project()
        self.assertLess(project.progress, 1.0)
        _record_progress(project, self.result(), spent=self.ledger(0.42))
        self.assertEqual(project.progress, 1.0)
        self.assertIsNone(project.current_stage)

    def test_the_cost_is_recorded(self) -> None:
        from vtv.product_jobs import _record_progress

        project = self.project()
        _record_progress(project, self.result(), spent=self.ledger(0.4237))
        self.assertAlmostEqual(project.total_cost_usd, 0.4237)

    def test_a_ledger_that_cannot_answer_does_not_lose_the_render(self) -> None:
        from vtv.product_jobs import _record_progress

        class Broken:
            def total_usd(self) -> float:
                raise RuntimeError("no ledger")

        project = self.project()
        _record_progress(project, self.result(), spent=Broken())
        self.assertEqual(project.progress, 1.0)


class TheDeploymentCanOnlyTighten(unittest.TestCase):
    """Two questions wearing the same clothes: what the system *can* do, and
    what this customer *may* do."""

    def test_a_free_tier_limit_is_enforced(self) -> None:
        from vtv.contracts.scale import ceiling_minutes

        self.assertEqual(ceiling_minutes(15), 15)

    def test_an_operator_cannot_raise_it_past_what_was_measured(self) -> None:
        """Setting 600 does not make the contracts hold 600 minutes; it makes
        them raise a validation error somewhere a user cannot act on."""
        from vtv.contracts.scale import MAX_PROJECT_MINUTES, ceiling_minutes

        self.assertEqual(ceiling_minutes(600), MAX_PROJECT_MINUTES)

    def test_unset_means_the_engineering_ceiling(self) -> None:
        from vtv.contracts.scale import MAX_PROJECT_MINUTES, ceiling_minutes

        self.assertEqual(ceiling_minutes(None), MAX_PROJECT_MINUTES)
        self.assertEqual(ceiling_minutes(0), MAX_PROJECT_MINUTES)
