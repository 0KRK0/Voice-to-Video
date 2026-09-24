"""Where a job's compute happens, and who pays for it.

## The business decision this encodes

Rendering is the only part of this product whose marginal cost scales with the
length of the customer's video. Everything else — orchestration, project state,
asset indexes, the Visual Director's decisions — is cheap and roughly constant.
So *where the frames get composed* is not an implementation detail; it is the
line between two different companies:

    browser  →  our servers compose 60 minutes of 1080p  →  we pay per minute
    desktop  →  the customer's machine composes it       →  they already paid

The second is not merely cheaper. It is faster for the customer — a laptop's
eight cores are not shared with anyone — and it removes the ceiling on project
length, because nobody is metering the machine.

## Why this exists before a desktop app does

Because retrofitting it is the expensive version. Every stage above the
renderer — script, Visual Director, visual units, assets, timeline — is already
indifferent to where the encode happens, and the way to keep it that way is to
name the concept now and make the renderer the only thing that reads it. A
`RenderJob` says *what* to produce; an `ExecutionPolicy` says *where*; nothing
in between is allowed to care.

## The honesty rule, which is the whole point

**A target with no backend registered for it is never offered and never
chosen.** Not greyed out, not "coming soon", not silently falling back while
the interface claims otherwise — absent, with a reason a person can read.

This is the same rule as `treatment.Capabilities.from_router`: the Visual
Director asks the provider registry what exists rather than trusting a config
file, so a deployment with no image provider makes videos out of charts and
type instead of failing. The same shape applies here, for the same reason. A
"⚡ Fast — GPU" button on a build with no GPU compositor is a lie the user
discovers by waiting.

Today exactly one backend is registered, and `available()` says so. When a GPU
compositor is written it registers itself and the option appears — with no
change to the interface, the policy, or anything upstream.

## Why AUTO and HYBRID are not targets

The obvious enum is `LOCAL_CPU | LOCAL_GPU | CLOUD_CPU | CLOUD_GPU | HYBRID |
AUTO`, and it is wrong in a way that costs later. Four of those name a *place
work runs*. `AUTO` names a **policy** — "you choose" — and `HYBRID` names a
**split** — "these stages there, those stages here". A field typed as that enum
can hold `AUTO`, which means every reader has to handle a value that is not a
place, and the ones that forget will treat "you choose" as a location.

So: `ExecutionTarget` is a place. `ExecutionPolicy` carries a preference, which
may be a place, or `AUTO`, and resolves to a place. Hybrid is expressed as a
policy whose stages resolve independently — which is what it actually is, and
which is why it needs no enum member of its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum


class Machine(str, Enum):
    """Whose computer the work runs on."""

    #: The customer's own machine — the desktop engine.
    LOCAL = "local"
    #: Ours.
    CLOUD = "cloud"


class Processor(str, Enum):
    """What does the arithmetic."""

    CPU = "cpu"
    GPU = "gpu"


class ExecutionTarget(str, Enum):
    """A place work can run. Four real ones; no policies mixed in."""

    LOCAL_CPU = "local_cpu"
    LOCAL_GPU = "local_gpu"
    CLOUD_CPU = "cloud_cpu"
    CLOUD_GPU = "cloud_gpu"

    @property
    def machine(self) -> Machine:
        return Machine.LOCAL if self.value.startswith("local") else Machine.CLOUD

    @property
    def processor(self) -> Processor:
        return Processor.GPU if self.value.endswith("gpu") else Processor.CPU

    @property
    def costs_us_money(self) -> bool:
        """Whether running here consumes infrastructure we pay for.

        The field the pricing model is built on. A local render's marginal cost
        to us is zero, which is what lets a desktop tier charge for software
        and intelligence rather than for minutes of somebody else's video.
        """
        return self.machine is Machine.CLOUD

    @property
    def label(self) -> str:
        return {
            ExecutionTarget.LOCAL_CPU: "This computer",
            ExecutionTarget.LOCAL_GPU: "This computer's graphics card",
            ExecutionTarget.CLOUD_CPU: "VTV cloud",
            ExecutionTarget.CLOUD_GPU: "VTV cloud, accelerated",
        }[self]


class Preference(str, Enum):
    """What the user asked for. Not the same type as where it runs.

    `AUTO` is the default and should stay the default: the question a person
    has is "how long will my video take", and the answer to that is a number
    the system can work out better than they can.
    """

    AUTO = "auto"
    #: Prefer the customer's own machine, whichever processor it has.
    LOCAL = "local"
    #: Prefer our infrastructure. What a browser-only user gets.
    CLOUD = "cloud"
    #: Name one exactly. For operators and for support reproducing a bug.
    EXACT = "exact"


@dataclass(frozen=True)
class Availability:
    """Whether a target can be used, and — when it cannot — why not.

    The reason is not decoration. "Local GPU — no GPU compositor is built yet"
    and "Local GPU — this machine has no supported card" send a user to two
    completely different actions, and an interface that renders both as a
    disabled button sends them to neither.
    """

    target: ExecutionTarget
    ready: bool
    reason: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "label": self.target.label,
            "ready": self.ready,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ExecutionPolicy:
    """Where the caller would like this job to run, and what may substitute.

    `fallbacks` is ordered and is what makes a GPU path safe to ship: a
    customer whose card runs out of memory in the fortieth minute of a
    sixty-minute render must not lose the render. Because segments are
    checkpointed, falling back is genuinely cheap — the work already on disk
    stays on disk and a different backend finishes the rest.
    """

    preference: Preference = Preference.AUTO
    #: Used only when `preference` is `EXACT`.
    target: ExecutionTarget | None = None
    fallbacks: tuple[ExecutionTarget, ...] = ()
    #: Refuse rather than fall back. For a customer on a metered plan who has
    #: said "never run this in the cloud", and for tests.
    strict: bool = False

    @classmethod
    def auto(cls) -> ExecutionPolicy:
        return cls()

    @classmethod
    def exactly(cls, target: ExecutionTarget, *, strict: bool = False) -> ExecutionPolicy:
        return cls(preference=Preference.EXACT, target=target, strict=strict)


@dataclass(frozen=True)
class Decision:
    """Where this job will actually run, and how that was arrived at."""

    target: ExecutionTarget
    why: str
    #: What was asked for, when it differs from what was chosen. Present means
    #: a substitution happened, and the user is entitled to be told.
    instead_of: ExecutionTarget | None = None

    @property
    def substituted(self) -> bool:
        return self.instead_of is not None

    def as_json(self) -> dict[str, object]:
        return {
            "target": self.target.value,
            "label": self.target.label,
            "why": self.why,
            "instead_of": self.instead_of.value if self.instead_of else None,
        }


class NoBackendAvailable(RuntimeError):
    """Nothing can run this job. Raised rather than guessed at."""


#: Preference order when nobody expressed one.
#:
#: The customer's own machine first, because it is free to us, faster for them
#: — nobody is sharing those cores — and it is the only option with no ceiling
#: on project length. Cloud is the fallback, not the default, which is the
#: opposite of how this product works today and is the direction the desktop
#: engine moves it in.
_AUTO_ORDER: tuple[ExecutionTarget, ...] = (
    ExecutionTarget.LOCAL_GPU,
    ExecutionTarget.LOCAL_CPU,
    ExecutionTarget.CLOUD_GPU,
    ExecutionTarget.CLOUD_CPU,
)


@dataclass
class Registry:
    """Which targets actually have something behind them.

    A backend registers itself; nothing here knows what a backend *is*, which
    is what keeps this module a contract rather than a second renderer. The
    registry is the only source of truth about what exists — a config file
    saying `execution=local_gpu` cannot conjure a GPU compositor into being,
    and this refuses to pretend otherwise.
    """

    backends: dict[ExecutionTarget, object] = field(default_factory=dict)
    #: Why a target is unavailable, when we know something more useful than
    #: "not registered". Set by whatever probed the hardware.
    notes: dict[ExecutionTarget, str] = field(default_factory=dict)

    def register(self, target: ExecutionTarget, backend: object) -> None:
        self.backends[target] = backend

    def backend_for(self, target: ExecutionTarget) -> object | None:
        return self.backends.get(target)

    def available(self) -> list[Availability]:
        """Every target, with a straight answer about each.

        Returns all four rather than only the working ones, because an
        interface that lists two options cannot explain why there are not four,
        and "why can't I use my graphics card" is a support ticket either way.
        Better to answer it in the product.
        """
        out: list[Availability] = []
        for target in ExecutionTarget:
            if target in self.backends:
                out.append(Availability(target=target, ready=True))
                continue
            out.append(
                Availability(
                    target=target,
                    ready=False,
                    reason=self.notes.get(
                        target, "no execution backend is built for this yet"
                    ),
                )
            )
        return out

    def ready(self) -> tuple[ExecutionTarget, ...]:
        return tuple(target for target in ExecutionTarget if target in self.backends)

    def chain(self, policy: ExecutionPolicy) -> list[tuple[ExecutionTarget, object]]:
        """Every backend worth trying, best first. The fallback order.

        ## Why this is a list rather than a single answer

        `resolve` answers "where will this job run", which is the right question
        for a report, a price and a progress line. It is the wrong question for
        the render loop, because the answer can change **between one segment and
        the next**: a graphics card that runs out of memory in the fortieth
        minute has not made the whole job impossible, it has made this segment
        impossible, and the segment after it may well succeed.

        So the loop is given the whole chain and descends it per segment. What
        makes that affordable is checkpointing: a segment that failed on the
        first backend has cost one segment, and every segment already on disk
        stays there. Without that, per-segment fallback would mean re-rendering
        from zero on a different backend, which nobody would ship.

        A `strict` policy gets only what it asked for, because "never run this
        anywhere else" is a real requirement and a chain that quietly appends
        the cloud to it is a policy that does nothing.
        """
        wanted = _wanted(policy)
        ordered = [t for t in wanted if t in self.backends]
        if not policy.strict:
            ordered += [
                t for t in _AUTO_ORDER if t in self.backends and t not in ordered
            ]
        return [(target, self.backends[target]) for target in ordered]


def resolve(policy: ExecutionPolicy, registry: Registry) -> Decision:
    """Turn a preference into a place, or refuse.

    The one rule: **only a target with a registered backend is ever returned.**
    Everything else here is about explaining the choice well enough that a user
    who wanted their graphics card and got their processor knows why.
    """
    ready = registry.ready()
    if not ready:
        raise NoBackendAvailable(
            "no execution backend is registered; this build cannot render"
        )

    wanted = _wanted(policy)
    for candidate in wanted:
        if candidate in ready:
            first = wanted[0]
            return Decision(
                target=candidate,
                why=_why(policy, candidate, substituted=candidate is not first),
                instead_of=first if candidate is not first else None,
            )

    if policy.strict:
        asked = wanted[0].label if wanted else "the requested target"
        raise NoBackendAvailable(
            f"{asked} is not available and this job may not run anywhere else"
        )

    # Nothing asked for is available and substitution is permitted. Take the
    # best thing that exists rather than failing a render over a preference.
    #
    # The explanation goes through `_why` like every other path. Writing it
    # here instead was how this function came to describe the same situation
    # two different ways — one of them mentioning the cloud and one of them
    # not — which is a small thing until it is the sentence a customer quotes
    # back while asking why they were billed.
    for candidate in _AUTO_ORDER:
        if candidate in ready:
            return Decision(
                target=candidate,
                why=_why(policy, candidate, substituted=True),
                instead_of=wanted[0] if wanted else None,
            )
    raise NoBackendAvailable("no execution backend is registered")  # pragma: no cover


def _wanted(policy: ExecutionPolicy) -> tuple[ExecutionTarget, ...]:
    """The caller's order of preference, most wanted first."""
    if policy.preference is Preference.EXACT and policy.target is not None:
        head: tuple[ExecutionTarget, ...] = (policy.target,)
    elif policy.preference is Preference.LOCAL:
        head = (ExecutionTarget.LOCAL_GPU, ExecutionTarget.LOCAL_CPU)
    elif policy.preference is Preference.CLOUD:
        head = (ExecutionTarget.CLOUD_GPU, ExecutionTarget.CLOUD_CPU)
    else:
        head = _AUTO_ORDER
    return _unique((*head, *policy.fallbacks))


def _unique(items: Iterable[ExecutionTarget]) -> tuple[ExecutionTarget, ...]:
    seen: list[ExecutionTarget] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def _why(policy: ExecutionPolicy, chosen: ExecutionTarget, *, substituted: bool) -> str:
    """One sentence a person can act on.

    Written for the user, not the log. "Falling back to cloud_cpu" tells
    somebody nothing; "your graphics card cannot be used yet, so this ran on
    VTV's servers" tells them why their render cost what it did and what would
    change it.
    """
    if substituted:
        if chosen.machine is Machine.CLOUD:
            return (
                "this computer cannot render this yet, so it ran on VTV's servers"
            )
        return f"ran on {chosen.label.lower()} because that is what is available"
    if policy.preference is Preference.EXACT:
        return "chosen explicitly"
    if chosen.machine is Machine.LOCAL:
        return "this computer is faster and costs nothing to run on"
    return "no local execution engine is installed, so this ran in the cloud"


__all__ = [
    "Availability",
    "Decision",
    "ExecutionPolicy",
    "ExecutionTarget",
    "Machine",
    "NoBackendAvailable",
    "Preference",
    "Processor",
    "Registry",
    "resolve",
]
