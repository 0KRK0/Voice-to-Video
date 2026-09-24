"""The licensed-media rung, driven through the *real* `AssetResolver`.

## Why this file exists

`SceneComposer._attempt` called `AssetResolver.resolve` without
`organisation_id`, which that method requires. The result was a `TypeError` —
not a `VTVError`, so `_realise`'s handler did not catch it, and a scene the
Director sent to the commons failed the entire composition instead of
descending its ladder.

Two things hid it for as long as it existed:

* the field was declared `asset_resolver: object | None`, and the call carried
  `# type: ignore[attr-defined]`, so no checker ever read the call site;
* every test that reached this rung supplied a double declared
  `async def resolve(self, **_: object)`, which accepts any arguments at all.

A test double more permissive than the thing it stands in for cannot fail the
way the real object fails. So these tests use `AssetResolver` itself, and fake
only what is genuinely outside the process — the search API and the HTTP
fetch.
"""

from __future__ import annotations

import asyncio
import unittest

from vtv.contracts.asset import (
    AssetKind,
    AssetProvenance,
    AssetSource,
    License,
    Permission,
)
from vtv.contracts.base import Budget, ObjectRef, RetentionClass, TimeSpan
from vtv.contracts.errors import NotFound
from vtv.contracts.scene import Scene, ScenePurpose, VisualGoal
from vtv.contracts.timeline import AssetClipSource
from vtv.contracts.visual_plan import (
    CostEstimate,
    LicensedMediaRequirements,
    SceneVisualPlan,
    VisualDirective,
    VisualStrategy,
)
from vtv.observability.events import EventSink
from vtv.pipeline.assets import AssetResolver
from vtv.pipeline.composition import SceneComposer
from vtv.ports.assets import AssetCandidate


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


ORG = "org_0000000000000000000000"
PROJECT = "prj_0000000000000000000001"


class MemoryStorage:
    """Just enough of `StorageProvider` for the resolver to store one asset."""

    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        retention: RetentionClass = RetentionClass.PROJECT,
    ) -> ObjectRef:
        self.written[key] = data
        return ObjectRef(bucket="test", key=key, content_type=content_type)


class StubSearch:
    """One openly-licensed photograph, however it is asked for."""

    name = "stub-commons"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def search(
        self, *, query: str, kind: object, constraints: object, limit: int
    ) -> list[AssetCandidate]:
        del kind, constraints, limit
        self.queries.append(query)
        # The title echoes the query, because that is what a search engine
        # returns for a *good* hit and the resolver now ranks on exactly that.
        # A double whose titles were unrelated to their queries was a double
        # modelling only the failures — and it hid the fact that nothing was
        # ranking at all.
        return [
            AssetCandidate(
                kind=AssetKind.IMAGE,
                title=f"A photograph of {query}",
                download_url="https://example.org/photo.jpg",
                provenance=AssetProvenance(
                    source=AssetSource.OPENVERSE,
                    source_id="stub-1",
                    original_url="https://example.org/photo",
                    title=f"A photograph of {query}",
                    creator="A. Photographer",
                    license=License(
                        spdx_id="CC-BY-4.0",
                        name="Creative Commons Attribution 4.0",
                        commercial_use=Permission.ALLOWED,
                        modification=Permission.ALLOWED,
                        attribution_required=True,
                    ),
                ),
            )
        ]


class StubFetcher:
    async def fetch(self, url: str) -> tuple[bytes, str]:
        del url
        return b"\xff\xd8\xff\xe0jpeg-ish", "image/jpeg"


def scene() -> Scene:
    return Scene(
        index=0,
        span=TimeSpan.of(0.0, 3.0),
        narration="JavaScript is a typed language now.",
        purpose=ScenePurpose.EXPLANATION,
        visual_goal=VisualGoal.SHOW_ENTITY,
        visual_brief="show someone writing typed JavaScript",
    )


