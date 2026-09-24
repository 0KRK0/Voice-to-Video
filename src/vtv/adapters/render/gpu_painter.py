"""Compositing frames on the machine's graphics card.

**STATUS: RUN ON REAL HARDWARE ONCE; FOUR DEFECTS FOUND AND FIXED; AWAITING
RE-VERIFICATION.**

First contact was a GTX 1650 (driver 610.88, OpenGL 3.3). Fifteen of the
twenty-three calibration scenes matched the reference; eight did not, and the
pattern in *which* eight was the diagnosis:

| what failed | cause |
|---|---|
| every scene with a plate, label or message | the framebuffer is read bottom-up |
| the one scene whose picture is smaller than the frame | cleared to black, not the theme background |
| the same scene, a nine-pixel band | `paste_fitted` overflows its box; a quad did not |
| both overlay scenes | the reference *replaces* pixels and flattens onto the theme background; a card alpha-composites |

Every failing scene contained something vertically asymmetric and every passing
one was symmetric, which is what a mirrored readback looks like from the
outside. The pixel counts confirmed it to the unit: a plate at y 11..50 in a
180-row frame differed across y 11..169 — itself and its mirror.

All four are fixed. The product still does not offer GPU compositing until the
equivalence run passes: `gpu_probe` reports unavailable, `wiring` registers no
GPU target. See `docs/GPU_RENDERING.md`.

## What the graphics card is actually asked to do

Not everything, and the profile says why. Composition was measured at 77% of
render time, and inside that, **text rasterisation was 0.7%**. The expensive
part was never drawing glyphs; it was moving 1920×1080 buffers around — a
paste, a blend, a channel split, a convert, per frame.

So the split is:

    CPU   layout, text, rounded rectangles, the animation engine, overlays,
          and the transition mix — all of it the reference's own code
    GPU   the one expensive thing: resampling a photograph into a moving frame

That is narrower than it first looked, and each narrowing was forced by a
measurement rather than chosen. Overlays went back to the reference because it
composites them by *replacing* pixels and flattening onto the theme background,
which no card does by accident. Transitions went back because the reference
wipe is a hard step at either end with a blurred band only in between — and
because doing it on the card meant three readbacks and two uploads to replace
one pass of `Image.blend`.

What is left is the part that was always the point: a photograph is larger than
the frame, so every picture is a LANCZOS reduction, once per frame, per shot.

That is deliberately lopsided, and it buys two things. The expensive work moves
to hardware built for it. And **the parts that must match the reference exactly
are still produced by the reference**: a glyph is rasterised by PIL on both
paths, so a caption cannot land a pixel high on one of them. Reimplementing
text on a card would be a font-rendering project whose output could never sit
inside a two-per-channel tolerance.

## The honest consequence

A typography-heavy video will not get much faster. Its frames are produced by
the animation engine on the processor and then uploaded, and the upload is new
work. An image-heavy video — photographs with camera moves, dissolves between
them — is where the transform and the compositing dominate and the card wins.

That is a measurement to be made per project, not a claim. `calibration.better`
already refuses to switch backends for less than a ten percent gain, and
`scripts/bench_gpu_renderer.py` is what produces the number.

## Why OpenGL 3.3 through moderngl

Chosen after checking what the runtime can actually carry, which is the part of
this decision people skip.

* **It runs where the customer is.** OpenGL 3.3 is available on every Windows
  machine with a driver from the last decade — NVIDIA, AMD and Intel alike —
  and `moderngl` creates a standalone context with no window and no display.
* **It is a small dependency.** One C++ extension and a pip wheel, not a
  runtime, a toolchain or a vendor SDK.
* **It is not CUDA.** Nothing here is NVIDIA-specific. A machine with an AMD or
  Intel card runs the same shaders.
* **The seam is one file.** `Painter` is the port; swapping to `wgpu` — which
  maps onto D3D12, Vulkan and Metal, and is the better long-term answer once
  its Python bindings settle — replaces this module and nothing else.

The known cost of that choice: macOS has deprecated OpenGL and caps it at 4.1.
It still works, and a Metal path through `wgpu` is the eventual answer there.
"""

from __future__ import annotations

from typing import Any

from PIL import Image

