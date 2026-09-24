"""Licensing and provenance: the rules that protect the company.

These tests are the executable form of Rule 5. Every one of them describes a way
a media business gets itself sued, and asserts that the type system refuses it.
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from vtv.contracts import (
    Asset,
    AssetKind,
    AssetProvenance,
    AssetSource,
    License,
    ObjectRef,
    Permission,
    Status,
)


def permissive_license(**overrides: object) -> License:
    fields: dict[str, object] = {
        "spdx_id": "CC-BY-4.0",
        "name": "Creative Commons Attribution 4.0",
        "commercial_use": Permission.ALLOWED,
        "modification": Permission.ALLOWED,
        "attribution_required": True,
    }
    fields.update(overrides)
    return License(**fields)  # type: ignore[arg-type]


def provenance(license_: License, source: AssetSource = AssetSource.OPENVERSE):
    return AssetProvenance(
        source=source,
        source_id="abc-123",
        original_url="https://example.org/photo",
        title="A photograph",
        creator="A. Photographer",
        license=license_,
    )


def asset(**overrides: object) -> Asset:
    fields: dict[str, object] = {
        "kind": AssetKind.IMAGE,
        "source": AssetSource.OPENVERSE,
        "provenance": provenance(permissive_license()),
        "object": ObjectRef(bucket="b", key="k.jpg", content_type="image/jpeg"),
        "status": Status.READY,
    }
    fields.update(overrides)
    return Asset(**fields)  # type: ignore[arg-type]


class UnknownRightsFailClosed(unittest.TestCase):
    """The central commercial safety property of the whole system."""

    def test_unknown_commercial_use_is_not_usable(self) -> None:
        candidate = asset(
            provenance=provenance(
                permissive_license(commercial_use=Permission.UNKNOWN)
            )
        )
        self.assertFalse(candidate.is_commercially_usable)

    def test_unknown_modification_rights_are_not_usable(self) -> None:
        # Everything in this pipeline is cropped, graded and animated. An asset
        # we may not modify is an asset we may not use.
        candidate = asset(
            provenance=provenance(permissive_license(modification=Permission.UNKNOWN))
        )
        self.assertFalse(candidate.is_commercially_usable)

    def test_a_bare_license_defaults_to_unusable(self) -> None:
        bare = License(name="Some licence we did not parse")
        self.assertFalse(bare.is_commercially_usable)
        self.assertIs(bare.commercial_use, Permission.UNKNOWN)

    def test_explicitly_permitted_rights_are_usable(self) -> None:
        self.assertTrue(asset().is_commercially_usable)


class ScrapingIsStructurallyImpossible(unittest.TestCase):
    def test_scraped_provenance_cannot_be_constructed(self) -> None:
        with self.assertRaises(ValidationError):
            AssetProvenance(
                source=AssetSource.WEB_SCRAPE,
                source_id="whatever",
                license=permissive_license(),
            )

    def test_scraped_asset_cannot_be_constructed(self) -> None:
        with self.assertRaises(ValidationError):
            asset(source=AssetSource.WEB_SCRAPE, provenance=None)


class ProvenanceIsMandatoryForExternalMedia(unittest.TestCase):
    def test_external_asset_without_provenance_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            asset(provenance=None)

    def test_provenance_source_must_match_the_asset(self) -> None:
        with self.assertRaises(ValidationError):
            asset(
                source=AssetSource.WIKIMEDIA_COMMONS,
                provenance=provenance(permissive_license(), AssetSource.OPENVERSE),
            )

    def test_generated_assets_must_reference_their_generation(self) -> None:
        with self.assertRaises(ValidationError):
            asset(source=AssetSource.GENERATED, provenance=None)

    def test_internal_assets_need_no_provenance(self) -> None:
        internal = asset(source=AssetSource.INTERNAL_LIBRARY, provenance=None)
        self.assertTrue(internal.is_commercially_usable)


class AttributionIsProducedAsData(unittest.TestCase):
    def test_attribution_line_is_formatted_once_centrally(self) -> None:
        line = asset().attribution_line()
        self.assertEqual(line, "A photograph by A. Photographer (CC-BY-4.0)")

    def test_no_attribution_line_when_none_is_required(self) -> None:
        public_domain = asset(
            provenance=provenance(
                permissive_license(
                    spdx_id="CC0-1.0", name="CC0", attribution_required=False
                )
            )
        )
        self.assertIsNone(public_domain.attribution_line())


class ReadyAssetsHaveBytes(unittest.TestCase):
    def test_ready_requires_an_object_reference(self) -> None:
        with self.assertRaises(ValidationError):
            asset(object=None)

    def test_pending_asset_may_have_no_bytes_yet(self) -> None:
        pending = asset(object=None, status=Status.PENDING)
        self.assertIs(pending.status, Status.PENDING)


if __name__ == "__main__":
    unittest.main()
