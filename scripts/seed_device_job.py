"""Put a real, renderable project in front of the device path.

    python scripts/seed_device_job.py
    python scripts/seed_device_job.py --seconds 30 --photos

## Why this exists

Verifying Phase C means verifying the *device* path: pair, claim, download,
render, upload, report. It does not mean verifying the content pipeline, which
needs a transcription provider, a language model and an asset search before it
produces a timeline at all.

So this writes a timeline directly, using the same contracts and the same
document store the pipeline writes to. What the device receives afterwards is
indistinguishable from a pipeline-produced job, because it is the same document
read by the same code — the only thing skipped is the part that costs money and
is not under test.

It is a verification fixture and it says so. Nothing here is reachable over
HTTP, and it writes only into the development tenant.

## What it produces

A project with a stored `timeline` document, narration audio in storage, and —
with `--photos` — a real image asset so the run exercises the parts that only
appear with pictures: signed asset URLs, digest verification, the download
cache, and per-segment routing sending photographs to the graphics card.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtv.adapters.media import ffmpeg
from vtv.adapters.repository.sqlite import SqliteProjectRepository
from vtv.config import Settings
from vtv.contracts.base import TimeSpan
from vtv.contracts.errors import Status
from vtv.contracts.project import Project
from vtv.contracts.style import StyleProfile
from vtv.contracts.tenancy import PlanTier
from vtv.contracts.timeline import (
    AssetClipSource,
    CameraMotion,
    NarrationTrack,
    ProgrammaticClipSource,
    Timeline,
    Transition,
    TransitionKind,
    VisualClip,
)
from vtv.contracts.visual_language import TypographySpec
from vtv.security.directory import bootstrap
from vtv.wiring import build, repository_path

#: The tenant an unauthenticated development request runs as. Must match
#: `vtv.api.app.DEV_ORGANISATION_SLUG`, or the seeded project belongs to an
#: organisation the API is not acting for and every call returns "no such
#: project" — which looks exactly like a bug in the device path and is not one.
DEV_SLUG = "development"

SHOT = 6.0


def photograph(width: int = 1600, height: int = 1000) -> bytes:
    """Detail that rings, so the resampling path is genuinely exercised.

    Fine stripes and saturated white: soft material cannot tell a correct
    resampler from an incorrect one, which is how a real defect survived a full
    round of hardware testing on this project.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (8, 10, 14))
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 17):
        draw.line([(x, 0), (x, height)], fill=(255, 255, 255), width=3)
    for y in range(0, height, 53):
        draw.line([(0, y), (width, y)], fill=(0, 0, 0), width=2)
    draw.ellipse([200, 150, 1100, 800], outline=(255, 200, 60), width=11)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def seed(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    assembly = build(settings)
    storage = assembly.storage
    repository = SqliteProjectRepository(repository_path(settings))

    organisation = assembly.directory.organisation_by_slug(DEV_SLUG)
    if organisation is None:
        # Through `bootstrap`, which creates the organisation *and* an owner
        # *and* their membership. Creating only the organisation is not a
        # shortcut, it is a broken tenant: `development_principal` returns
        # `None` when an organisation has no members, so every unauthenticated
        # development request then fails with "please sign in" — which reads
        # exactly like a bug in the device path and is not one.
        #
        # Found by running this against a real server rather than by reasoning
        # about it, which is the entire argument for this verification.
        organisation, _, _ = bootstrap(
            assembly.directory,
            name="Development",
            slug=DEV_SLUG,
            owner_email="dev@localhost",
            plan=PlanTier.ENTERPRISE,
        )
    elif not assembly.directory.members(organisation.organisation_id):
        raise SystemExit(
            f"the '{DEV_SLUG}' organisation exists but has no members, so the API "
            "cannot act as it. Delete the directory database and start again."
        )
    tenant = organisation.organisation_id

    project = Project(organisation_id=tenant, title=args.title)

    # Narration, so the finished file has the duration the timeline claims and
    # `ffprobe` has something to confirm.
    #
    # The format matters once the videos get long. Uncompressed 44.1kHz mono is
    # 5 MB a minute, so a four-hour fixture is a 1.3 GB file that gets read into
    # memory whole and stored — a real cost, and one that measures nothing,
    # because a genuine four-hour project would not carry uncompressed audio
    # either. `--narration-format webm` gives Opus and about 50 MB instead.
    suffix = "webm" if args.narration_format == "webm" else "wav"
    audio_path = Path(args.workdir or ".") / f"seed-narration.{suffix}"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.synthesise_tone_audio(
        audio_path, duration=args.seconds, segments=[(0.3, args.seconds - 0.3)]
    )
    audio = await storage.put(
        key=f"orgs/{tenant}/projects/{project.project_id}/narration.{suffix}",
        data=audio_path.read_bytes(),
        content_type="audio/webm" if suffix == "webm" else "audio/wav",
    )
    audio_path.unlink(missing_ok=True)

    picture = None
    if args.photos:
        picture = await storage.put(
            key=f"orgs/{tenant}/projects/{project.project_id}/photo.png",
            data=photograph(),
            content_type="image/png",
        )

    kinds = (TransitionKind.DISSOLVE, TransitionKind.WIPE, TransitionKind.PUSH)
    motions = (CameraMotion.KEN_BURNS, CameraMotion.PAN_RIGHT, CameraMotion.ZOOM_IN)
    clips: list[VisualClip] = []
    shot = float(args.shot_seconds)
    # Round *up*, so the last shot reaches the end of the narration.
    #
    # It rounded down, and `--seconds 20` with six-second shots therefore
    # produced three clips covering eighteen seconds against twenty seconds of
    # audio. `Timeline.is_renderable` rejects that — correctly — and the render
    # failed with "1 uncovered stretch(es) of narration, totalling 2.0s". Every
    # length used so far happened to divide exactly, so a broken fixture looked
    # like working code for as long as nobody typed an awkward number.
    shots = max(1, math.ceil(args.seconds / shot))
    for index in range(shots):
        start, end = index * shot, min((index + 1) * shot, args.seconds)
        if start >= args.seconds:
            break
        common = {
            "scene_id": f"scn_{index:024d}",
            "span": TimeSpan.of(start, end),
            "transition_in": Transition(
                kind=TransitionKind.CUT if index == 0 else kinds[index % len(kinds)],
                duration_seconds=0.0 if index == 0 else 0.8,
            ),
        }
        # Alternating, so one render exercises both routing decisions: a
        # typography segment goes to the processor and a photograph segment to
        # the graphics card, inside the same video.
        if picture is not None and index % 2:
            clips.append(
                VisualClip(
                    source=AssetClipSource(
                        asset_id="ast_" + "b" * 24,
                        object=picture,
                        attribution="Test pattern, public domain",
                    ),
                    camera_motion=motions[index % len(motions)],
                    **common,  # type: ignore[arg-type]
                )
            )
        else:
            clips.append(
                VisualClip(
                    source=ProgrammaticClipSource(
                        spec=TypographySpec(headline=f"Section {index + 1}")
                    ),
                    **common,  # type: ignore[arg-type]
                )
            )

    timeline = Timeline(
        organisation_id=tenant,
        project_id=project.project_id,
        scene_graph_id="sgr_" + "a" * 24,
        narration=NarrationTrack(audio=audio, duration_seconds=args.seconds),
        style=StyleProfile(captions_enabled=False),
        clips=clips,
        captions=[],
        status=Status.READY,
    )

    project = project.model_copy(
        update={"timeline_id": timeline.timeline_id, "status": Status.READY}
    )
    await repository.save_project(project)
    await repository.put_document(
        project_id=project.project_id,
        kind=Timeline.document_name,
        document_id=timeline.timeline_id,
        payload=timeline.model_dump(mode="json"),
    )

    print(f"organisation : {tenant}")
    print(f"project      : {project.project_id}")
    print(f"timeline     : {timeline.timeline_id}")
    print(f"video        : {args.seconds:.0f}s, {len(clips)} shots"
          f"{', with photographs' if picture else ', typography only'}")
    print()
    print("Queue it for your own computers with:")
    print()
    print(
        "  curl -s -X POST "
        f"http://127.0.0.1:8000/v1/projects/{project.project_id}/render/device"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--title", default="Device verification")
    parser.add_argument(
        "--photos",
        action="store_true",
        help="include real image assets, so signed URLs and GPU routing are exercised",
    )
    parser.add_argument("--workdir", help="where to write the temporary narration file")
    parser.add_argument(
        "--narration-format",
        choices=("wav", "webm"),
        default="wav",
        help="uncompressed (default) or Opus, which is what long fixtures want",
    )
    parser.add_argument(
        "--shot-seconds",
        type=float,
        default=SHOT,
        help=(
            "length of each shot (default %(default)s). Raising it is how a "
            "long fixture avoids a timeline document with thousands of clips."
        ),
    )
    return asyncio.run(seed(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
