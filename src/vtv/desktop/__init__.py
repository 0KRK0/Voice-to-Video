"""VTV Desktop: rendering on the customer's own computer.

## What this package is for

The engine can already use a graphics card. This is what turns that into a
product: a small program a customer runs on their own machine, which pairs to
their account, asks for render jobs, draws them locally, and hands the finished
video back. Their hardware, their electricity, no cloud compute billed to
anybody.

## The shape

    pair    once, with a code from the web app  ->  a device token on disk
    run     forever: ask for a job, draw it, report, upload, repeat

Nothing here reimplements rendering. The executor loads the same `Timeline`, the
same `RenderSettings` and the same `FfmpegRenderer` the server uses, with the
same segment checkpointing, the same per-segment routing and the same GPU
painter that was verified against the reference. A frame drawn on a customer's
desktop and the same frame drawn in the cloud differ by no more than the
equivalence tolerance, because they are drawn by the same code.

## The rules this package lives under

**It never holds a provider credential.** No model key, no search key, no
storage credential. It receives a resolved timeline and expiring URLs for that
job's assets. Everything that costs money or carries a secret happened on the
server before the assignment was written.

**It never trusts what it is told about itself.** Hardware is measured, and the
graphics card is not reported as usable until it has drawn the calibration
scenes and matched the reference on this machine.

**It gives work back rather than holding it.** Every claim is a lease that
expires. A closed laptop is a delay, never a stuck render.
"""

from vtv.desktop.hardware import describe, detect

__all__ = ["describe", "detect"]
