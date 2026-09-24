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
class SpeechSynthesisProvider(Provider, Protocol):
    """Text to spoken audio.

    Second only to speech-to-text in privacy weight: it receives the words the
    user wrote, and a cloned voice is the user's own likeness. Selection checks
    :attr:`~vtv.ports.base.DataPolicy.is_acceptable_for_user_voice` for the same
    reason.
    """

    async def synthesize(self, request: GenerationRequest) -> GenerationResult:
        """Params are :class:`~vtv.contracts.generation.SpeechParams`.

        The result carries exactly one audio output. The adapter must report the
        real duration of what it produced; the timeline is built from it, so a
        wrong duration desynchronises the entire video.
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
class PricesRequests(Protocol):
    """A provider whose price depends on what is being asked for.

    ## Why this exists

    `ProviderCapabilities.unit_cost_usd` is one number, declared once when the
    provider is constructed, and the router uses it for two things: refusing a
    call that cannot fit the shot's ceiling, and ordering the cheapest provider
    first. That works only while a provider charges one price.

    Image generation does not. The same 16:9 shot is $0.016, $0.063 or $0.25
    depending on the fidelity the *request* names, and no single declared number
    is right for all three. Declaring the cheapest would let a `fine` shot pass
    a ceiling it will then exceed by sixteen times — the exact failure the
    price table was introduced to stop. Declaring the dearest would refuse
    `draft` shots that are affordable sixteen times over.

    So the router asks. A provider that implements this is priced per request;
    one that does not keeps its declared capability, unchanged.

    Deliberately **not** async and deliberately not part of
    :class:`ImageGenerationProvider`: it is consulted inside the router's
    candidate filter, before any decision to dispatch, and a provider that had
    to make a network call to answer "what would this cost" could not be asked
    in that loop.
    """

    def price_for(self, request: GenerationRequest) -> float:
        """What one unit of this request would cost, in US dollars.

        Must be the price that will actually be billed, or the number this
        replaces was better. Where the provider cannot know — a self-hosted
        endpoint at whatever price its operator pays — it must return its
        declared cost rather than guess low.
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
    "PricesRequests",
    "SpeechSynthesisProvider",
    "SpeechToTextProvider",
    "TextGenerationProvider",
    "VideoGenerationProvider",
]
