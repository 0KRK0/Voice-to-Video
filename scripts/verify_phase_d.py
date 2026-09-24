"""Phase D over real HTTP, with nothing to set up.

    python scripts/verify_phase_d.py

Same shape as `verify_recovery.py` and for the same reason: three attempts at
Phase C by hand failed on terminal environments and stopwatch timing, none of
which was what was under test. This owns its own server, its own database and
its own desktop process, so it can run while your own server is up and there is
nothing to get wrong.

## What it proves that the unit tests cannot

The unit tests run every one of these behaviours against `TestClient`, which is
the application in-process. Four of the twelve bugs Phase C produced lived in
exactly the gap that leaves — a signed URL that is relative, a route nobody
mounted, a client that cannot stream a file handle, a CLI module with no
`__main__` guard. So this drives the **real** binary over the **real** wire:

1. **Auto picks a device when one is available, the cloud when none is.** With
   a real desktop process paired and running, then stopped.
2. **The Studio can play a video its own server never drew.** The whole point of
   the feature, and the thing Phase C skipped: it verified the device render with
   `ffprobe` on the file in storage and never asked whether the product could
   find it. `GET /projects/{id}/video` is asked here, over HTTP, and the bytes
   are checked.
3. **The idle poll widens, and pressing Render snaps it back.** Measured from
   the server's own request log rather than asserted about the constants.
4. **A browser sign-in pairs a computer**, with the approval done the way a
   browser would do it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_recovery import ROOT, Scene, _get  # noqa: E402


def _status(url: str) -> int:
    """Just the status code, without reading the body as text.

    `_get` decodes every response as UTF-8, which is right for JSON and
    fatal for an MP4: polling `/video` with it raised `UnicodeDecodeError`
    on the first success, the helper swallowed that as a network error, and
    the harness reported that the Studio could not find a render the server
    log showed it serving with a 200 every two seconds. A false negative in
    a verification tool is its own kind of lie, and a slower one to notice
    than a false positive.
    """
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except Exception:  # noqa: BLE001
        return 0


def _request(url: str, payload: dict | None = None, method: str = "POST") -> tuple[int, str]:
    body = json.dumps(payload or {}).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=body, headers={"content-type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()
    except Exception as error:  # noqa: BLE001
        return 0, str(error)


class Check:
    """A named assertion with the evidence printed either way."""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def that(self, name: str, passed: bool, evidence: str = "") -> bool:
        self.results.append((name, bool(passed), evidence))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}" + (f"\n         {evidence}" if evidence else ""),
              flush=True)
        return bool(passed)

    @property
    def ok(self) -> bool:
        return all(passed for _, passed, _ in self.results)


def verify(scene: Scene, args: argparse.Namespace, check: Check) -> None:
    print("\n=== 1. Where a render runs ===\n")

    project = scene.seed(args.seconds)
    where = f"{scene.base}/v1/projects/{project}/render/where"

    status, body = _get(where)
    options = {row["execution"]: row for row in json.loads(body)["options"]}
    check.that(
        "with no computer paired, Auto resolves to the cloud",
        options["auto"]["resolves_to"] == "cloud",
        options["auto"]["reason"],
    )
    check.that(
        "and 'this device' is unavailable, with a sentence saying why",
        not options["device"]["available"]
        and "desktop app" in options["device"]["reason"],
        options["device"]["reason"],
    )

    scene.pair()
    scene.start_desktop()
    # The desktop announces itself on its first poll; until then the server has
    # a paired computer it has never heard from, which is correctly not capacity.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        options = {row["execution"]: row for row in json.loads(_get(where)[1])["options"]}
        if options["auto"]["resolves_to"] == "device":
            break
        time.sleep(1.0)

    status, body = _get(where)
    options = {row["execution"]: row for row in json.loads(body)["options"]}
    check.that(
        "with a computer running, Auto resolves to it and names it",
        options["auto"]["resolves_to"] == "device"
        and "recovery check" in options["auto"]["reason"],
        options["auto"]["reason"],
    )

    print("\n=== 2. The Studio can play what the desktop drew ===\n")

    status, body = _request(
        f"{scene.base}/v1/projects/{project}/render/device", {}
    )
    check.that("queueing for a device is accepted", status == 202, body[:160])
    if status != 202:
        return
    chosen = json.loads(body)
    check.that(
        "and it says device, not cloud",
        chosen.get("execution") == "device",
        f"execution={chosen.get('execution')} reason={chosen.get('reason')}",
    )

    print("\n  waiting for the render...", flush=True)
    started = time.monotonic()
    video = f"{scene.base}/v1/projects/{project}/video"
    playable = False
    while time.monotonic() - started < args.render_timeout:
        if _status(video) == 200:
            playable = True
            break
        time.sleep(2.0)
    took = time.monotonic() - started

    check.that(
        "GET /projects/{id}/video serves a video the server never drew",
        playable,
        f"after {took:.0f}s"
        + ("" if playable else " — the Studio cannot find the finished render"),
    )
    if playable:
        out = scene.root / "downloaded.mp4"
        with urllib.request.urlopen(video, timeout=120) as response:
            out.write_bytes(response.read())
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_name", "-of", "json", str(out),
            ],
            capture_output=True, text=True, timeout=120,
        )
        info = json.loads(probe.stdout or "{}")
        seconds = float(info.get("format", {}).get("duration", 0.0))
        codecs = sorted(s.get("codec_name", "?") for s in info.get("streams", []))
        check.that(
            "the bytes it served are a real video of the right length",
            abs(seconds - args.seconds) <= max(1.0, args.seconds * 0.02)
            and "h264" in codecs,
            f"{seconds:.2f}s, codecs {codecs}, {out.stat().st_size / 1e6:.1f} MB",
        )

        status, body = _get(f"{scene.base}/v1/projects/{project}/renders")
        recorded = json.loads(body) if status == 200 else {}
        rows = recorded.get("renders") or recorded.get("items") or []
        check.that(
            "and the render appears in the project's history",
            bool(rows),
            f"{len(rows)} row(s)",
        )

    print("\n=== 3. A settled machine wakes when somebody presses Render ===\n")

    # This check has been wrong twice, and both times the fix was to the
    # measurement rather than to the product.
    #
    # It first counted claims in a window after Render against a quiet window,
    # which cannot work: a device that has just taken a job spends the next two
    # minutes *rendering* and makes no claims at all, so the busy window always
    # counted fewer. It measured the opposite of what it claimed to.
    #
    # Timing the wake instead is what found the real defect. An adaptive poll
    # ladder hit its traffic target — a real machine settled to a 52-second gap
    # — and then took **49 seconds** to start a render somebody was watching
    # for, because a `Retry-After` hint cannot shorten a sleep the device is
    # already in. That is what put the long-poll in: the machine is connected
    # when the job appears, and the same measurement now reads under a second.
    print(f"  watching an idle machine for {args.settle:.0f}s...", flush=True)
    widest = _widest_gap(scene, args.settle)
    check.that(
        "an idle computer asks far less often than every five seconds",
        widest >= 12.0,
        f"longest gap between claims arriving: {widest:.0f}s "
        f"(a flat five-second poll would be 5s)",
    )

    _request(f"{scene.base}/v1/projects/{project}/render/device", {})
    woke = _seconds_to_next_claim(scene, limit=args.settle)
    check.that(
        "and wakes within seconds of Render, not within its own interval",
        woke is not None and woke < max(8.0, widest / 2),
        f"claimed {woke:.1f}s after Render was pressed" if woke is not None
        else f"no claim within {args.settle:.0f}s",
    )

    print("\n=== 4. A render nobody claimed reaches the cloud ===\n")

    # Two things had never run: the cloud handler, and the only path that
    # produces work for it.
    #
    # The cloud kind was added with a handler, a registration, and a test
    # asserting the two spellings agreed — and nothing anywhere enqueued one. It
    # could only be reached by a path that did not exist. What gives it a real
    # producer is this: a render routed to somebody's computer, which then shut
    # its lid. "Auto" asked whether a machine was available at the moment the
    # button was pressed, which was the right question then; it stops being the
    # right answer ninety seconds later, and the job would otherwise sit pending
    # forever with nothing in the system calling that broken.
    # **The computer goes away first.** This queued the render and killed the
    # desktop afterwards, which tests something else entirely and does so
    # unreliably: with a held claim an idle machine takes a job in about a third
    # of a second, so the job was claimed and *then* orphaned. Escalation
    # deliberately does not touch claimed work — that is the queue's job, on the
    # reclaim window — so the check depended on the desktop still being busy
    # with the previous render when this one was queued.
    #
    # On a slow processor it was, and the check passed. On a real graphics card
    # the previous render had already finished, the machine was idle, and it
    # grabbed the job before it could be killed. **A check that passes because
    # the machine running it is slow is not a check.**
    stranded_project = scene.seed(args.seconds)
    scene.kill_desktop()
    # Long enough that a claim already in flight has landed and been abandoned
    # rather than arriving after the kill.
    time.sleep(2.0)

    status, _ = _request(
        f"{scene.base}/v1/projects/{stranded_project}/render/device", {}
    )
    check.that("a render is queued for a computer that is no longer there", status == 202)

    scene.start_worker()
    # Asked separately, because "the render was stranded" is the symptom of two
    # completely different faults — the escalation not firing, and the cloud
    # worker not being there to do the work — and a check that cannot tell them
    # apart sends somebody looking in the wrong half of the system.
    time.sleep(8.0)
    check.that(
        "the cloud worker is running",
        scene.worker is not None and scene.worker.poll() is None,
        scene.worker_trouble(),
    )

    video = f"{scene.base}/v1/projects/{stranded_project}/video"
    started = time.monotonic()
    rescued = False
    while time.monotonic() - started < args.render_timeout:
        if _status(video) == 200:
            rescued = True
            break
        time.sleep(2.0)
    check.that(
        "and the cloud finishes it once no computer takes it",
        rescued,
        f"after {time.monotonic() - started:.0f}s"
        if rescued
        else (
            f"still stranded after {time.monotonic() - started:.0f}s\n"
            f"         WHY: {scene.job_failures()}\n"
            f"         WORKER: {scene.worker_trouble()}"
        ),
    )

    print("\n=== 5. Signing in through a browser ===\n")


    status, body = _request(
        f"{scene.base}/v1/devices/pair/start",
        {"name": "A second computer", "hardware": {"cpu_cores": 4}},
    )
    check.that("a computer can ask to be paired with no credential", status == 201,
               body[:160])
    if status != 201:
        return
    started_pairing = json.loads(body)

    status, _ = _request(
        f"{scene.base}/v1/devices/pair/collect",
        {"device_code": started_pairing["device_code"]},
    )
    check.that(
        "and collects nothing until a person approves it",
        status == 202,
        f"status {status}",
    )

    status, body = _get(
        f"{scene.base}/v1/devices/pair/requests/{started_pairing['user_code']}"
    )
    check.that(
        "the approval screen can name the computer being approved",
        status == 200 and json.loads(body)["name"] == "A second computer",
        body[:160],
    )

    _request(
        f"{scene.base}/v1/devices/pair/approve",
        {"code": started_pairing["user_code"]},
    )
    status, body = _request(
        f"{scene.base}/v1/devices/pair/collect",
        {"device_code": started_pairing["device_code"]},
    )
    check.that("after approval the computer gets its token", status == 201, body[:80])
    if status != 201:
        return
    token = json.loads(body)["token"]

    claimed = urllib.request.Request(
        f"{scene.base}/v1/devices/claim",
        data=json.dumps({"hardware": {"cpu_cores": 4}}).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(claimed, timeout=30) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        code = error.code
    check.that(
        "and that token really works as a device credential",
        code in (200, 204),
        f"claim returned {code}",
    )


def _widest_gap(scene: Scene, seconds: float) -> float:
    """The longest gap between claims observed while watching for `seconds`.

    Evidence that the ladder actually opened on this machine, rather than an
    assertion about the constants that set it — which would test arithmetic
    nobody doubted.
    """
    last = _claims(scene)
    since = time.monotonic()
    widest = 0.0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        now = _claims(scene)
        if now != last:
            widest = max(widest, time.monotonic() - since)
            last, since = now, time.monotonic()
        time.sleep(0.5)
    # A machine that made no claim at all in the window has a gap of at least
    # the window itself.
    return max(widest, time.monotonic() - since)


def _seconds_to_next_claim(scene: Scene, *, limit: float) -> float | None:
    started = time.monotonic()
    before = _claims(scene)
    while time.monotonic() - started < limit:
        if _claims(scene) > before:
            return time.monotonic() - started
        time.sleep(0.25)
    return None


def _claims(scene: Scene) -> int:
    """How many times a device has asked for work, counted from the request log.

    The first version of this counted *devices* and called them claims, so the
    two windows were always equal and the check could only ever pass. Counting
    the actual requests is the only thing that measures a poll rate; the
    alternative — asserting that the ladder constants multiply correctly — tests
    arithmetic that was never in doubt.
    """
    if scene.log is None or not scene.log.exists():
        return 0
    text = scene.log.read_text(encoding="utf-8", errors="replace")
    return text.count("POST /v1/devices/claim")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8941)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--render-timeout", type=float, default=900.0)
    parser.add_argument("--settle", type=float, default=150.0,
                        help="how long to let the idle poll widen before timing a wake")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    check = Check()
    scene = Scene(port=args.port, keep=args.keep)
    try:
        # The request log is the measurement for the polling check.
        scene.start_server(access_log=True)
        verify(scene, args, check)
    finally:
        if args.keep:
            print(f"\nsandbox kept at {scene.root}")
        scene.close()

    print("\n" + "=" * 64)
    failed = [name for name, passed, _ in check.results if not passed]
    print(f"  {len(check.results) - len(failed)}/{len(check.results)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    print("=" * 64)
    return 0 if check.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
