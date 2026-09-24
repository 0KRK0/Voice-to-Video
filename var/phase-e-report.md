# Phase E — long-video validation

`Windows-10-10.0.26200-SP0`, Python 3.11.4.

| Length | Result | Render | vs realtime | Segments | CPU/GPU | Peak RSS | MB/segment | Peak scratch | Peak VRAM | Output | Duration error |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 120 min | PASS | 142.0 min | 0.84x | 600 | 2/598 | 465 MB | -0.095 | 4597 MB | 505 MB | 962 MB | +0.00s |
| 240 min | PASS | 341.4 min | 0.70x | 1200 | 2/1198 | 471 MB | -0.113 | 9211 MB | 916 MB | 1924 MB | +0.00s |


## Reading the columns that matter

**MB/segment** is the least-squares slope of resident memory against finished segments. Near zero is what a renderer that releases what it allocates looks like. A few megabytes is invisible at thirteen segments and is a gigabyte by twelve hundred, which is the whole reason for measuring at length rather than inferring from a short run. A dash means too few segments to fit a line through; short rungs will not answer this question and do not pretend to.

**Duration error** is the output measured by `ffprobe` against what the timeline asked for. This is the column that catches a render which reports every segment complete and produces a file that stops early.

**Peak scratch** is a high-water mark taken during the run. Measuring afterwards always reads zero, because a delivered job removes its own directory.

Run with `--minutes 120,240`.
