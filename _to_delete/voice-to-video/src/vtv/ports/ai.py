"""Ports for every AI capability the system uses.

They are gathered in one module because they share one shape: take a
:class:`~vtv.contracts.generation.GenerationRequest`, return a
:class:`~vtv.contracts.generation.GenerationResult`. That uniformity is what
makes a router, a cache, a retry policy and a fallback ladder possible to write
once instead of four times.

What an adapter is responsible for:

* translating our provider-independent params into its vendor's API,
* translating the response back into our contract,
* reporting cost and latency **honestly**, including on failure,
* raising :class:`~vtv.contracts.errors.VTVError` subclasses, never leaking a
  vendor exception type upward,
* validating structured output against the requested schema before returning it.

What an adapter must never do: retry (the router owns that), cache (the router
owns that), choose a different model than the one it advertises, or decide that a
request is unimportant enough to silently degrade.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from vtv.contracts.generation import GenerationRequest, GenerationResult
from vtv.ports.base import Provider


@runtime_checkable
class SpeechToTextProvider(Provider, Protocol):
    """Audio to timestamped text.

    The most privacy-sensitive provider in the system: it receives raw user
    voice. Selection must check
    :attr:`~vtv.ports.base.DataPolicy.is_acceptable_for_user_voice`.
    """

    async def transcribe(self, request: GenerationRequest) -> GenerationResult:
        """Params are :class:`~vtv.contracts.generation.SpeechToTextParams`.

        ``structured_output`` on the result carries a document validating
        against the exported ``transcript`` schema.
        """
        ...


@runtime_checkable
class TextGenerationProvider(Provider, Protocol):
    """Structured reasoning: understanding, scene planning, visual direction.

    Note the absence of a "chat" method. Every call in this system asks for a
    structured document that must validate against a named contract. There is no
    place in the architecture where free-form model prose is consumed directly
    (Rule 2).
    """

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Params are :class:`~vtv.contracts.generation.TextParams`.

        The adapter must validate the response against
        ``params.response_schema`` and raise
        :class:`~vtv.contracts.errors.ValidationFailed` rather than returning
        something malformed.
        """
        ...


@runtime_checkable
class ImageGenerationProvider(Provider, Protocol):
    async def generate_image(self, request: GenerationRequest) -> GenerationResult:
        """Params are :class:`~vtv.contracts.generation.ImageParams`.

        Outputs are stored via the storage port before the result is returned;
        the result carries object references, never raw bytes or a vendor URL
        that will expire.
        """
        ...


@runtime_checkable
class VideoGenerationProvider(Provider, Protocol):
    async def generate_video(self, request: GenerationRequest) -> GenerationResult:
        """Params are :class:`~vtv.contracts.generation.VideoParams`.

        These calls take minutes, not seconds. Adapters own their polling and
        must respect ``request.budget.max_latency_seconds`` by giving up and
        raising :class:`~vtv.contracts.errors.TimeoutExceeded`, so the fallback
        ladder can proceed rather than the project hanging.
        """
        ...


__all__ = [
    "ImageGenerationProvider",
    "SpeechToTextProvider",
    "TextGenerationProvider",
    "VideoGenerationProvider",
]
