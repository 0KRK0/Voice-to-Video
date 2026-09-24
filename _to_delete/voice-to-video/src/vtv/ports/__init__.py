"""Ports: every interface between the core and the outside world.

Nothing in this package imports a vendor SDK, an HTTP client, a database driver
or a cloud library. These are pure interface definitions plus the small value
types they exchange. Adapters that *do* import those things arrive in later
stages and live in their own package, depending on this one.

The rule is enforced mechanically by ``tests/test_architecture_boundaries.py``,
which fails the build if a forbidden import appears here or in
:mod:`vtv.contracts`. An architecture principle that is only written down is a
principle that will be violated within a quarter.

Ports defined here:

===========================  ==============================================
``StorageProvider``          object storage: bytes in, references out
``SpeechToTextProvider``     voice to timestamped transcript
``TextGenerationProvider``   structured reasoning over contracts
``ImageGenerationProvider``  still image generation
``VideoGenerationProvider``  moving image generation
``AssetSearchProvider``      licensed and open media search
``Renderer``                 timeline to MP4
``JobQueue``                 background work
===========================  ==============================================
"""

from __future__ import annotations

from vtv.ports.ai import (
    ImageGenerationProvider,
    SpeechToTextProvider,
    TextGenerationProvider,
    VideoGenerationProvider,
)
from vtv.ports.assets import AssetCandidate, AssetSearchProvider
from vtv.ports.base import (
    DataPolicy,
    HealthStatus,
    Provider,
    ProviderCapabilities,
    ProviderHealth,
)
from vtv.ports.jobs import JobHandle, JobPriority, JobQueue
from vtv.ports.rendering import Renderer
from vtv.ports.storage import StorageProvider

#: Every port, used by the boundary test and by the dependency-injection
#: container that arrives in Stage 15.
ALL_PORTS: tuple[type, ...] = (
    StorageProvider,
    SpeechToTextProvider,
    TextGenerationProvider,
    ImageGenerationProvider,
    VideoGenerationProvider,
    AssetSearchProvider,
    Renderer,
    JobQueue,
)

__all__ = [
    "ALL_PORTS",
    "AssetCandidate",
    "AssetSearchProvider",
    "DataPolicy",
    "HealthStatus",
    "ImageGenerationProvider",
    "JobHandle",
    "JobPriority",
    "JobQueue",
    "Provider",
    "ProviderCapabilities",
    "ProviderHealth",
    "Renderer",
    "SpeechToTextProvider",
    "StorageProvider",
    "TextGenerationProvider",
    "VideoGenerationProvider",
]