def directive(query: str = "javascript source code on a screen") -> VisualDirective:
    return VisualDirective(
        strategy=VisualStrategy.LICENSED_MEDIA,
        requirements=LicensedMediaRequirements(query=query),
        rationale="a real photograph is cheaper and more truthful here",
        estimate=CostEstimate(usd=0.0, latency_seconds=2.0),
        confidence=0.6,
    )


class TheCommonsRungRunsAtAll(unittest.TestCase):
    """The defect itself: the call had to be *made* correctly, not just typed."""

    def setUp(self) -> None:
        self.search = StubSearch()
        self.storage = MemoryStorage()
        self.resolver = AssetResolver(
            storage=self.storage,
            events=EventSink(),
            providers=[self.search],
            fetcher=StubFetcher(),
        )
        self.composer = SceneComposer(
            storage=self.storage, events=EventSink(), asset_resolver=self.resolver
        )

    def test_a_licensed_media_directive_produces_an_asset_clip(self) -> None:
        clip, assets = run(
            self.composer._attempt(
                scene(),
                directive(),
                TimeSpan.of(0.0, 3.0),
                organisation_id=ORG,
                project_id=PROJECT,
                budget=Budget(max_cost_usd=0.1),
            )
        )
        self.assertIsNotNone(clip)
        assert clip is not None
        self.assertIsInstance(clip.source, AssetClipSource)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].source, AssetSource.OPENVERSE)

    def test_the_search_is_asked_for_what_the_directive_wanted(self) -> None:
        run(
            self.composer._attempt(
                scene(),
                directive("a diagram of a compiler"),
                TimeSpan.of(0.0, 3.0),
                organisation_id=ORG,
                project_id=PROJECT,
                budget=Budget(max_cost_usd=0.1),
            )
        )
        self.assertIn("a diagram of a compiler", self.search.queries)

    def test_the_asset_is_stored_under_the_calling_tenant(self) -> None:
        """The argument that was missing is the one that isolates tenants.

        It is not an incidental parameter: `tenant_key` builds the storage key
        from it, so an omitted `organisation_id` would not merely have failed —
        had a default existed, it would have written one customer's media into
        another customer's prefix.
        """
        run(
            self.composer._attempt(
                scene(),
                directive(),
                TimeSpan.of(0.0, 3.0),
                organisation_id=ORG,
                project_id=PROJECT,
                budget=Budget(max_cost_usd=0.1),
            )
        )
        self.assertTrue(self.storage.written)
        for key in self.storage.written:
            self.assertIn(ORG, key)
            self.assertIn(PROJECT, key)


