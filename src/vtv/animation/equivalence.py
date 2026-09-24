"""Do two painters draw the same picture?

## Why this is library code and not a test

Because it has to run in three places: in the suite, in the GPU probe before a
card is trusted with a customer's render, and in the benchmark script on the
user's own machine. A comparison that lives only in a test file is one the
product cannot perform on itself.

## Why the tolerance is not zero

Rasterisation on a graphics card is allowed to differ from PIL in the last bit
or two. Texture filtering is implementation-defined, an edge pixel that lands
on 127 instead of 128 is not a defect, and demanding exactness would reject
every correct implementation for being a different correct implementation.

But a *loose* tolerance is worse than none, because it launders real mistakes.
So the rule has two halves, and the second is the one that bites:

* no pixel may differ by more than `TOLERANCE` per channel;
* **no pixel may be outside it at all** — `MAX_OUTLIERS` is zero.

A mean-difference check would pass a caption sitting one pixel high on every
frame of a four-hour video. Real errors — a plate in the wrong place, a missing
overlay, a transition running backwards — differ in the hundreds and are caught
by either half. Subtle errors are only caught by the second.

## The suite of scenes

Deliberately not "a typical frame". Every layer type, every transition, every
camera motion, the alpha cases, and the two that broke real renders: an overlay
during a transition (which must not fade with the picture) and a caption long
enough to page. A painter that handles a plain typography shot and nothing else
would pass a lazier comparison and fail on the first real video.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter

from vtv.contracts.display import (
    Anchor,
    Background,
    DisplayList,
    Drawn,
    Label,
    Message,
    Mix,
    Picture,
    Plate,
    Vignette,
)

#: Per-channel difference a correct implementation may show. See the module
#: docstring; mirrored in `gpu_probe.TOLERANCE`.
TOLERANCE = 2

#: Pixels allowed to exceed it. Zero, and see above for why.
MAX_OUTLIERS = 0


@dataclass(frozen=True)
class Difference:
    """How far apart two paintings of the same frame are."""

    scene: str
    worst: int
    outliers: int
    #: Where they differ, when they do. For a person looking at a screenshot.
    box: tuple[int, int, int, int] | None = None

    @property
    def ok(self) -> bool:
        return self.worst <= TOLERANCE and self.outliers <= MAX_OUTLIERS

    def __str__(self) -> str:
        if self.ok:
            return f"{self.scene}: matches (worst {self.worst})"
        return (
            f"{self.scene}: worst {self.worst}, {self.outliers} pixels outside "
            f"tolerance, at {self.box}"
        )


def compare(one: Image.Image, two: Image.Image, *, scene: str = "") -> Difference:
    """Two images, one verdict."""
    if one.size != two.size:
        return Difference(scene=scene, worst=255, outliers=one.size[0] * one.size[1])
    diff = ImageChops.difference(one.convert("RGB"), two.convert("RGB"))
    worst = max(band[1] for band in diff.getextrema())
    # Pixels above the tolerance, counted rather than averaged.
    #
    # A mean hides exactly the errors worth finding: a caption sitting one
    # pixel high on every frame moves the average almost nothing. The maximum
    # per channel is taken first — `ImageChops.difference` gives three bands
    # and a pixel is an outlier if *any* channel is — then thresholded and
    # counted through the histogram, which is one pass in C rather than two
    # million in Python.
    channelwise = ImageChops.lighter(
        ImageChops.lighter(*diff.split()[:2]), diff.split()[2]
    )
    above = channelwise.point(lambda level: 255 if level > TOLERANCE else 0)
    outliers = above.histogram()[255]
    return Difference(
        scene=scene, worst=worst, outliers=outliers, box=diff.getbbox()
    )


def measure(one: Image.Image, two: Image.Image) -> dict:
    """Every number a diagnosis needs, for one pair of frames.

    Separate from `compare`, which answers a yes-or-no. This answers "how, and
    where" — and a report of "worst 136 over 1230 pixels" is what turns a
    failing scene into a fixable one.
    """
    diff = ImageChops.difference(one.convert("RGB"), two.convert("RGB"))
    histogram = diff.convert("L").histogram()
    total = sum(histogram) or 1
    mean = sum(level * count for level, count in enumerate(histogram)) / total
    channelwise = ImageChops.lighter(
        ImageChops.lighter(*diff.split()[:2]), diff.split()[2]
    )
    outliers = channelwise.point(lambda v: 255 if v > TOLERANCE else 0).histogram()[255]
    return {
        "dimensions": list(one.size),
        "max_per_channel": max(band[1] for band in diff.getextrema()),
        "pixels_outside_tolerance": outliers,
        "mean_absolute_difference": round(mean, 3),
        "bounding_box": list(diff.getbbox()) if diff.getbbox() else None,
    }


def classify(cpu: Image.Image, gpu: Image.Image) -> list[str]:
    """Name the cause by transforming the GPU frame until the difference falls.

    Each hypothesis is a transform. If applying it collapses the difference, the
    hypothesis is the cause; if it does not, the hypothesis is eliminated. That
    is stronger than reading a heatmap, and it is what makes this report worth
    sending back rather than a folder of screenshots.
    """
    base = measure(cpu, gpu)["max_per_channel"]
    found: list[str] = []

    def better_as(name: str, candidate: Image.Image) -> None:
        after = measure(cpu, candidate)["max_per_channel"]
        if after < base * 0.4:
            found.append(f"{name} (worst {base} -> {after})")

    better_as("vertical flip", gpu.transpose(Image.Transpose.FLIP_TOP_BOTTOM))
    better_as("horizontal flip", gpu.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        better_as(
            f"translation ({dx:+d},{dy:+d})",
            ImageChops.offset(gpu, dx, dy),
        )

    # A constant difference everywhere is a clear colour or a blend against the
    # wrong background, not a geometry problem.
    diff = ImageChops.difference(cpu.convert("RGB"), gpu.convert("RGB"))
    extrema = diff.getextrema()
    if all(low == high for low, high in extrema) and base > TOLERANCE:
        found.append(f"uniform offset {[high for _, high in extrema]} (clear colour)")

    # Differences confined to edges are a sampler difference, not a placement
    # one: erode the difference and see whether it survives.
    interior = diff.convert("L").filter(ImageFilter.MinFilter(3))
    if interior.getextrema()[1] <= TOLERANCE and base > TOLERANCE:
        found.append("edges only (texture filtering)")

    # High frequency only: the same picture, resampled differently.
    #
    # The check that was missing the first time, and the reason a real run came
    # back "unclassified" on fifteen scenes. Erosion finds a difference that
    # lives on edges; it cannot find one that lives *everywhere* but only in
    # fine detail — which is exactly what two resampling kernels disagreeing
    # looks like on a picture full of stripes.
    #
    # So: throw the detail away from both and look again. If the pictures agree
    # once blurred, they are the same picture reconstructed by different
    # filters, and the fix is the sampler rather than the geometry.
    if base > TOLERANCE:
        small = (max(1, cpu.width // 8), max(1, cpu.height // 8))
        coarse = measure(
            cpu.convert("RGB").resize(small, Image.Resampling.BOX),
            gpu.convert("RGB").resize(small, Image.Resampling.BOX),
        )["max_per_channel"]
        if coarse <= max(TOLERANCE, base // 8):
            found.append(
                f"high frequency only (resampling kernel): coarse difference "
                f"{coarse} against {base} at full size"
            )

    if not found:
        found.append("unclassified: see the diff image")
    return found



def scenes(width: int, height: int, *, still_key: str = "still") -> dict[str, DisplayList]:
    """Every kind of frame a painter has to get right.

    Keyed by name so a failure says *which* scene, which is the difference
    between "the GPU painter is wrong" and "the GPU painter gets wipes
    backwards".
    """
    box = (0.06 * width, 0.06 * height, 0.42 * width, 0.28 * height)
    plate = Plate(box=box, radius=0.02 * width, fill=(0, 0, 0, 140))
    outlined = Plate(
        box=box, radius=0.02 * width, outline=(220, 220, 230, 160), outline_width=2
    )
    label = Label(
        text="A caption line",
        role="body",
        position=(0.08 * width, 0.10 * height),
        colour=(240, 240, 245, 255),
    )
    chip = Label(
        text="Photo by A. Photographer, CC-BY 4.0",
        role="micro",
        position=(0.08 * width, 0.32 * height),
        colour=(230, 230, 235, 217),
        anchor=Anchor.LEFT_MIDDLE,
    )
    background = Background(colour=(14, 14, 18, 255))

    def frame(*layers: object, **kw: object) -> DisplayList:
        return DisplayList(width=width, height=height, layers=tuple(layers), **kw)  # type: ignore[arg-type]

    # A still, held at three points of a camera move: the transform is the
    # single most likely thing for a second implementation to get subtly wrong,
    # and a scene that only checks the midpoint would miss an inverted axis.
    def picture(scale: float, dx: float = 0.0) -> Picture:
        w, h = width * scale, height * scale
        return Picture(
            key=still_key,
            box=((width - w) / 2 + dx * width, (height - h) / 2, (width + w) / 2 + dx * width, (height + h) / 2),
        )

    plain = frame(picture(1.0))
    over = frame(picture(1.1, 0.03))

    return {
        "background": frame(background),
        "message": frame(Message(text="Visual unavailable for this section")),
        "picture": plain,
        "picture-zoomed": frame(picture(1.1)),
        "picture-panned": frame(picture(1.1, 0.04)),
        "picture-contained": frame(picture(0.8)),
        "plate-filled": frame(background, plate),
        "plate-outlined": frame(background, outlined),
        "text": frame(background, plate, label),
        "text-anchored": frame(background, chip),
        "vignette": frame(background, Vignette(strength=0.35)),
        "vignette-strong": frame(background, Vignette(strength=0.9)),
        # The overlay cases. An overlay is on the glass in front of the shot,
        # so a transition must not touch it — the defect that shipped once and
        # was caught only by comparing against the previous implementation.
        "overlay-over-picture": DisplayList(
            width=width, height=height, layers=(picture(1.0),),
            overlays=(plate, label),
        ),
        "overlay-during-blend": DisplayList(
            width=width, height=height, layers=over.layers,
            overlays=(plate, label), beneath=plain, transition=Mix.BLEND, progress=0.5,
        ),
        # Every transition, at three points, because a painter can get the
        # endpoints right and the middle backwards.
        **{
            f"{name}-{int(p * 100)}": DisplayList(
                width=width, height=height, layers=over.layers,
                beneath=plain, transition=kind, progress=p,
            )
            for name, kind in (
                ("blend", Mix.BLEND), ("wipe", Mix.WIPE), ("push", Mix.PUSH)
            )
            for p in (0.0, 0.5, 1.0)
        },
    }


def check(first: object, second: object, *, width: int, height: int) -> list[Difference]:
    """Paint every scene with both painters and report every difference.

    Returns all of them rather than stopping at the first, because "wipes are
    backwards" and "text is a pixel high" are separate bugs and finding them
    one render at a time is how a port takes a week.
    """
    out: list[Difference] = []
    for name, frame in scenes(width, height).items():
        try:
            a = first.paint(frame)  # type: ignore[attr-defined]
            b = second.paint(frame)  # type: ignore[attr-defined]
        except Exception as exc:
            out.append(
                Difference(scene=f"{name} ({type(exc).__name__}: {exc})",
                           worst=255, outliers=width * height)
            )
            continue
        out.append(compare(a, b, scene=name))
    return out


def summarise(differences: list[Difference]) -> str:
    failed = [d for d in differences if not d.ok]
    if not failed:
        worst = max((d.worst for d in differences), default=0)
        return f"{len(differences)} scenes match (worst channel difference {worst})"
    lines = [f"{len(failed)} of {len(differences)} scenes differ:"]
    lines += [f"  {d}" for d in failed]
    return "\n".join(lines)


__all__ = [
    "MAX_OUTLIERS",
    "classify",
    "measure",
    "TOLERANCE",
    "Difference",
    "check",
    "compare",
    "scenes",
    "summarise",
]
