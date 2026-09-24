"""The media API, driven through the real HTTP application.

The promises under test are the three the module exists to keep — your file is
yours, it survives a re-plan, and it is never thrown away — plus the tenancy and
upload-safety rules that every route in this system shares.

Written against `create_app` rather than the service, for the reason the
2026-08-13 audit established: comprehensively-tested code can sit off the
request path entirely, and a media library nobody can reach is not a feature.
"""

from __future__ import annotations

import asyncio
import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from vtv.api.media import MEDIA_DOC
from vtv.config import Settings
from vtv.contracts.media import MediaLibrary
from vtv.contracts.tenancy import (
    Capability,
    Membership,
    Organisation,
    PlanTier,
    Role,
    User,
)
from vtv.contracts.visual_unit import VisualUnit
from vtv.security.keys import mint_api_key
from vtv.wiring import build

SCRIPT = """Before computer science was born, mathematical methods were used to solve complex problems. These methods were slow and prone to error.

Mechanical computation eventually emerged. Charles Babbage designed engines of brass and steel. Nothing was completed in his lifetime.

The transistor transformed computing in 1947. It replaced the vacuum tube almost everywhere within twenty years.
"""

#: The smallest real PNG: an 8-byte signature plus a minimal IHDR. Enough for
#: `sniff` to identify it by magic, which is the only identification this system
#: trusts.
PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
WAV = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 32
CLEAN_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
HOSTILE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


class MediaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = TemporaryDirectory(prefix="vtv-media-")
        root = Path(self._dir.name)
        self.settings = Settings(asset_search_endpoint="", 
            storage_root=root / "storage",
            database_url=f"sqlite:///{root / 'vtv.db'}",
            env="development",
            signing_key="test-signing-key-not-a-real-secret",
        )
        self.assembly = build(self.settings)
        from vtv.api.app import create_app

        self.app = create_app(self.settings, assembly=self.assembly)
        self.client = TestClient(self.app)
        self.repository = self.app.state.vtv.repository
        self.org, self.key = self.tenant("acme")

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

    # -- helpers ----------------------------------------------------------

    def auth(self, key: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {key or self.key}"}

    def project(self, key: str | None = None) -> str:
        response = self.client.post(
            "/v1/projects", json={"title": "media"}, headers=self.auth(key)
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["project_id"])

    def upload(
        self,
        project_id: str,
        *,
        name: str = "photo.png",
        data: bytes = PNG,
        content_type: str = "image/png",
        kind: str | None = None,
        key: str | None = None,
    ):  # type: ignore[no-untyped-def]
        form = {"kind": kind} if kind else None
        return self.client.post(
            f"/v1/projects/{project_id}/media",
            files={"file": (name, data, content_type)},
            data=form,
            headers=self.auth(key),
        )

    def paste(self, project_id: str) -> None:
        response = self.client.post(
            f"/v1/projects/{project_id}/script",
            json={"text": SCRIPT},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 201, response.text)

    def plan(self, project_id: str) -> list:  # type: ignore[type-arg]
        response = self.client.post(
            f"/v1/projects/{project_id}/visual-units", json={}, headers=self.auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return list(response.json()["units"])

    def stored_units(self, project_id: str) -> list[VisualUnit]:
        payload = run(
            self.repository.get_document(project_id=project_id, kind="visual_units")
        )
        return [
            VisualUnit.model_validate(item) for item in (payload or {}).get("units", [])
        ]

    def library(self, project_id: str) -> MediaLibrary:
        payload = run(
            self.repository.get_document(project_id=project_id, kind=MEDIA_DOC)
        )
        if not payload:
            # A project that has never had an upload has no library document.
            # That is the empty library, not a failure to read one.
            return MediaLibrary(organisation_id=self.org, project_id=project_id)
        return MediaLibrary.model_validate(payload)


class UploadsAreIdentifiedByTheirBytes(MediaTestCase):
    def test_an_image_is_accepted_and_becomes_an_asset(self) -> None:
        project_id = self.project()
        response = self.upload(project_id)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["kind"], "image")
        self.assertEqual(body["origin"], "user_upload")
        self.assertEqual(body["status"], "ready")
        self.assertTrue(body["user_owned"])
        self.assertTrue(body["capabilities"]["visual"])
        self.assertFalse(body["capabilities"]["trim"])

    def test_a_renamed_file_is_identified_by_content_not_by_name(self) -> None:
        """The whole point of inspecting bytes."""
        project_id = self.project()
        response = self.upload(
            project_id, name="song.mp3", data=PNG, content_type="audio/mpeg"
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["kind"], "image")

    def test_a_document_is_refused_with_a_sentence_that_helps(self) -> None:
        project_id = self.project()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        response = self.upload(
            project_id,
            name="notes.docx",
            data=buffer.getvalue(),
            content_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        )
        self.assertEqual(response.status_code, 415, response.text)
        message = response.json()["error"]["message"]
        self.assertIn("notes.docx", message)
        self.assertIn("script", message.lower())

    def test_an_svg_with_active_content_is_refused_not_stored(self) -> None:
        """`inspect_upload` raises for this one case; the route must not leak it."""
        project_id = self.project()
        response = self.upload(
            project_id,
            name="logo.svg",
            data=HOSTILE_SVG,
            content_type="image/svg+xml",
        )
        self.assertEqual(response.status_code, 415, response.text)
        self.assertIn("active content", response.json()["error"]["message"])
        self.assertEqual(self.library(project_id).assets, [])

    def test_a_clean_svg_becomes_a_vector(self) -> None:
        project_id = self.project()
        response = self.upload(
            project_id, name="mark.svg", data=CLEAN_SVG, content_type="image/svg+xml"
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["kind"], "vector")
        # A vector scales; it has no pixels to lose.
        self.assertFalse(body["capabilities"]["crop"])

    def test_audio_gets_the_controls_audio_needs(self) -> None:
        project_id = self.project()
        response = self.upload(
            project_id, name="room.wav", data=WAV, content_type="audio/wav"
        )
        self.assertEqual(response.status_code, 201, response.text)
        capabilities = response.json()["capabilities"]
        self.assertTrue(capabilities["audio_lane"])
        self.assertTrue(capabilities["level"])
        self.assertTrue(capabilities["loop"])
        self.assertFalse(capabilities["visual"])

    def test_a_logo_is_never_a_visual(self) -> None:
        project_id = self.project()
        response = self.upload(project_id, name="mark.png", kind="logo")
        self.assertEqual(response.status_code, 201, response.text)
        capabilities = response.json()["capabilities"]
        self.assertFalse(capabilities["visual"])
        self.assertTrue(capabilities["project_mark"])

    def test_a_caller_cannot_declare_a_video_to_be_a_logo(self) -> None:
        """The kind hint is checked against the bytes, not trusted."""
        project_id = self.project()
        response = self.upload(
            project_id, name="clip.wav", data=WAV, content_type="audio/wav", kind="logo"
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["kind"], "audio")


class MediaIsStoredWhereTheTenantCanReachIt(MediaTestCase):
    def test_the_object_key_is_inside_the_tenant_prefix(self) -> None:
        project_id = self.project()
        self.upload(project_id)
        asset = self.library(project_id).assets[0]
        assert asset.object is not None
        self.assertTrue(asset.object.key.startswith(f"orgs/{self.org}/"))
        self.assertIn(f"projects/{project_id}/media/", asset.object.key)

    def test_another_tenant_cannot_list_or_upload(self) -> None:
        project_id = self.project()
        _other_org, other_key = self.tenant("beta")

        listed = self.client.get(
            f"/v1/projects/{project_id}/media", headers=self.auth(other_key)
        )
        self.assertEqual(listed.status_code, 404, listed.text)

        uploaded = self.upload(project_id, key=other_key)
        self.assertEqual(uploaded.status_code, 404, uploaded.text)

    def test_a_detail_read_carries_a_signed_url(self) -> None:
        project_id = self.project()
        created = self.upload(project_id).json()
        response = self.client.get(
            f"/v1/projects/{project_id}/media/{created['media_asset_id']}",
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["url"])


class EditsAreRefusedWhenTheKindCannotDoThem(MediaTestCase):
    def test_an_image_cannot_be_trimmed(self) -> None:
        project_id = self.project()
        created = self.upload(project_id).json()
        response = self.client.patch(
            f"/v1/projects/{project_id}/media/{created['media_asset_id']}",
            json={"trim": {"in_seconds": 1.0, "out_seconds": 4.0}},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("length", response.json()["error"]["message"].lower())

    def test_a_vector_cannot_be_cropped(self) -> None:
        project_id = self.project()
        created = self.upload(
            project_id, name="m.svg", data=CLEAN_SVG, content_type="image/svg+xml"
        ).json()
        response = self.client.patch(
            f"/v1/projects/{project_id}/media/{created['media_asset_id']}",
            json={"crop": {"x": 0.1, "y": 0.1, "width": 0.5, "height": 0.5}},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_audio_accepts_a_trim_a_level_and_a_loop(self) -> None:
        project_id = self.project()
        created = self.upload(
            project_id, name="room.wav", data=WAV, content_type="audio/wav"
        ).json()
        response = self.client.patch(
            f"/v1/projects/{project_id}/media/{created['media_asset_id']}",
            json={
                "trim": {
                    "in_seconds": 11.0,
                    "out_seconds": 64.0,
                    "gain_db": -18.0,
                    "duck_db": -10.0,
                    "loop": True,
                }
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["trim"]["gain_db"], -18.0)
        self.assertTrue(body["trim"]["loop"])
        self.assertEqual(body["duration_seconds"], 53.0)

    def test_a_crop_outside_the_frame_is_refused(self) -> None:
        project_id = self.project()
        created = self.upload(project_id).json()
        response = self.client.patch(
            f"/v1/projects/{project_id}/media/{created['media_asset_id']}",
            json={"crop": {"x": 0.8, "y": 0.0, "width": 0.5, "height": 0.5}},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)


class YourFileBecomesTheVisualAndStaysThere(MediaTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.units = self.plan(self.project_id)
        self.asset = self.upload(self.project_id).json()

    def use(self, unit_id: str, **body: object):  # type: ignore[no-untyped-def]
        return self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/{unit_id}/media",
            json={"media_asset_id": self.asset["media_asset_id"], **body},
            headers=self.auth(),
        )

    def test_using_a_file_selects_it_and_locks_the_visual(self) -> None:
        unit_id = self.units[1]["visual_unit_id"]
        response = self.use(unit_id)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["locked"])
        self.assertEqual(body["status"], "locked")

        unit = next(
            u for u in self.stored_units(self.project_id)
            if u.visual_unit_id == unit_id
        )
        self.assertTrue(unit.locked)
        assert unit.selected is not None
        assert unit.selected.object is not None
        self.assertIn("media/", unit.selected.object.key)
        self.assertIn(self.asset["media_asset_id"], unit.selected.object.key)

    def test_the_lock_can_be_declined(self) -> None:
        unit_id = self.units[1]["visual_unit_id"]
        response = self.use(unit_id, lock=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["locked"])

    def test_your_file_survives_a_re_plan(self) -> None:
        """The promise the whole media system rests on."""
        unit_id = self.units[1]["visual_unit_id"]
        self.use(unit_id)
        before = next(
            u for u in self.stored_units(self.project_id)
            if u.visual_unit_id == unit_id
        )
        chosen = before.selected_version_id

        # Edit a line elsewhere, then re-plan — the operation the editor has to
        # perform after every script change.
        script = self.client.get(
            f"/v1/projects/{self.project_id}/script", headers=self.auth()
        ).json()
        self.client.patch(
            f"/v1/projects/{self.project_id}/script/blocks/"
            f"{script['blocks'][0]['block_id']}",
            json={"text": "Something rather shorter."},
            headers=self.auth(),
        )
        self.plan(self.project_id)

        after = {u.visual_unit_id: u for u in self.stored_units(self.project_id)}
        self.assertIn(unit_id, after, "the unit holding your file was dropped")
        kept = after[unit_id]
        self.assertTrue(kept.locked)
        self.assertEqual(kept.selected_version_id, chosen)

    def test_a_locked_visual_refuses_a_replacement(self) -> None:
        """Your own file does not outrank your own lock."""
        unit_id = self.units[1]["visual_unit_id"]
        self.use(unit_id)
        second = self.upload(self.project_id, name="other.png").json()
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/{unit_id}/media",
            json={"media_asset_id": second["media_asset_id"]},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("locked", response.json()["error"]["message"].lower())

    def test_a_logo_cannot_be_used_as_a_visual(self) -> None:
        logo = self.upload(self.project_id, name="mark.png", kind="logo").json()
        response = self.client.post(
            f"/v1/projects/{self.project_id}/visual-units/"
            f"{self.units[0]['visual_unit_id']}/media",
            json={"media_asset_id": logo["media_asset_id"]},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("overlay", response.json()["error"]["message"].lower())

    def test_your_version_makes_no_claim_the_system_must_stand_behind(self) -> None:
        """Grounding and consistency are not applicable to your own photograph."""
        unit_id = self.units[1]["visual_unit_id"]
        self.use(unit_id)
        unit = next(
            u for u in self.stored_units(self.project_id)
            if u.visual_unit_id == unit_id
        )
        assert unit.selected is not None
        self.assertEqual(unit.selected.grounding.value, "not_applicable")
        self.assertEqual(unit.selected.consistency.value, "not_applicable")
        self.assertEqual(unit.selected.cost_usd, 0.0)


class NothingIsThrownAwayByAccident(MediaTestCase):
    def test_deleting_an_asset_a_visual_uses_is_refused(self) -> None:
        project_id = self.project()
        self.paste(project_id)
        units = self.plan(project_id)
        asset = self.upload(project_id).json()
        self.client.post(
            f"/v1/projects/{project_id}/visual-units/"
            f"{units[1]['visual_unit_id']}/media",
            json={"media_asset_id": asset["media_asset_id"]},
            headers=self.auth(),
        )
        response = self.client.delete(
            f"/v1/projects/{project_id}/media/{asset['media_asset_id']}",
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("Visual", response.json()["error"]["message"])

    def test_an_unused_asset_deletes_cleanly(self) -> None:
        project_id = self.project()
        asset = self.upload(project_id).json()
        response = self.client.delete(
            f"/v1/projects/{project_id}/media/{asset['media_asset_id']}",
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.library(project_id).assets, [])


class TheLibraryReportsWhatItHolds(MediaTestCase):
    def test_the_list_counts_by_origin_and_names_the_project_mark(self) -> None:
        project_id = self.project()
        self.upload(project_id, name="a.png")
        self.upload(project_id, name="b.png")
        logo = self.upload(project_id, name="mark.png", kind="logo").json()

        response = self.client.get(
            f"/v1/projects/{project_id}/media", headers=self.auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["assets"]), 3)
        self.assertEqual(body["counts"]["user_upload"], 3)
        self.assertEqual(body["counts"]["ai_generated"], 0)
        self.assertEqual(body["project_mark_id"], logo["media_asset_id"])

    def test_the_list_can_be_filtered_by_origin(self) -> None:
        project_id = self.project()
        self.upload(project_id)
        response = self.client.get(
            f"/v1/projects/{project_id}/media?origin=ai_generated",
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["assets"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class AnUploadedImageCanBecomeTheProjectMark(MediaTestCase):
    """Nobody uploads a file thinking "this is a logo".

    Kind is decided from the bytes at upload, which is right — a caller must not
    be able to rename a video to `audio` and get it onto a sound lane. But it
    left the project mark reachable only by uploading the same file a second
    time with a flag set, which is a workflow nobody would design on purpose.

    So there is exactly one promotion, between two kinds that share the same
    bytes and the same inspection verdict, and it is reversible.
    """

    def test_an_image_can_be_promoted_and_demoted(self) -> None:
        project_id = self.project()
        asset_id = self.upload(project_id, name="mark.png").json()["media_asset_id"]

        promoted = self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"kind": "logo"},
            headers=self.auth(),
        )
        self.assertEqual(promoted.status_code, 200, promoted.text)
        self.assertEqual(promoted.json()["kind"], "logo")
        self.assertTrue(promoted.json()["capabilities"]["project_mark"])
        self.assertFalse(promoted.json()["capabilities"]["visual"])

        listed = self.client.get(
            f"/v1/projects/{project_id}/media", headers=self.auth()
        ).json()
        self.assertEqual(listed["project_mark_id"], asset_id)

        demoted = self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"kind": "image"},
            headers=self.auth(),
        )
        self.assertEqual(demoted.status_code, 200, demoted.text)
        self.assertEqual(demoted.json()["kind"], "image")
        listed = self.client.get(
            f"/v1/projects/{project_id}/media", headers=self.auth()
        ).json()
        self.assertIsNone(listed["project_mark_id"])

    def test_promoting_a_second_image_demotes_the_first(self) -> None:
        """At most one mark per project, enforced rather than hoped for."""
        project_id = self.project()
        first = self.upload(project_id, name="a.png").json()["media_asset_id"]
        second = self.upload(
            project_id, name="b.png", data=PNG + b"\x00"
        ).json()["media_asset_id"]

        for asset_id in (first, second):
            response = self.client.patch(
                f"/v1/projects/{project_id}/media/{asset_id}",
                json={"kind": "logo"},
                headers=self.auth(),
            )
            self.assertEqual(response.status_code, 200, response.text)

        library = self.library(project_id)
        logos = [a.media_asset_id for a in library.assets if a.kind.value == "logo"]
        self.assertEqual(logos, [second])

    def test_the_placement_of_a_demoted_mark_is_forgotten(self) -> None:
        project_id = self.project()
        asset_id = self.upload(project_id, name="mark.png").json()["media_asset_id"]

        self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"kind": "logo"},
            headers=self.auth(),
        )
        placed = self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"logo": {"placement": "top_left", "width_percent": 22.0}},
            headers=self.auth(),
        )
        self.assertEqual(placed.status_code, 200, placed.text)
        self.assertEqual(placed.json()["logo"]["placement"], "top_left")

        self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"kind": "image"},
            headers=self.auth(),
        )
        again = self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"kind": "logo"},
            headers=self.auth(),
        )
        self.assertNotEqual(again.json()["logo"]["placement"], "top_left")

    def test_a_video_cannot_be_renamed_into_something_it_is_not(self) -> None:
        """The reason `kind` is not a setter."""
        project_id = self.project()
        asset_id = self.upload(
            project_id, name="clip.wav", data=WAV, content_type="audio/wav"
        ).json()["media_asset_id"]

        response = self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"kind": "image"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("comes from the file itself", response.text)

    def test_an_unknown_kind_is_a_400_not_a_500(self) -> None:
        project_id = self.project()
        asset_id = self.upload(project_id).json()["media_asset_id"]
        response = self.client.patch(
            f"/v1/projects/{project_id}/media/{asset_id}",
            json={"kind": "hologram"},
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)


