"""Generation requests, caching, and the failure model."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from vtv.contracts import (
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

IMAGE = ObjectRef(bucket="b", key="out.png", content_type="image/png")


def image_request(prompt: str = "a silicon die, macro", **overrides: object):
    fields: dict[str, object] = {
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
            kind=GenerationKind.IMAGE,
            params=ImageParams(prompt="a silicon die, macro", seed=7),
        )
        self.assertNotEqual(image_request().cache_key(), seeded.cache_key())

    def test_kind_and_params_must_agree(self) -> None:
        with self.assertRaises(ValidationError):
            GenerationRequest(
                kind=GenerationKind.VIDEO, params=ImageParams(prompt="mismatch")
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