class NothingUsableDescendsTheLadder(unittest.TestCase):
    """A commons with no match is an ordinary outcome, not a failure."""

    def test_the_ladder_reaches_typography_when_the_search_is_empty(self) -> None:
        class Empty:
            name = "empty"

            async def search(self, **_: object) -> list[AssetCandidate]:
                return []

        resolver = AssetResolver(
            storage=MemoryStorage(),
            events=EventSink(),
            providers=[Empty()],
            fetcher=StubFetcher(),
        )
        composer = SceneComposer(
            storage=MemoryStorage(), events=EventSink(), asset_resolver=resolver
        )
        from vtv.contracts.visual_language import TypographySpec
        from vtv.contracts.visual_plan import ProgrammaticRequirements

        plan = SceneVisualPlan(
            scene_id=scene().scene_id,
            primary=directive(),
            fallbacks=[
                VisualDirective(
                    strategy=VisualStrategy.PROGRAMMATIC,
                    requirements=ProgrammaticRequirements(
                        spec=TypographySpec(headline="JavaScript is typed now")
                    ),
                    rationale="type always renders",
                    estimate=CostEstimate(usd=0.0, latency_seconds=0.4),
                    confidence=0.5,
                )
            ],
            budget=Budget(max_cost_usd=0.1, max_latency_seconds=60.0),
        )
        clip, _ = run(
            composer.realise(
                scene(),
                plan,
                TimeSpan.of(0.0, 3.0),
                organisation_id=ORG,
                project_id=PROJECT,
            )
        )
        from vtv.contracts.timeline import ProgrammaticClipSource

        self.assertIsInstance(clip.source, ProgrammaticClipSource)
        # And the descent is recorded rather than silent.
        self.assertTrue(clip.degradation)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class AVendorResponseCannotKillARender(unittest.TestCase):
    """The failure that reached the user, reproduced.

    Wikimedia returned a scan 16578 pixels wide. `AssetDimensions` caps width at
    16384, so building the candidate raised `ValidationError` — which is not a
    `VTVError`, so it went past `AssetResolver`'s handler, past
    `SceneComposer._realise`'s handler, and out of the `render_scope` job. Three
    attempts, dead-letter queue, and "Something went wrong on our side." in the
    Studio.

    The photograph was fine. Only a *measurement* failed a sanity bound.
    """

    def setUp(self) -> None:
        self.storage = MemoryStorage()
        self.composer = SceneComposer(
            storage=self.storage,
            events=EventSink(),
            asset_resolver=AssetResolver(
                storage=self.storage,
                events=EventSink(),
                providers=[self._exploding()],
                fetcher=StubFetcher(),
            ),
        )

    @staticmethod
    def _exploding():  # type: ignore[no-untyped-def]
        class Oversized:
            """Parses its response exactly as the real adapter used to."""

            name = "oversized-commons"

            async def search(self, **_: object) -> list[AssetCandidate]:
                from vtv.contracts.asset import AssetDimensions

                # The line that raised, unguarded, as it was written.
                AssetDimensions(width=16578, height=9000)
                raise AssertionError("unreachable: the line above raises")

        return Oversized()

    def test_the_search_failure_is_recorded_and_the_ladder_descends(self) -> None:
        from vtv.contracts.timeline import ProgrammaticClipSource
        from vtv.contracts.visual_language import TypographySpec
        from vtv.contracts.visual_plan import ProgrammaticRequirements

        plan = SceneVisualPlan(
            scene_id=scene().scene_id,
            primary=directive(),
            fallbacks=[
                VisualDirective(
                    strategy=VisualStrategy.PROGRAMMATIC,
                    requirements=ProgrammaticRequirements(
                        spec=TypographySpec(headline="JavaScript is typed now")
                    ),
                    rationale="type always renders",
                    estimate=CostEstimate(usd=0.0, latency_seconds=0.4),
                    confidence=0.5,
                )
            ],
            budget=Budget(max_cost_usd=0.1, max_latency_seconds=60.0),
        )
        clip, _ = run(
            self.composer.realise(
                scene(),
                plan,
                TimeSpan.of(0.0, 3.0),
                organisation_id=ORG,
                project_id=PROJECT,
            )
        )
        self.assertIsInstance(clip.source, ProgrammaticClipSource)

    def test_an_adapter_that_raises_outright_does_not_escape_the_ladder(self) -> None:
        """Not only pydantic. Any exception from a rung is a rung failing."""
        from vtv.contracts.timeline import ProgrammaticClipSource
        from vtv.contracts.visual_language import TypographySpec
        from vtv.contracts.visual_plan import ProgrammaticRequirements

        class Hostile:
            name = "hostile"

            async def search(self, **_: object) -> list[AssetCandidate]:
                raise KeyError("results")

        composer = SceneComposer(
            storage=MemoryStorage(),
            events=EventSink(),
            asset_resolver=AssetResolver(
                storage=MemoryStorage(),
                events=EventSink(),
                providers=[Hostile()],
                fetcher=StubFetcher(),
            ),
        )
        plan = SceneVisualPlan(
            scene_id=scene().scene_id,
            primary=directive(),
            fallbacks=[
                VisualDirective(
                    strategy=VisualStrategy.PROGRAMMATIC,
                    requirements=ProgrammaticRequirements(
                        spec=TypographySpec(headline="fallback")
                    ),
                    rationale="type always renders",
                    estimate=CostEstimate(usd=0.0, latency_seconds=0.4),
                    confidence=0.5,
                )
            ],
            budget=Budget(max_cost_usd=0.1, max_latency_seconds=60.0),
        )
        clip, _ = run(
            composer.realise(
                scene(),
                plan,
                TimeSpan.of(0.0, 3.0),
                organisation_id=ORG,
                project_id=PROJECT,
            )
        )
        self.assertIsInstance(clip.source, ProgrammaticClipSource)