class AnAssetReachesTheTimelineByIdNotByKey(MediaTestCase):
    """Placing a file on a lane must not require the client to hold a storage key.

    The alternative was publishing `ObjectRef`s in the asset view — a tenant's
    key space handed to every browser to save one server-side lookup. Naming the
    asset instead keeps keys where they belong and makes the tenant check
    structural: an id from another tenant is simply not in this project's
    library, so there is no key to get wrong.
    """

    def setUp(self) -> None:
        super().setUp()
        self.project_id = self.project()
        self.paste(self.project_id)
        self.plan(self.project_id)

    def music_track(self) -> str:
        response = self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json={
                "operations": [
                    {"kind": "add_track", "track_kind": "music", "track_name": "Music"}
                ]
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        timeline = self.client.get(
            f"/v1/projects/{self.project_id}/timeline", headers=self.auth()
        ).json()
        return str(
            next(t for t in timeline["tracks"] if t["kind"] == "music")["track_id"]
        )

    def test_an_audio_file_can_be_placed_on_a_music_lane(self) -> None:
        track_id = self.music_track()
        asset_id = self.upload(
            self.project_id, name="room.wav", data=WAV, content_type="audio/wav"
        ).json()["media_asset_id"]

        response = self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json={
                "operations": [
                    {
                        "kind": "insert",
                        "track_id": track_id,
                        "start": 2.0,
                        "end": 12.0,
                        "media_asset_id": asset_id,
                    }
                ]
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)

        timeline = self.client.get(
            f"/v1/projects/{self.project_id}/timeline", headers=self.auth()
        ).json()
        music = next(t for t in timeline["tracks"] if t["kind"] == "music")
        self.assertEqual(len(music["clips"]), 1)
        self.assertEqual(music["clips"][0]["source_kind"], "object")

    def test_no_object_key_is_published_in_the_asset_view(self) -> None:
        asset_id = self.upload(self.project_id).json()["media_asset_id"]
        detail = self.client.get(
            f"/v1/projects/{self.project_id}/media/{asset_id}", headers=self.auth()
        ).json()
        self.assertNotIn("object", detail)
        self.assertNotIn("key", str(detail.get("provenance", {})))

    def test_another_tenants_asset_id_is_not_found(self) -> None:
        track_id = self.music_track()
        _other_org, other_key = self.tenant("globex")
        other_project = self.project(other_key)
        stranger = self.upload(
            other_project,
            name="theirs.wav",
            data=WAV,
            content_type="audio/wav",
            key=other_key,
        ).json()["media_asset_id"]

        response = self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json={
                "operations": [
                    {
                        "kind": "insert",
                        "track_id": track_id,
                        "start": 2.0,
                        "end": 12.0,
                        "media_asset_id": stranger,
                    }
                ]
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 404, response.text)

    def test_a_refused_file_cannot_reach_the_timeline(self) -> None:
        track_id = self.music_track()
        library = self.library(self.project_id)
        # Upload something real, then mark it refused the way inspection would.
        asset_id = self.upload(
            self.project_id, name="room.wav", data=WAV, content_type="audio/wav"
        ).json()["media_asset_id"]
        library = self.library(self.project_id)
        library.assets = [
            a.model_copy(update={"status": a.status.__class__("refused")})
            if a.media_asset_id == asset_id
            else a
            for a in library.assets
        ]
        run(
            self.repository.put_document(
                project_id=self.project_id,
                kind=MEDIA_DOC,
                document_id=MEDIA_DOC,
                payload=library.model_dump(mode="json"),
            )
        )

        response = self.client.patch(
            f"/v1/projects/{self.project_id}/timeline",
            json={
                "operations": [
                    {
                        "kind": "insert",
                        "track_id": track_id,
                        "start": 2.0,
                        "end": 12.0,
                        "media_asset_id": asset_id,
                    }
                ]
            },
            headers=self.auth(),
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("not ready", response.text)
