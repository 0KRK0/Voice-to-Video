# GPU rendering

**Status: run once on real hardware. Four defects found and fixed. Awaiting
re-verification.**

First contact was a GTX 1650 (driver 610.88, OpenGL 3.3). Fifteen of the
twenty-three calibration scenes matched; eight did not. The product still does
not offer GPU compositing — `/health` reports the target unavailable and
`wiring.execution_registry` registers no GPU backend — until the equivalence run
passes.

### What the first run found

The *pattern* was the diagnosis before any pixel was inspected: every failing
scene contained something vertically asymmetric (a plate near the top, a line of
text, a message card) and every passing one was symmetric (a flat background, a
centred vignette, a full-frame picture, a horizontal wipe). That is what a
mirrored framebuffer readback looks like from the outside, and the arithmetic
confirmed it to the pixel — a plate at y 11..50 in a 180-row frame differed
across y 11..169, which is that plate and its mirror and nothing between.

| # | defect | evidence | fix |
|---|---|---|---|
| 1 | **Framebuffer read bottom-up.** OpenGL's origin is lower-left, PIL's is upper-left, so every frame came back mirrored. | 7 of 8 failures; bbox exactly plate ∪ mirror(plate) | flip on readback |
| 2 | **Cleared to black**, where the reference starts from `theme.background` (11,13,16). | 18 432 of the 20 736 differing pixels in `picture-contained` | clear to the theme background |
| 3 | **Picture drawn at its box**, but `paste_fitted` scales to *cover* and clamps the origin, so it overflows one edge. | the remaining 2 304 pixels — a 9-row band, computed exactly | `_fitted_box` reproduces the arithmetic |
| 4 | **Overlays alpha-composited on the card.** The reference *replaces* pixels (PIL's RGBA draw writes alpha through) and flattens onto the theme background — so a plate blends with the theme colour, not the photo behind it. | `overlay-*` worst 193 | overlays go through the reference painter |

18 432 + 2 304 = 20 736, the reported outlier count for that scene exactly. That
is what made the diagnosis a measurement rather than a guess.

Defect 4 is worth dwelling on. It is not a GPU bug — it is a *quirk of the
reference* that a card would never reproduce by accident. Reimplementing a PIL
compositing quirk in a shader would be reimplementing something nobody chose,
and it would drift the first time either side was touched. So vector layers —
plates, labels, vignettes, messages — go through `CpuPainter`. The frame has to
come back from the card for the encoder anyway, so this costs nothing, and it
is where the time never was: glyph rasterisation is 0.7% of composition.

[Re-validate on Windows](#validating-on-windows) is three commands.

## Why this is worth doing at all

Rendering is the only cost that scales with the length of somebody's video, and
it was profiled rather than guessed:

| | share of render time |
|---|---|
| composing frames | **77%** |
| encoding them (x264) | 23% |

Two consequences follow, and the second is the one most GPU work gets wrong.

**Hardware encoding is not the win.** Even at zero cost it caps a render at
1.3×, and it fights the worker pool that carries the other 77% — consumer
NVIDIA drivers limit concurrent encode sessions to a handful, so eight parallel
segments would serialise on the card. Encoding stays on the processor until
measurement says otherwise.

**Text is not the win either.** Inside that 77%, glyph rasterisation was 0.7%.
The expensive part was moving 1920×1080 buffers: a paste, a blend, a channel
split, a convert, per frame. Those are what move to the card.

## Architecture

```
RenderPlan
   ↓
RenderContext.describe(t)     pure; no pixels, no fonts, no filesystem
   ↓
DisplayList                   ONE scene representation
   ↓
Painter                       two implementations
   ├── CpuPainter  (reference)
   └── GpuPainter  (this)
   ↓
frame → ffmpeg → MP4
```

Above `Renderer`, nothing changed and nothing can tell which painter ran. The
Visual Director, the timeline, visual units, grounding, assets, narration and
checkpointing are untouched by this work.

**The invariant: both painters consume the same `DisplayList`.** The moment a
backend needs its own description, the composer has to know which backend it is
describing for, and the boundary is gone.

## What runs where

| | CPU | GPU |
|---|---|---|
| Layout, text, rounded rectangles | ✅ | — |
| Animation engine (charts, diagrams, typography) | ✅ | — |
| Texture upload | — | ✅ |
| Transform + sample (Ken Burns, pan, zoom, crop) | — | ✅ |
| Alpha-over compositing | — | ✅ |
| Transitions (blend, wipe, push) | — | ✅ |
| Vignette | ✅ rasterised | ✅ composited |
| Encoding | ✅ | — |

Text and plates are rasterised by **the reference painter** on both paths, into
a transparent RGBA tile the card composites. That is deliberate: it makes a
glyph bit-identical between the two implementations. Reimplementing type on a
card would be a font-rendering project whose output could never sit inside a
two-per-channel tolerance.

### The honest consequence

A typography-heavy video will not get much faster. Its frames come from the
animation engine on the processor and are then uploaded, and the upload is new
work. An image-heavy video — photographs with camera moves, dissolves between
them — is where the card wins. That is a measurement per project, not a claim,
which is what `scripts/bench_gpu_renderer.py` produces.

## Technology, and why

**OpenGL 3.3 through `moderngl`.**

* **It runs where the customer is.** Available on every Windows machine with a
  driver from the last decade — NVIDIA, AMD, Intel alike — and `moderngl`
  creates a standalone context with no window and no display.
* **It is small.** One C++ extension and a pip wheel. Not a runtime, a
  toolchain or a vendor SDK.
* **It is not CUDA.** Nothing here is NVIDIA-specific.
* **The seam is one file.** Swapping to `wgpu` — which maps onto D3D12, Vulkan
  and Metal, and is the better long-term answer once its Python bindings settle
  — replaces `gpu_painter.py` and nothing else.

Known cost: macOS deprecated OpenGL and caps it at 4.1. It still works; a Metal
path through `wgpu` is the eventual answer there.

## Capability detection

Hardware being present is **not** a capability. A card can be installed and
driverless, installed with a driver too old for the shaders, installed in a
container with no render node, or installed and already holding all its memory.
Every one of those reports as "an RTX 4070 is present" and fails at the first
frame of a sixty-minute render.

So `gpu_probe.probe()` asks the graphics stack to initialise, compile the
pipelines, **draw a calibration frame, and match it against the CPU reference**.
Only then is the card reported usable. A driver that initialises and draws the
*wrong* picture is worse than one that refuses — the render succeeds, the file
plays, and the captions are two pixels off until a customer notices.

Cached against a fingerprint that includes the adapter and driver strings, so a
driver update invalidates it and nothing else needs to.

`VTV_DISABLE_GPU=1` turns it off regardless. A switch that only ever *disables*
is safe to expose; one that forces a capability on is how a user ends up with a
render that cannot start.

## Determinism policy

Not bit-for-bit, and deliberately. Rasterisation on a card may differ from PIL
in the last bit or two — texture filtering is implementation-defined, and an
edge pixel landing on 127 rather than 128 is not a defect.

The rule has two halves and the second is the one that bites:

* no pixel may differ by more than **2** per channel;
* **no pixel may be outside that at all** (`MAX_OUTLIERS = 0`).

A mean-difference check would pass a caption sitting one pixel high on every
frame of a four-hour video. Real errors — a plate misplaced, an overlay
missing, a wipe running backwards — differ in the hundreds and either half
catches them. Subtle errors are caught only by the second.

The harness is tested before it is trusted: pointed at painters that shift a
plate by two pixels and that drop overlays, it must catch both; pointed at two
reference painters, it must pass.

## Fallback

A GPU failure means *this backend cannot do this segment*, never *the project is
broken*:

```
segment 41 → local_gpu ❌ out of video memory
segment 41 → local_cpu ✅
job.backends = {"local_gpu": 118, "local_cpu": 1}
```

Because segments are checkpointed, a failed attempt costs one segment and every
finished segment stays on disk. Every fallback is reported in the event stream
with its reason — a render that quietly moved to different hardware is one
whose timing and cost the user cannot explain.

`GpuRenderBackend` marks almost every failure `elsewhere=True`: driver resets,
lost contexts, exhausted memory and shader failures all say something about the
card and nothing about the timeline.

## Validating on Windows

```powershell
cd "D:\Voice to Video"
.\.venv\Scripts\Activate.ps1

pip install moderngl

# 1. Does the device initialise and draw a matching calibration frame?
python -c "from vtv.adapters.render.gpu_probe import describe; print(describe())"

# 2. The acceptance test: every layer type, every transition, every alpha case.
python -m unittest tests.test_painter_equivalence -v

# 3. Is it actually faster, and on what kind of content?
python scripts\bench_gpu_renderer.py
python scripts\bench_gpu_renderer.py --resolution 4k
```

Step 2 is the one that matters. Until it passes, this is unverified code.

Expected shape of step 3 on a machine where the card wins:

```
gpu        : GPU compositing available: NVIDIA ... via opengl
calibration: worst channel difference 1

correctness
  27 scenes match (worst channel difference 1)

composition (96 frames per workload)
  workload         CPU fps   GPU fps   speedup  verdict
  typography          26.1      27.4     1.05x  CPU (tie or slower)
  images              18.3      74.2     4.05x  GPU
  transitions          9.4      61.8     6.57x  GPU
  mixed               15.7      48.1     3.06x  GPU
```

Those numbers are an **illustration of the format, not a prediction**. Nothing
in this repository has measured a GPU.

## The open question the next run must answer

The reference resizes photographs with **LANCZOS**; a graphics card samples with
its own filter. On a flat colour those are identical — which is why the built-in
calibration still *is* flat, isolating geometry from resampling.

On a real photograph they will differ, and possibly by more than two per
channel. If they do, that is not a bug to fix and not a tolerance to loosen: it
is a policy question about what "the same picture" means when two correct
implementations use different reconstruction filters. The honest answer would
distinguish *geometry, placement, alpha and ordering* — which must match exactly
— from *resampling kernels*, which cannot.

`python scripts/diagnose_gpu_painter.py --photo` is what settles it. The
classifier reports `edges only (texture filtering / resampling kernel)` when a
difference survives nowhere but the edges, which is the signature of exactly
this and of nothing else.

## Diagnosing a failure

```powershell
python scripts\diagnose_gpu_painter.py              # flat still: geometry only
python scripts\diagnose_gpu_painter.py --photo      # detailed still: adds resampling
```

Writes `cpu.png`, `gpu.png` and an amplified `diff.png` per failing scene, plus
`report.json` with dimensions, worst per-channel difference, pixels outside
tolerance, mean absolute difference, bounding box — and a **classified cause**.

The classifier works by transforming the GPU frame until the difference
collapses: if mirroring it vertically takes the worst difference from 184 to 0,
the cause is a mirrored readback and not a matter of opinion. It is tested by
injecting each defect it claims to recognise, the same way the comparison
harness is.

## Known limitations

1. **Awaiting re-verification.** Four defects were found on the first hardware
   run and fixed; the fixes have not themselves been run on a card.
2. **The animation engine is still CPU.** Charts, diagrams and typography are
   rasterised on the processor and uploaded, so typography-heavy projects gain
   little. Moving primitives onto the card is incremental and does not change
   the `DisplayList` contract.
3. **Encoding is CPU.** By measurement, not oversight.
4. **OpenGL on macOS is deprecated.** Works, capped at 4.1; `wgpu` later.
5. **One device per worker process.** Eight segment workers create eight
   contexts. Whether that is better than fewer workers with more work each is a
   measurement nobody has taken.
6. **No user-facing control yet.** Deliberate: the backend becomes real and
   measured first. Fast/Auto/GPU is exposed after a machine passes steps 2
   and 3.