from vtv.animation.painter import (  # the reference implementation
    _base,
    _draw_over,
    _message,
    mix,
)
from vtv.animation.theme import Theme
from vtv.contracts.display import (
    Background,
    DisplayList,
    Drawn,
    Fit,
    Message,
    Mix,
    Picture,
)


def _has_raster(frame: DisplayList) -> bool:
    """Whether this frame is worth a device. The rule lives on the frame.

    Deliberately a one-line delegation rather than a copy: the renderer asks the
    same question when it decides whether to build a device for a segment, and
    two implementations of "is this worth a card" that drift apart would route
    a segment to hardware the painter then declines to use.
    """
    return frame.needs_resampling


#: Minimum OpenGL this needs. 3.3 core gives sampler objects, instancing and
#: GLSL 330 — everything below — and is the floor every desktop driver since
#: about 2010 clears.
REQUIRE_GL = 330

#: Draws a textured quad into the framebuffer, blending alpha-over.
#:
#: The quad's corners come from the layer's box, so the *transform* — Ken
#: Burns, a pan, a zoom, a crop — is expressed as vertex positions rather than
#: as a resampled buffer. That is the operation this whole module exists for:
#: on the processor it is a LANCZOS resize and a paste of several megabytes,
#: and here it is four vertices and the sampler.
_VERTEX = """
#version 330
in vec2 position;
in vec2 uv;
out vec2 texcoord;
void main() {
    texcoord = uv;
    gl_Position = vec4(position, 0.0, 1.0);
}
"""

_FRAGMENT = """
#version 330
uniform sampler2D source;
uniform float opacity;
in vec2 texcoord;
out vec4 colour;
void main() {
    vec4 texel = texture(source, texcoord);
    colour = vec4(texel.rgb, texel.a * opacity);
}
"""

#: One axis of a Lanczos-3 resample, matching PIL tap for tap.
#:
#: ## Why a card's built-in filter is not good enough
#:
#: Not merely "does not match". A photograph is almost always *larger* than the
#: frame it goes into — a 4000-pixel source in a 1920-pixel video — so every
#: picture is a downscale, and bilinear filtering samples only the four texels
#: nearest the destination pixel however far apart the source pixels are. On
#: detail finer than the reduction factor that aliases: fine stripes crawl and
#: shimmer as a Ken Burns move drifts across them.
#:
#: Measured on the calibration still, LANCZOS against bilinear for a 0.8
#: downscale differs by up to 40 per channel with a mean of 8 — the reference is
#: right and the card's default is wrong, and the difference is visible motion
#: artefacts rather than a subtle tint.
#:
#: ## Why one axis and not two
#:
#: Because PIL resizes in two passes and **writes an 8-bit image between them**,
#: and that intermediate is not a rounding detail: Lanczos overshoots at a sharp
#: edge, and `clip8` throws the overshoot away before the second pass ever sees
#: it. A single 2D accumulation in float keeps the overshoot, and the two agree
#: only on material soft enough never to ring past 0 or 255.
#:
#: That is exactly what happened here. A first version did the whole kernel in
#: one 2D pass and was verified against PIL on a gradient and on stripes of 210
#: over 40 — worst 1, and worthless, because neither rings out of range. On a
#: still with pure white lines it was out by 19 to 23 with hundreds of pixels
#: past tolerance. Measured afterwards on high-contrast material: one pass in
#: float is out by up to 60; two passes through a clipped 8-bit intermediate are
#: out by 0.
#:
#: So the intermediate framebuffer is RGBA8 on purpose. The clamp and the
#: quantisation to eight bits are not a storage decision, they are the
#: behaviour being reproduced.
#:
#: ## The rest of matching PIL
#:
#: `ImagingResampleHorizontal` maps destination pixel `i` to source coordinate
#: `(i + 0.5) * scale`, widens the kernel by `max(1, scale)` when reducing —
#: without which a downscale is undersampled no matter how good the kernel —
#: **truncates** the tap range at the image edge and renormalises by the weights
#: that survived. Truncates, not clamps: an earlier version clamped the tap to
#: the edge texel and counted its weight, which duplicates that texel and is a
#: different picture along every border.
#:
#: What is still not reproduced is PIL's arithmetic: it rounds each weight to
#: 22-bit fixed point and accumulates in integers, where this is float32. That
#: is worth well under one level per channel and is the residual the
#: equivalence run should show.
_RESAMPLE_FRAGMENT = """
#version 330
uniform sampler2D source;
uniform vec2 sourceSize;    // the logical region being read, in texels
uniform vec2 textureSize;   // the physical texture, which may be larger
uniform float filterScale;  // max(1, scale) along `axis`: widens the kernel
uniform vec2 axis;          // (1,0) horizontal pass, (0,1) vertical pass
uniform float opacity;
in vec2 texcoord;
out vec4 colour;

const float PI = 3.141592653589793;
// Six taps per unit of filter scale, plus a couple for the rounding. Sixty-four
// covers a reduction to about a tenth; beyond that the painter hands the frame
// to the reference rather than quietly dropping taps — see `_TAP_LIMIT`.
const int MAX_TAPS = 64;

float lanczos3(float x) {
    x = abs(x);
    if (x < 1e-8) return 1.0;
    if (x >= 3.0) return 0.0;
    float px = PI * x;
    return (3.0 * sin(px) * sin(px / 3.0)) / (px * px);
}

void main() {
    vec2 pos = texcoord * sourceSize;
    float center = dot(pos, axis);
    float extent = dot(sourceSize, axis);
    float support = 3.0 * filterScale;
    float lo = floor(center - support + 0.5);
    float hi = floor(center + support + 0.5);

    vec4 acc = vec4(0.0);
    float total = 0.0;
    for (int i = 0; i < MAX_TAPS; ++i) {
        float s = lo + float(i);
        if (s > hi) break;
        // Outside the picture the tap is dropped, not clamped to the border.
        // PIL narrows the range and renormalises over what is left, which is
        // what `total` does below.
        if (s < 0.0 || s > extent - 1.0) continue;
        float w = lanczos3((s - center + 0.5) / filterScale);
        vec2 texel = pos * (vec2(1.0) - axis) + axis * (s + 0.5);
        acc += texture(source, texel / textureSize) * w;
        total += w;
    }
    vec4 mixed = total > 0.0 ? acc / total : texture(source, texcoord);
    colour = vec4(clamp(mixed.rgb, 0.0, 1.0), clamp(mixed.a, 0.0, 1.0) * opacity);
}
"""