class OversizedMetadataIsDroppedNotFatal(unittest.TestCase):
    """The adapter keeps the photograph and forgets the measurement."""

    def test_a_scan_too_wide_for_the_contract_still_yields_a_candidate(self) -> None:
        from vtv.adapters.assets.openverse import dimensions_or_none

        self.assertIsNone(dimensions_or_none(16578, 9000))
        self.assertIsNotNone(dimensions_or_none(1920, 1080))

    def test_the_real_parser_survives_the_response_that_broke_it(self) -> None:
        from vtv.adapters.assets.openverse import OpenverseAssetSearchProvider
        from vtv.contracts.visual_plan import MediaSearchConstraints

        payload = {
            "results": [
                {
                    "id": "abc",
                    "url": "https://example.org/huge.jpg",
                    "license": "by",
                    "license_version": "4.0",
                    "width": 16578,
                    "height": 9000,
                    "title": "A very large scan",
                    "creator": "Someone",
                }
            ]
        }
        candidates = OpenverseAssetSearchProvider(endpoint="https://x").parse(
            payload, constraints=MediaSearchConstraints(), limit=8
        )
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0].dimensions)


class WikimediaSpeaksMediaWikiNotOpenverse(unittest.TestCase):
    """It only ever overrode `parse`, and inherited Openverse's request.

    Against `https://commons.wikimedia.org/w/api.php` the inherited `search`
    built `https://commons.wikimedia.org/w/api.php/images/?q=…`, which is not an
    endpoint. MediaWiki answers 301, the redirect lands on HTML, and
    `response.json()` raises `JSONDecodeError`. **Every Wikimedia search this
    system ever made failed**, behind an `asset.rejected` event that read like
    an outage.

    Inheriting the HTTP call and overriding only the parsing was the mistake:
    the request shape and the response shape are one decision, and splitting
    them across a subclass boundary let the parser be correct for a request
    that was never sent.
    """

    def test_it_no_longer_inherits_the_openverse_request(self) -> None:
        from vtv.adapters.assets.openverse import (
            OpenverseAssetSearchProvider,
            WikimediaAssetSearchProvider,
        )

        self.assertIn("search", WikimediaAssetSearchProvider.__dict__)
        self.assertIsNot(
            WikimediaAssetSearchProvider.search,
            OpenverseAssetSearchProvider.search,
        )

    def test_it_calls_the_api_root_with_mediawiki_parameters(self) -> None:
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlparse

        from vtv.adapters.assets.openverse import WikimediaAssetSearchProvider
        from vtv.contracts.visual_plan import MediaSearchConstraints

        seen: list[tuple[str, dict]] = []

        class Api(BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                pass

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                seen.append(
                    (parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()})
                )
                body = _json.dumps({"query": {"pages": {}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Api)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            provider = WikimediaAssetSearchProvider(
                endpoint=f"http://{host}:{port}/w/api.php"
            )
            run(
                provider.search(
                    query="hands typing on a keyboard",
                    constraints=MediaSearchConstraints(),
                    limit=8,
                )
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(len(seen), 1)
        path, params = seen[0]
        # The API root itself, not a path appended to it.
        self.assertEqual(path, "/w/api.php")
        self.assertEqual(params["action"], "query")
        self.assertEqual(params["format"], "json")
        self.assertEqual(params["generator"], "search")
        # Namespace 6 is File:. Without it the search returns articles, which
        # carry no `imageinfo` and parse to nothing.
        self.assertEqual(params["gsrnamespace"], "6")
        self.assertIn("keyboard", params["gsrsearch"])
        self.assertIn("extmetadata", params["iiprop"])
        # And none of Openverse's vocabulary, which MediaWiki does not know.
        self.assertNotIn("q", params)
        self.assertNotIn("license_type", params)


class TheFetcherIdentifiesItself(unittest.TestCase):
    """`upload.wikimedia.org` answers 403 without a User-Agent.

    Their CDN serves a large share of what Openverse indexes, so without this
    the commons rung found correctly-licensed photographs and then failed to
    download them — reported as `asset_not_found`, which was true of the
    outcome and wrong about the cause.
    """

    def test_a_user_agent_is_sent_on_every_download(self) -> None:
        import inspect

        from vtv.adapters.assets import http_fetcher

        source = inspect.getsource(http_fetcher.HttpFetcher.fetch)
        self.assertIn("User-Agent", source)
        self.assertTrue(http_fetcher.USER_AGENT.startswith("VoiceToVideo/"))
        # Wikimedia's policy asks for a contact or project URL, not just a name.
        self.assertIn("http", http_fetcher.USER_AGENT)


def _titled(title: str, index: int) -> AssetCandidate:
    """A clearly-licensed candidate that says only what its title says."""
    return AssetCandidate(
        kind=AssetKind.IMAGE,
        title=title,
        download_url=f"https://example.org/{index}.jpg",
        provenance=AssetProvenance(
            source=AssetSource.OPENVERSE,
            source_id=f"stub-{index}",
            original_url=f"https://example.org/{index}",
            title=title,
            creator="A. Photographer",
            license=License(
                spdx_id="CC0-1.0",
                name="CC0 1.0",
                commercial_use=Permission.ALLOWED,
                modification=Permission.ALLOWED,
                attribution_required=False,
                share_alike=False,
            ),
        ),
    )


class TheBestCandidateWinsNotTheFirst(unittest.TestCase):
    """The defect that shipped a gold rank insignia for "person directing AI".

    `resolve` used to store-and-return inside its own search loop, so the asset
    in the video was the first hit with an acceptable licence that downloaded.
    Nothing was ever compared with anything.
    """

    def provider(self, titles: list[str]) -> object:
        class Ranked:
            name = "ranked"

            async def search(self, *, query, kind, constraints, limit):  # type: ignore[no-untyped-def]
                del kind, constraints, limit
                return [_titled(title, i) for i, title in enumerate(titles)]

        return Ranked()

    def resolve(self, titles: list[str], query: str):  # type: ignore[no-untyped-def]
        storage = MemoryStorage()
        resolver = AssetResolver(
            storage=storage,
            events=EventSink(),
            providers=[self.provider(titles)],
            fetcher=StubFetcher(),
        )
        return run(
            resolver.resolve(
                organisation_id=ORG,
                project_id=PROJECT,
                scene_id=scene().scene_id,
                requirements=LicensedMediaRequirements(
                    query=query
                ),
            )
        )

    def test_the_relevant_hit_is_chosen_over_the_first_one(self) -> None:
        """Both were on offer. Only one is a picture of the query."""
        asset = self.resolve(
            [
                "Müllsammelaktion am Hofer Hauptbahnhof 20230513 HOF02547 RAW-Export.png",
                "A person clicking a computer mouse at a desk",
            ],
            "person clicking on computer",
        )
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertIn("clicking", (asset.description or "").lower())

    def test_a_pool_of_nothing_relevant_resolves_to_nothing(self) -> None:
        """So the ladder descends and draws it, which is the honest answer.

        The old loop would have downloaded and shipped the first of these.
        """
        asset = self.resolve(
            [
                "AM Mural crown.jpg",
                "Central West Livestock Exchange near Forbes, NSW 01.jpg",
            ],
            "person directing AI",
        )
        self.assertIsNone(asset)

    def test_a_named_persons_photograph_is_never_chosen(self) -> None:
        asset = self.resolve(
            ["Elin Wieslander at SXSW 2025 03 (cropped).jpg"], "person talking"
        )
        self.assertIsNone(asset)

    def test_only_the_winner_is_downloaded(self) -> None:
        """Ranking before fetching turns n downloads into one. The old loop
        paid for the bytes of everything it tried, including a 13 MB TIFF it
        kept and should not have."""
        fetched: list[str] = []

        class Counting(StubFetcher):
            async def fetch(self, url: str):  # type: ignore[no-untyped-def]
                fetched.append(url)
                return await StubFetcher.fetch(self, url)

        resolver = AssetResolver(
            storage=MemoryStorage(),
            events=EventSink(),
            providers=[
                self.provider(
                    [
                        "A red bicycle in a street",
                        "A red bicycle leaning on a wall",
                        "A red bicycle by a canal",
                    ]
                )
            ],
            fetcher=Counting(),
        )
        run(
            resolver.resolve(
                organisation_id=ORG,
                project_id=PROJECT,
                scene_id=scene().scene_id,
                requirements=LicensedMediaRequirements(
                    query="red bicycle"
                ),
            )
        )
        self.assertEqual(len(fetched), 1)


class WhenTheAgentsPickWillNotDownload(unittest.TestCase):
    """A commons entry can be indexed and still 404. One real render's judged
    pick did exactly that, and the resolver fell through to its own word-overlap
    ranking — which shipped a photograph of a Space Force officer at a podium
    under a line about "systems to accomplish it".

    That is the behaviour the pre-selection parameter was added to remove,
    reappearing on the failure path.
    """

    def resolver(self, titles: list[str], fetcher: object) -> AssetResolver:
        class Ranked:
            name = "ranked"

            async def search(self, *, query, kind, constraints, limit):  # type: ignore[no-untyped-def]
                del kind, constraints, limit
                return [_titled(t, i) for i, t in enumerate(titles)]

        return AssetResolver(
            storage=MemoryStorage(),
            events=EventSink(),
            providers=[Ranked()],
            fetcher=fetcher,  # type: ignore[arg-type]
        )

    def test_it_descends_rather_than_shipping_something_unendorsed(self) -> None:
        class Missing:
            async def fetch(self, url: str):  # type: ignore[no-untyped-def]
                raise NotFound("404")

        asset = run(
            self.resolver(["A person at a computer"], Missing()).resolve(
                organisation_id=ORG,
                project_id=PROJECT,
                scene_id=scene().scene_id,
                requirements=LicensedMediaRequirements(query="anything at all here"),
                chosen=_titled("The agent's choice", 99),
            )
        )
        self.assertIsNone(asset)

    def test_a_working_pick_is_still_used(self) -> None:
        asset = run(
            self.resolver([], StubFetcher()).resolve(
                organisation_id=ORG,
                project_id=PROJECT,
                scene_id=scene().scene_id,
                requirements=LicensedMediaRequirements(query="anything at all here"),
                chosen=_titled("The agent's choice", 7),
            )
        )
        self.assertIsNotNone(asset)


class TheSameFileIsConsideredOnce(unittest.TestCase):
    """Several queries per shot means one popular file comes back several times.

    Left in, it wastes a download attempt on a repeat — one render fetched the
    same 404 twice in a row, burning two of its three attempts on one missing
    file — and lets one file occupy several places in the ranking.
    """

    def test_duplicates_are_collapsed_by_download_url(self) -> None:
        from vtv.pipeline.assets import _once_each

        one = _titled("A red bicycle", 1)
        pool = [("red bicycle", one), ("a bicycle", one), ("bicycle red", _titled("Another", 2))]
        self.assertEqual(len(_once_each(pool)), 2)

    def test_the_first_query_that_found_it_is_the_one_kept(self) -> None:
        """Queries arrive in the reader's order of preference, so the earliest
        is the most considered."""
        from vtv.pipeline.assets import _once_each

        one = _titled("A red bicycle", 1)
        kept = _once_each([("best query", one), ("worse query", one)])
        self.assertEqual(kept[0][0], "best query")
