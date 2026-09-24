"""Rate limiting and request bounds.

Two different jobs that are often confused:

**Rate limiting protects the service.** A token bucket per principal, so one
tenant's runaway script cannot exhaust the capacity every other tenant paid for.
It is about requests per second and it resets continuously.

**Quotas protect the business.** Minutes of video per month, against a plan.
They reset on a billing boundary and live in `vtv.billing`, not here.

A token bucket rather than a fixed window, because a fixed window lets a caller
send its whole allowance in the last millisecond of one window and again in the
first millisecond of the next — twice the intended rate, at the worst possible
moment. The bucket smooths that by construction.

The limiter is in-process, which is correct for a single node and wrong for a
fleet: two API processes each allow the full rate. The `RateLimiter` interface
is what a Redis-backed implementation would satisfy, and the limitation is
stated here rather than discovered in production.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from vtv.contracts.errors import ErrorCode, PolicyViolation
from vtv.contracts.tenancy import PlanTier
from vtv.security.shared_state import InMemorySharedStore, SharedStore


class RateLimited(PolicyViolation):
    """Too many requests. Retryable, and the caller is told when."""

    code = ErrorCode.RATE_LIMITED
    user_message = "You are making requests too quickly. Please slow down."


@dataclass(frozen=True)
class RateLimitPolicy:
    """A bucket's shape: steady rate plus how much burst is tolerated."""

    #: Sustained requests per second.
    rate_per_second: float
    #: Bucket depth. A caller may spend this many in an instant, then is held to
    #: the sustained rate. Real clients are bursty; a burst of one is hostile to
    #: legitimate use.
    burst: int

    def __post_init__(self) -> None:
        if self.rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if self.burst < 1:
            raise ValueError("burst must be at least 1")


#: Per-plan defaults. Deliberately a table rather than a formula: pricing is a
#: commercial decision that changes without notice, and a table can be changed
#: by someone who does not read Python.
PLAN_LIMITS: dict[PlanTier, RateLimitPolicy] = {
    PlanTier.FREE: RateLimitPolicy(rate_per_second=1.0, burst=5),
    PlanTier.STARTER: RateLimitPolicy(rate_per_second=5.0, burst=20),
    PlanTier.PROFESSIONAL: RateLimitPolicy(rate_per_second=20.0, burst=60),
    PlanTier.BUSINESS: RateLimitPolicy(rate_per_second=50.0, burst=150),
    PlanTier.ENTERPRISE: RateLimitPolicy(rate_per_second=200.0, burst=500),
}

#: Authentication is limited far harder than everything else, and by IP rather
#: than by principal — the whole point of a credential-stuffing attack is that
#: the attacker has no valid principal yet.
LOGIN_LIMIT = RateLimitPolicy(rate_per_second=0.1, burst=5)

#: Rendering costs real money per call, so it is limited separately from reads.
RENDER_LIMIT = RateLimitPolicy(rate_per_second=0.2, burst=3)


@dataclass(frozen=True)
class RateLimitVerdict:
    """The answer, including what the caller needs to behave well."""

    allowed: bool
    remaining: int
    #: Seconds until one more request would be allowed. Sent as `Retry-After`,
    #: which is the difference between a client that backs off and one that
    #: hammers.
    retry_after: float
    limit: int

    def raise_if_limited(self) -> None:
        if not self.allowed:
            raise RateLimited(
                f"rate limit exceeded; retry in {self.retry_after:.1f}s",
                # Carried in context so the API can set `Retry-After` without
                # parsing the message.
                context={"retry_after": self.retry_after, "limit": self.limit},
            )


@dataclass
class RateLimiter:
    """Token buckets over a store that every replica shares.

    STATUS: **EXECUTED, MULTI-PROCESS.** The buckets used to be a Python dict,
    which meant N replicas allowed N times the limit — including the
    credential-stuffing limit, so scaling out weakened authentication. The
    arithmetic now happens inside a single atomic operation in
    `vtv.security.shared_state`, so two processes racing for the last token
    produce one winner.

    The store's own limits are its business and are documented there. What
    matters here is that this class no longer holds correctness-critical state.
    """

    default: RateLimitPolicy = RateLimitPolicy(rate_per_second=10.0, burst=30)
    #: Where the buckets live. Defaults to the single-process store so a test
    #: or `make demo` needs no file, but `wiring.py` supplies the shared one.
    store: SharedStore = field(default_factory=InMemorySharedStore)
    clock: object = time.monotonic

    def check(
        self, key: str, *, policy: RateLimitPolicy | None = None, cost: float = 1.0
    ) -> RateLimitVerdict:
        """Spend ``cost`` tokens against ``key``. Does not raise."""
        policy = policy or self.default
        allowed, remaining, retry_after = self.store.spend(
            key,
            cost=cost,
            rate=policy.rate_per_second,
            burst=float(policy.burst),
            now=float(self.clock()),  # type: ignore[operator]
        )
        return RateLimitVerdict(
            allowed=allowed,
            remaining=int(max(0.0, remaining)),
            retry_after=retry_after,
            limit=policy.burst,
        )

    def require(
        self, key: str, *, policy: RateLimitPolicy | None = None, cost: float = 1.0
    ) -> RateLimitVerdict:
        verdict = self.check(key, policy=policy, cost=cost)
        verdict.raise_if_limited()
        return verdict

    def reset(self, key: str) -> None:
        """Clear one bucket. Used after a successful login, so a user who
        mistyped their password three times is not punished afterwards."""
        self.store.clear(key)


def limit_key(*parts: str | None) -> str:
    """Build a bucket key. `None` parts become `-` so keys stay well-formed."""
    return ":".join(part or "-" for part in parts)


@dataclass(frozen=True)
class RequestBounds:
    """Hard caps on a single request, enforced before the body is read.

    Checking after reading the body means the memory has already been spent,
    which is the whole attack. These are compared against `Content-Length` first
    and against the running total while streaming second, because a client may
    lie about the former.
    """

    max_body_bytes: int = 200 * 1024 * 1024
    max_json_bytes: int = 1 * 1024 * 1024
    max_field_count: int = 64
    max_header_bytes: int = 16 * 1024
    max_url_length: int = 2048
    #: Wall-clock ceiling on one request handler. A request that outlives this
    #: is holding a worker that other tenants need.
    timeout_seconds: float = 60.0

    def check_length(self, declared: int | None, *, json: bool = False) -> None:
        limit = self.max_json_bytes if json else self.max_body_bytes
        if declared is not None and declared > limit:
            raise PolicyViolation(f"request body exceeds {limit} bytes")


__all__ = [
    "LOGIN_LIMIT",
    "PLAN_LIMITS",
    "RENDER_LIMIT",
    "RateLimitPolicy",
    "RateLimitVerdict",
    "RateLimited",
    "RateLimiter",
    "RequestBounds",
    "limit_key",
]