class _BeyondReach(Exception):
    """A picture reduced further than the shader can sample honestly."""


#: Largest reduction the shader will attempt, from `MAX_TAPS`. A picture needing
#: more goes to the reference: dropping taps is undersampling, and undersampling
#: is the artefact this shader exists to remove.
_TAP_LIMIT = 10.0


def _quad(box: tuple[float, float, float, float], width: int, height: int) -> tuple:
    """A layer's box as clip-space vertices.

    OpenGL's clip space is -1..1 with y upward; a display list's box is pixels
    with y downward. Getting this inversion wrong produces a picture that is
    correct and upside down, which is exactly the class of mistake the
    equivalence harness exists to catch on the first run.
    """
    x0, y0, x1, y1 = box
    left = (x0 / width) * 2.0 - 1.0
    right = (x1 / width) * 2.0 - 1.0
    top = 1.0 - (y0 / height) * 2.0
    bottom = 1.0 - (y1 / height) * 2.0
    return (
        left, bottom, 0.0, 1.0,
        right, bottom, 1.0, 1.0,
        left, top, 0.0, 0.0,
        right, top, 1.0, 0.0,
    )


def _fitted_box(
    source: tuple[int, int],
    box: tuple[float, float, float, float],
    *,
    cover: bool,
) -> tuple[float, float, float, float]:
    """Where `Canvas.paste_fitted` actually puts a picture.

    Not the same rectangle as the box, and the difference is not a rounding
    detail. `paste_fitted` scales the source to cover (or fit) the box, centres
    it, and then **clamps the origin to the box's own corner** — so a source
    whose aspect ratio differs from the box overflows one edge instead of being
    cropped symmetrically.

    Drawing a quad at the box instead produced a nine-pixel band along the
    bottom of every contained picture on the calibration run. Reproducing the
    arithmetic here, rather than "fixing" it in either painter, is the only
    option that keeps one reference: the CPU path is what customers' videos
    already look like.
    """
    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    target_w, target_h = max(1, x1 - x0), max(1, y1 - y0)
    source_w, source_h = source
    scale = (
        max(target_w / source_w, target_h / source_h)
        if cover
        else min(target_w / source_w, target_h / source_h)
    )
    width = max(1, int(source_w * scale))
    height = max(1, int(source_h * scale))
    left = max(x0, x0 + (target_w - width) // 2)
    top = max(y0, y0 + (target_h - height) // 2)
    return (float(left), float(top), float(left + width), float(top + height))


class GpuPainter:
    """`Painter` on the graphics card. Unverified — see the module docstring."""

    name = "gpu"

    def __init__(
        self,
        *,
        theme: Theme,
        engine: object,
        size: object,
        stills: Any,
    ) -> None:
        import moderngl
        import numpy

        self.theme = theme
        self.engine = engine
        self.size = size
        self.stills = stills
        self._numpy = numpy
        self.width = int(getattr(size, "width", theme.width))
        self.height = int(getattr(size, "height", theme.height))

        self.ctx = moderngl.create_context(standalone=True, require=REQUIRE_GL)
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
        self._program = self.ctx.program(
            vertex_shader=_VERTEX, fragment_shader=_FRAGMENT
        )
        self._resampler = self.ctx.program(
            vertex_shader=_VERTEX, fragment_shader=_RESAMPLE_FRAGMENT
        )
        self._target = self.ctx.simple_framebuffer((self.width, self.height), 4)
        #: Uploaded stills, by layer key. Cleared by `release` between segments
        #: so a long video does not hold every photograph it has ever shown.
        self._textures: dict[str, Any] = {}
        self._memo: dict[str, Image.Image] = {}
        #: The 8-bit intermediate between the two resampling passes. Held and
        #: grown rather than allocated per frame: a camera move changes the
        #: destination width every frame, and re-creating a texture and a
        #: framebuffer sixty times a second is measurable where reusing one is
        #: not. Width only ever grows; height follows the source picture, which
        #: changes once per shot.
        self._stage: Any = None
        self._stage_size: tuple[int, int] = (0, 0)
        #: The reference painter, for frames with no full-frame work in them.
        #: Built lazily because most frames never need it.
        self._cpu: Any = None

    # -- Painter ----------------------------------------------------------

    def paint(self, frame: DisplayList) -> Image.Image:
        if frame.key and frame.key in self._memo:
            return self._memo[frame.key]

        # Nothing here for a graphics card.
        #
        # A frame made only of plates, labels, a message or a vignette is
        # rasterisation the reference already does, and there is no full-frame
        # buffer work to move. Handing it to the CPU painter is not a fallback
        # — it is the correct implementation, and it removes an entire class of
        # divergence: the reference composites overlays by *replacing* pixels
        # and then flattening onto the theme background, which is not what
        # alpha-over on a card does. Reimplementing that semantic in a shader
        # would be reimplementing a PIL quirk, and it would drift.
        if not _has_raster(frame):
            return self._reference().paint(frame)

        try:
            painted = self._compose(frame)
        except _BeyondReach:
            # The whole frame, not the one picture. Half a frame from each
            # painter would be a third rendering, equal to neither, and the
            # equivalence harness would be checking something no customer gets.
            painted = self._reference().paint(frame)
            if frame.key:
                self._memo[frame.key] = painted
            return painted

        if frame.key:
            self._memo[frame.key] = painted
        return painted

    def _compose(self, frame: DisplayList) -> Image.Image:
        """One frame, assuming the card can do it. Raises `_BeyondReach` if not."""
        if frame.beneath is not None and frame.transition is not Mix.NONE:
            # The pictures are composed on the card; the *mix* is the
            # reference's.
            #
            # Not a compromise — the mismatch was measured. The reference wipe
            # is a *hard step* at progress 0 and 1 (its feathering is guarded by
            # `0 < edge < width`) and a Gaussian-blurred band only in between;
            # the shader's `smoothstep` was soft at every progress. Matching it
            # would mean reimplementing PIL's blur in GLSL, and a shader that
            # imitates a library's internals is a shader that drifts when the
            # library changes.
            #
            # The cost of giving it back is small and known. On 1080p the
            # reference mixes at 1.3 ms (push), 8.1 ms (blend) and 28.4 ms
            # (wipe) per frame, against a whole-frame compose budget of about
            # 38 ms. Doing it on the card meant reading both frames back,
            # uploading them again, and reading back a third time — five
            # transfers of an 8 MB frame across the bus for arithmetic that
            # takes microseconds.
            painted = mix(
                self.paint(frame.beneath),
                self.paint(
                    DisplayList(
                        width=frame.width, height=frame.height, layers=frame.layers
                    )
                ),
                frame.transition,
                frame.progress,
            )
        else:
            painted = self._draw(frame.layers)

        if frame.overlays:
            # Drawn by the reference, onto the finished picture. The frame has
            # to come back from the card for the encoder anyway, so this costs
            # nothing extra — and it is the only way the plate ends up composited
            # against the same thing on both paths.
            painted = _draw_over(painted, frame.overlays, self.theme)

        return painted

    def release(self) -> None:
        """Drop per-segment resources, keeping the device and the pipelines."""
        for texture in self._textures.values():
            with _quiet():
                texture.release()
        self._textures.clear()
        self._release_stage()
        self._memo.clear()
        if self._cpu is not None:
            self._cpu.release()

    def close(self) -> None:
        """Drop the device itself. Called when a worker is finished."""
        self.release()
        self._release_stage()
        for item in (
            self._target, self._program, self._resampler, self.ctx
        ):
            with _quiet():
                item.release()

    # -- drawing ----------------------------------------------------------

    def _clear(self) -> None:
        """Start from the theme background, which is what the reference does.

        `CpuPainter` begins every frame with `Canvas(theme)`, and a `Canvas` is
        filled with `theme.background`. Clearing to black instead is invisible
        whenever a picture covers the whole frame and obvious the moment one
        does not: on the calibration run it accounted for 18 432 of the 20 736
        differing pixels in the one scene where the picture is smaller than the
        frame.
        """
        self._target.use()
        self._target.clear(*(channel / 255.0 for channel in self.theme.background))

    def _draw(self, layers: tuple) -> Image.Image:
        self._clear()
        vector: list[object] = []
        for layer in layers:
            if isinstance(layer, Background):
                self._target.clear(*[c / 255.0 for c in layer.colour])
            elif isinstance(layer, Picture):
                texture = self._texture_for(layer.key)
                self._resample(
                    texture,
                    _fitted_box(
                        texture.size, layer.box, cover=layer.fit is Fit.COVER
                    ),
                )
            elif isinstance(layer, Drawn | Message):
                # Produced by the reference implementation and uploaded. The
                # animation engine is the same code on both paths, which is
                # what keeps a chart identical rather than merely similar.
                image = _base(
                    layer,
                    theme=self.theme,
                    engine=self.engine,
                    size=self.size,
                    stills=self.stills,
                )
                self._blit(self._upload(image.convert("RGBA")), _full(self))
            else:
                vector.append(layer)
        painted = self._read()
        if vector:
            painted = _draw_over(painted, tuple(vector), self.theme)
        return painted

    def _blit(self, texture: Any, box: tuple) -> None:
        """Copy a texture into a rectangle of the frame, one texel per pixel.

        For frames the animation engine already produced at the right size.
        Running a resampling kernel over an identity mapping is work with no
        effect, and the plain shader says so.
        """
        self._render(self._program, texture, _quad(box, self.width, self.height))

    def _resample(self, texture: Any, box: tuple) -> None:
        """Draw a picture into `box`, scaled the way the reference scales it.

        Two passes, because that is what PIL does and the intermediate is
        observable — see `_RESAMPLE_FRAGMENT`. Horizontally into an RGBA8
        buffer, whose clamp and quantisation are the point rather than an
        implementation detail; then vertically out of that buffer straight into
        the frame.
        """
        import moderngl

        source_w, source_h = texture.size
        target_w = max(1, int(round(box[2] - box[0])))
        target_h = max(1, int(round(box[3] - box[1])))
        horizontal = max(1.0, source_w / target_w)
        vertical = max(1.0, source_h / target_h)
        if max(horizontal, vertical) > _TAP_LIMIT:
            # Raised rather than checked by the caller, because a call site that
            # has to remember is a call site that eventually does not — and the
            # failure it would produce is a picture that is slightly aliased on
            # some machines, which nobody reports and nobody can reproduce.
            raise _BeyondReach(
                f"reduction of {max(horizontal, vertical):.1f}x needs more taps "
                f"than the shader has"
            )

        if horizontal == 1.0 and vertical == 1.0:
            self._blit(texture, box)
            return

        stage, stage_w = self._staging(target_w, source_h)

        # Pass one: horizontal only, into the 8-bit intermediate.
        #
        # Blending off: this is a resample, not a composite, and the buffer's
        # previous contents are not part of the picture. The quad's v runs 0 at
        # the bottom of clip space so that row 0 of the intermediate is row 0 of
        # the source — the framebuffer's origin is the bottom left, and getting
        # this the other way round is the mirrored-frame bug in a smaller place.
        self.ctx.disable(moderngl.BLEND)
        stage.use()
        stage.viewport = (0, 0, target_w, source_h)
        self._render(
            self._resampler,
            texture,
            (-1.0, -1.0, 0.0, 0.0,
              1.0, -1.0, 1.0, 0.0,
             -1.0,  1.0, 0.0, 1.0,
              1.0,  1.0, 1.0, 1.0),
            source_size=(float(source_w), float(source_h)),
            texture_size=(float(source_w), float(source_h)),
            filter_scale=horizontal,
            axis=(1.0, 0.0),
        )
        self.ctx.enable(moderngl.BLEND)

        # Pass two: vertical, out of the intermediate and into the frame. The
        # logical region is the part of the buffer pass one wrote; the physical
        # texture may be wider, because it is grown and reused.
        self._target.use()
        self._render(
            self._resampler,
            stage.color_attachments[0],
            _quad(box, self.width, self.height),
            source_size=(float(target_w), float(source_h)),
            texture_size=(float(stage_w), float(source_h)),
            filter_scale=vertical,
            axis=(0.0, 1.0),
        )

    def _render(
        self,
        program: Any,
        texture: Any,
        quad: tuple,
        *,
        source_size: tuple[float, float] | None = None,
        texture_size: tuple[float, float] | None = None,
        filter_scale: float = 1.0,
        axis: tuple[float, float] = (1.0, 0.0),
    ) -> None:
        """One textured quad. The only place this painter issues a draw call."""
        import moderngl

        buffer = self.ctx.buffer(
            self._numpy.array(quad, dtype="f4").tobytes()
        )
        array = self.ctx.vertex_array(
            program, [(buffer, "2f 2f", "position", "uv")]
        )
        texture.use(0)
        program["source"].value = 0
        program["opacity"].value = 1.0
        if program is self._resampler:
            program["sourceSize"].value = source_size or (1.0, 1.0)
            program["textureSize"].value = texture_size or source_size or (1.0, 1.0)
            program["filterScale"].value = filter_scale
            program["axis"].value = axis
        array.render(moderngl.TRIANGLE_STRIP)
        array.release()
        buffer.release()

    def _staging(self, width: int, height: int) -> tuple[Any, int]:
        """The intermediate buffer, grown to fit and then kept."""
        held_w, held_h = self._stage_size
        if self._stage is None or width > held_w or height != held_h:
            self._release_stage()
            # Rounded up so a slow zoom does not reallocate on every frame.
            grown = max(width, held_w)
            grown = ((grown + 63) // 64) * 64
            colour = self.ctx.texture((grown, height), 4)
            colour.filter = _nearest()
            colour.repeat_x = colour.repeat_y = False
            self._stage = self.ctx.framebuffer(color_attachments=[colour])
            self._stage_size = (grown, height)
        return self._stage, self._stage_size[0]

    def _release_stage(self) -> None:
        if self._stage is not None:
            for item in (*self._stage.color_attachments, self._stage):
                with _quiet():
                    item.release()
        self._stage = None
        self._stage_size = (0, 0)

    # -- device -----------------------------------------------------------

    def _reference(self) -> Any:
        """A CPU painter sharing this one's theme, engine and stills."""
        from vtv.animation.painter import CpuPainter

        if getattr(self, "_cpu", None) is None:
            self._cpu = CpuPainter(
                theme=self.theme, engine=self.engine, size=self.size,
                stills=self.stills,
            )
        return self._cpu

    def _texture_for(self, key: str) -> Any:
        texture = self._textures.get(key)
        if texture is None:
            still = self.stills(key)
            image = (
                still.convert("RGBA")
                if still is not None
                else _message(self.theme, "Visual unavailable").convert("RGBA")
            )
            texture = self._upload(image, keep=True)
            self._textures[key] = texture
        return texture

    def _upload(self, image: Image.Image, *, keep: bool = False) -> Any:
        texture = self.ctx.texture(image.size, 4, image.tobytes())
        texture.filter = _nearest()
        texture.repeat_x = texture.repeat_y = False
        del keep
        return texture

    def _read(self) -> Image.Image:
        """Pull the framebuffer back as a PIL image, the right way up.

        ## The bug this line exists to fix

        OpenGL's framebuffer origin is the **bottom** left; PIL's is the top.
        `glReadPixels` therefore returns the last row of the picture first, and
        handing that straight to `Image.frombytes` produces a frame that is
        vertically mirrored.

        It was invisible in almost every check, which is what made it dangerous.
        A GTX 1650 ran the twenty-three calibration scenes and eight failed —
        and every single failing scene contained something vertically
        asymmetric (a caption plate at the top, a line of text, a message card)
        while every passing one was symmetric (a flat background, a centred
        vignette, a full-frame picture, a horizontal wipe). The arithmetic
        confirmed it exactly: a plate at y 11..50 in a 180-pixel frame produced
        differences spanning y 11..169, which is that plate and its mirror at
        y 130..169 and nothing in between.

        Textures do *not* need the same treatment. `glTexImage2D` maps the first
        row of data to t=0, and `_quad` puts t=0 at the top of the screen, so an
        uploaded PIL image already lands the right way up.
        """
        raw = Image.frombytes(
            "RGBA", (self.width, self.height), self._target.read(components=4)
        )
        return raw.transpose(Image.Transpose.FLIP_TOP_BOTTOM).convert("RGB")


def _nearest() -> tuple:
    """Nearest-neighbour filtering, for every texture this painter makes.

    Deliberate, not a default. Every sample taken here is either an exact texel
    centre or is weighted by the resampling shader, which does its own
    filtering. Leaving the fixed-function filter on would put a second,
    vendor-defined resample on top of ours — and vendor-defined is exactly what
    must not decide what a customer's video looks like.
    """
    import moderngl

    return (moderngl.NEAREST, moderngl.NEAREST)


def _full(painter: GpuPainter) -> tuple[float, float, float, float]:
    return (0.0, 0.0, float(painter.width), float(painter.height))


class _quiet:
    """Swallow release errors. A device teardown must never fail a render."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def inspect_device() -> Any:
    """Initialise a device, draw a calibration frame, compare it to the CPU.

    Called by `gpu_probe.probe`. Returns a `GpuReport`, and reports
    `available=True` only when a real frame came back matching the reference —
    hardware being present is not a capability, and a driver that initialises
    and draws the *wrong* picture is worse than one that refuses.
    """
    from vtv.adapters.render.gpu_probe import GpuReport
    from vtv.animation.engine import AnimationEngine, RenderSize
    from vtv.animation.equivalence import check, summarise
    from vtv.animation.painter import CpuPainter
    from vtv.contracts.style import StyleProfile

    try:
        import moderngl  # noqa: F401
    except ImportError:
        return GpuReport(
            available=False,
            reason=(
                "no GPU graphics library is installed. "
                "Install it with: pip install moderngl"
            ),
        )

    width, height = 320, 180
    theme = Theme.from_style(StyleProfile(), width=width, height=height)
    size = RenderSize(width, height)
    engine = AnimationEngine(StyleProfile())

    def blank(_key: str) -> Image.Image:
        return Image.new("RGB", (200, 120), (90, 120, 200))

    painter = GpuPainter(theme=theme, engine=engine, size=size, stills=blank)
    try:
        info = dict(painter.ctx.info)
        differences = check(
            CpuPainter(theme=theme, engine=engine, size=size, stills=blank),
            painter,
            width=width,
            height=height,
        )
        worst = max((d.worst for d in differences), default=255)
        failed = [d for d in differences if not d.ok]
        return GpuReport(
            available=not failed,
            reason="" if not failed else summarise(differences),
            vendor=str(info.get("GL_VENDOR", "")),
            device=str(info.get("GL_RENDERER", "")),
            driver=str(info.get("GL_VERSION", "")),
            api="opengl",
            max_texture=info.get("GL_MAX_TEXTURE_SIZE"),
            calibration_difference=worst,
        )
    finally:
        painter.close()


__all__ = ["REQUIRE_GL", "GpuPainter", "inspect_device"]
