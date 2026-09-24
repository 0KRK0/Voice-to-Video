"""Generation requests, caching, and the failure model."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from vtv.contracts import (
    Budget,
    DegradationReason,
    DegradationStep,
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    GenerationKind,
    GenerationRequest,
    GenerationResult,
    ImageParams,
    ObjectRef,
    PolicyViolation,
    ProviderError,
    Status,
    TextParams,
    ValidationFailed,
    VideoParams,
)
from vtv.contracts.generation import (
    DEFAULT_MAX_COST_USD,
    FALLBACK_MAX_COST_USD,
)
from vtv.contracts.tenancy import SYSTEM_ORGANISATION_ID

IMAGE = ObjectRef(bucket="b", key="out.png", content_type="image/png")


def image_request(prompt: str = "a silicon die, macro", **overrides: object):
    fields: dict[str, object] = {
        "organisation_id": SYSTEM_ORGANISATION_ID,
        "kind": GenerationKind.IMAGE,
        "params": ImageParams(prompt=prompt),
    }
    fields.update(overrides)
    return GenerationRequest(**fields)  # type: ignore[arg-type]


class RequestsAreContentAddressed(unittest.TestCase):
    """Caching is where the unit economics of this product are won or lost."""

    def test_identical_creative_requests_share_a_key(self) -> None:
        a = image_request()
        b = image_request()
        self.assertNotEqual(a.request_id, b.request_id)
        self.assertEqual(a.cache_key(), b.cache_key())

    def test_the_key_ignores_scene_budget_and_provider_hint(self) -> None:
        plain = image_request()
        decorated = image_request(
            project_id="prj_aaaaaaaaaaaaaaaaaaaaaaaa",
            scene_id="scn_aaaaaaaaaaaaaaaaaaaaaaaa",
            provider_hint="some-vendor",
        )
        self.assertEqual(plain.cache_key(), decorated.cache_key())

    def test_a_different_prompt_is_a_different_key(self) -> None:
        self.assertNotEqual(
            image_request("a").cache_key(), image_request("b").cache_key()
        )

    def test_a_different_seed_is_a_different_key(self) -> None:
        seeded = GenerationRequest(
            organisation_id=SYSTEM_ORGANISATION_ID,
            kind=GenerationKind.IMAGE,
            params=ImageParams(prompt="a silicon die, macro", seed=7),
        )
        self.assertNotEqual(image_request().cache_key(), seeded.cache_key())

    def test_kind_and_params_must_agree(self) -> None:
        with self.assertRaises(ValidationError):
            GenerationRequest(
                organisation_id=SYSTEM_ORGANISATION_ID,
                kind=GenerationKind.VIDEO, params=ImageParams(prompt="mismatch")
            )


class EveryRequestCarriesACostCeiling(unittest.TestCase):
    """No request can exist with an open-ended cost.

    It could, and did: `budget` defaulted to `Budget()`, whose `max_cost_usd` is
    None, and both of the router's budget checks are `is not None`-guarded. A
    request built without a budget — which is what the composer built for every
    generated shot — could not be refused whatever it cost.
    """

    def test_a_request_built_without_a_budget_is_not_unbounded(self) -> None:
        self.assertIsNotNone(image_request().budget.max_cost_usd)

    def test_the_backstop_is_the_one_declared_for_the_kind(self) -> None:
        self.assertEqual(
            image_request().budget.max_cost_usd,
            DEFAULT_MAX_COST_USD[GenerationKind.IMAGE],
        )
        video = GenerationRequest(
            organisation_id=SYSTEM_ORGANISATION_ID,
            kind=GenerationKind.VIDEO,
            params=VideoParams(prompt="a wave breaking"),
        )
        self.assertEqual(
            video.budget.max_cost_usd, DEFAULT_MAX_COST_USD[GenerationKind.VIDEO]
        )

    def test_every_kind_has_a_backstop_or_inherits_the_cheapest_one(self) -> None:
        # A kind added without a price must not become the unbounded one.
        for kind in GenerationKind:
            with self.subTest(kind=kind):
                self.assertLessEqual(
                    DEFAULT_MAX_COST_USD.get(kind, FALLBACK_MAX_COST_USD), 3.0
                )

    def test_a_stated_ceiling_is_never_overwritten(self) -> None:
        request = image_request(budget=Budget(max_cost_usd=0.01))
        self.assertEqual(request.budget.max_cost_usd, 0.01)

    def test_a_latency_only_budget_still_gets_a_cost_ceiling(self) -> None:
        """The Director's per-scene budget is latency-bounded but may be
        cost-open when the project itself has no ceiling."""
        request = image_request(budget=Budget(max_latency_seconds=180.0))
        self.assertEqual(request.budget.max_latency_seconds, 180.0)
        self.assertIsNotNone(request.budget.max_cost_usd)

    def test_the_backstop_does_not_disturb_the_cache_key(self) -> None:
        # Budgets are excluded from the key by design: two scenes that want the
        # same image pay for it once even at different ceilings.
        self.assertEqual(
            image_request().cache_key(),
            image_request(budget=Budget(max_cost_usd=0.01)).cache_key(),
        )


class TextGenerationIsAlwaysStructured(unittest.TestCase):
    def test_a_response_schema_can_be_demanded_by_name(self) -> None:
        params = TextParams(
            instruction="Extract the semantic units.",
            input_json={"transcript": "..."},
            response_schema="understanding",
        )
        self.assertEqual(params.response_schema, "understanding")

    def test_video_duration_is_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            VideoParams(prompt="a very long film", duration_seconds=600)


class ResultsAreHonest(unittest.TestCase):
    def test_a_ready_result_must_carry_outputs_and_a_provider(self) -> None:
        key = image_request().cache_key()
        with self.assertRaises(ValidationError):
            GenerationResult(
                request_id="gen_aaaaaaaaaaaaaaaaaaaaaaaa",
                cache_key=key,
                status=Status.READY,
            )
        with self.assertRaises(ValidationError):
            GenerationResult(
                request_id="gen_aaaaaaaaaaaaaaaaaaaaaaaa",
                cache_key=key,
                status=Status.READY,
                outputs=[IMAGE],
            )
        ok = GenerationResult(
            request_id="gen_aaaaaaaaaaaaaaaaaaaaaaaa",
            cache_key=key,
            status=Status.READY,
            outputs=[IMAGE],
            provider="a-provider",
            cost_usd=0.04,
            latency_ms=11_200,
        )
        self.assertEqual(ok.cost_usd, 0.04)

    def test_a_failed_result_must_say_why(self) -> None:
        with self.assertRaises(ValidationError):
            GenerationResult(
                request_id="gen_aaaaaaaaaaaaaaaaaaaaaaaa",
                cache_key=image_request().cache_key(),
                status=Status.FAILED,
            )

    def test_a_cached_result_may_not_be_charged_for(self) -> None:
        with self.assertRaises(ValidationError):
            GenerationResult(
                request_id="gen_aaaaaaaaaaaaaaaaaaaaaaaa",
                cache_key=image_request().cache_key(),
                status=Status.READY,
                outputs=[IMAGE],
                provider="a-provider",
                from_cache=True,
                cost_usd=0.04,
            )


class TheErrorModelDrivesBehaviour(unittest.TestCase):
    def test_retryability_follows_from_category(self) -> None:
        transient = ErrorInfo.of(
            ErrorCode.PROVIDER_UNAVAILABLE,
            ErrorCategory.PROVIDER,
            "upstream returned 503",
        )
        permanent = ErrorInfo.of(
            ErrorCode.ASSET_LICENSE_UNACCEPTABLE,
            ErrorCategory.POLICY,
            "licence forbids commercial use",
        )
        self.assertTrue(transient.retryable)
        self.assertFalse(permanent.retryable)

    def test_exceptions_carry_serialisable_information(self) -> None:
        error = ProviderError("connection reset", context={"attempt": 2})
        self.assertTrue(error.retryable)
        self.assertIs(error.info.category, ErrorCategory.PROVIDER)
        self.assertIn("attempt", error.info.context)

    def test_policy_violations_are_not_retried(self) -> None:
        self.assertFalse(PolicyViolation("share-alike not accepted").retryable)

    def test_validation_failures_are_not_retried_blindly(self) -> None:
        self.assertFalse(ValidationFailed("model returned an unknown field").retryable)

    def test_user_messages_are_separate_from_engineer_messages(self) -> None:
        error = ProviderError("HTTP 503 from vendor-x pool eu-west-1")
        self.assertIsNotNone(error.info.user_message)
        self.assertNotIn("vendor-x", error.info.user_message or "")

    def test_status_terminality_and_usability(self) -> None:
        self.assertTrue(Status.READY.is_usable)
        self.assertFalse(Status.RETRYING.is_terminal)
        self.assertTrue(Status.FAILED.is_terminal)
        self.assertFalse(Status.FAILED.is_usable)

    def test_degradation_is_recorded_as_data(self) -> None:
        step = DegradationStep(
            from_strategy="generated_video",
            to_strategy="generated_image",
            reason=DegradationReason.BUDGET_EXCEEDED,
        )
        self.assertIs(step.reason, DegradationReason.BUDGET_EXCEEDED)


if __name__ == "__main__":
    unittest.main()
