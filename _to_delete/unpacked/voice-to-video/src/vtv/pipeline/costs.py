"""Stage 17 — Cost accounting.

Every operation that spends money records what it spent, on which provider, for
which scene, and whether it was served from cache. Without that, "prefer the
cheaper visual" is a slogan; with it, it is a measurement.

The ledger is deliberately append-only and in-memory per project. It is small —
a few dozen entries — and gets persisted with the project, so unit economics can
be reconstructed for any past render.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from vtv.contracts.base import VTVModel, utc_now
from vtv.contracts.generation import GenerationKind


class CostEntry(VTVModel):
    """One spend, attributable to a scene and a provider."""

    kind: GenerationKind
    provider: str
    model: str | None = None
    scene_id: str | None = None
    usd: float = 0.0
    latency_ms: int = 0
    from_cache: bool = False
    succeeded: bool = True
    at: object = None

    def model_post_init(self, _context: object) -> None:
        if self.at is None:
            object.__setattr__(self, "at", utc_now())


@dataclass
class CostLedger:
    """Append-only record of what a project cost."""

    entries: list[CostEntry] = field(default_factory=list)

    def record(self, entry: CostEntry) -> None:
        self.entries.append(entry)

    @property
    def total_usd(self) -> float:
        return round(sum(entry.usd for entry in self.entries), 6)

    @property
    def total_latency_ms(self) -> int:
        return sum(entry.latency_ms for entry in self.entries)

    @property
    def cache_hit_rate(self) -> float:
        """Cache hits over all requests. The single most load-bearing number in
        the unit economics: it is the difference between paying once per idea
        and paying once per render."""
        if not self.entries:
            return 0.0
        return round(
            sum(1 for entry in self.entries if entry.from_cache) / len(self.entries), 4
        )

    @property
    def failure_rate(self) -> float:
        if not self.entries:
            return 0.0
        return round(
            sum(1 for entry in self.entries if not entry.succeeded) / len(self.entries), 4
        )

    def by_provider(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for entry in self.entries:
            totals[entry.provider] += entry.usd
        return {key: round(value, 6) for key, value in sorted(totals.items())}

    def by_kind(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for entry in self.entries:
            totals[entry.kind.value] += entry.usd
        return {key: round(value, 6) for key, value in sorted(totals.items())}

    def by_scene(self) -> dict[str, float]:
        totals: dict[str, float] = defaultdict(float)
        for entry in self.entries:
            if entry.scene_id:
                totals[entry.scene_id] += entry.usd
        return {key: round(value, 6) for key, value in sorted(totals.items())}

    def summary(self) -> dict[str, object]:
        return {
            "total_usd": self.total_usd,
            "requests": len(self.entries),
            "cache_hit_rate": self.cache_hit_rate,
            "failure_rate": self.failure_rate,
            "by_provider": self.by_provider(),
            "by_kind": self.by_kind(),
            "total_latency_ms": self.total_latency_ms,
        }


__all__ = ["CostEntry", "CostLedger"]
