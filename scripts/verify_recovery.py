"""Run the two interruption tests without a human holding the stopwatch.

    python scripts/verify_recovery.py            # both tests
    python scripts/verify_recovery.py --test resume
    python scripts/verify_recovery.py --test offline

## Why this exists

Test 1 — pair, claim, render, upload — is easy to run by hand because every step
either works or visibly does not. The two interruption tests are not, and three
attempts failed for the same reason each time: they need a kill at the right
moment, a wait long enough for a lease to expire, two processes whose
environments must agree, and a measurement taken *while* something is running
rather than after it. Every one of those is a chance for a person to be a second
late or a terminal to be configured differently, and none of them is what is
under test.

So this owns the whole scene: it starts its own server on its own port with its
own throwaway database, seeds a project, pairs a device, runs the desktop as a
child process, and kills things on a clock. Nothing is shared with a running
setup, so it can be run while your own server is up.

## What it is careful about

**The resume test measures during the run, not after.** A delivered job deletes
its own scratch directory, so a check afterwards finds nothing — which is what
defeated two attempts. This samples the finished segments' modification times
several times a second for the whole of the resume, and fails if any of them
ever moves.

**The offline test is honest about what it simulates.** It stops the *server*
rather than pulling a network cable, so the device gets connection-refused where
a real Wi-Fi drop would give it a hang until timeout. The code path is the same
one — an exception out of the HTTP call — but the timing is not, and a genuine
adapter test is still worth doing once by hand. This says so in its output
rather than claiming more than it did.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Sampling interval while a render runs. Fast enough that a segment being
#: rewritten cannot slip between two samples — a segment takes seconds to encode
#: and this looks four times a second.
SAMPLE_SECONDS = 0.25


def _post(url: str, payload: dict | None = None) -> tuple[int, str]:
    body = json.dumps(payload or {}).encode()
    request = urllib.request.Request(
        url, data=body, headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()
    except Exception as error:  # noqa: BLE001 - the server may simply be down
        return 0, str(error)


def walk(root: Path, pattern: str = "*") -> list[Path]:
    """Every match under `root`, tolerating the tree vanishing mid-walk.

    `Path.rglob` is a generator that reads directories as it goes, so a
    directory removed while it is iterating raises out of the **loop header**,
    where a `try` around the loop body cannot catch it.

    That is not a hypothetical race here, it is the normal end of every job: a
    delivered render deletes its own scratch directory, and anything sampling
    that directory is by definition still walking it when it goes. It cost a
    240-minute render — 1200 segments drawn correctly over six hours, delivered,
    and then reported as `FileNotFoundError` because the measurement fell over
    while tidying up behind itself.
    """
    found: list[Path] = []
    try:
        for path in root.rglob(pattern):
            found.append(path)
    except OSError:
        # Whatever was collected before the tree moved. A partial sample is the
        # right answer for a directory that is being deleted; an exception is
        # not an answer at all.
        pass
    return found


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()
    except Exception as error:  # noqa: BLE001
        return 0, str(error)


@dataclass
class Segments:
    """What is on disk, and when it was last written.

    Nanosecond modification times, because a segment redrawn quickly could land
    in the same whole second as the original and look untouched.
    """

    seen: dict[str, tuple[int, int]] = field(default_factory=dict)

    @classmethod
    def under(cls, root: Path) -> Segments:
        found: dict[str, tuple[int, int]] = {}
        for path in walk(root, "seg-*.mp4"):
            # `.part` files are still being written by definition; only
            # finished segments are checkpoints worth protecting.
            if ".part" in path.name:
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            found[path.name] = (info.st_mtime_ns, info.st_size)
        return cls(seen=found)

    def redrawn(self, later: Segments) -> list[str]:
        """Segments this snapshot holds that `later` has *changed*."""
        return [
            name
            for name, stamp in self.seen.items()
            if name in later.seen and later.seen[name] != stamp
        ]


class Scene:
    """A whole VTV installation, on its own port, in a temporary directory."""

    def __init__(self, port: int, keep: bool = False) -> None:
        self.port = port
        self.keep = keep
        self.root = Path(tempfile.mkdtemp(prefix="vtv-recovery-"))
        self.server: subprocess.Popen | None = None
        #: Where the request log goes when `start_server(access_log=True)`.
        self.log: Path | None = None
        self.desktop: subprocess.Popen | None = None
        self.worker: subprocess.Popen | None = None
        self.worker_log: Path | None = None
        self.base = f"http://127.0.0.1:{port}"

        self.env = dict(os.environ)
        self.env.update(
            {
                "VTV_ENV": "development",
                "VTV_STORAGE_ROOT": str(self.root / "storage"),
                "VTV_DATABASE_URL": "sqlite:///"
                + str(self.root / "vtv.db").replace("\\", "/"),
                "VTV_SIGNING_KEY": "recovery-check-not-a-real-secret",
                "VTV_DESKTOP_HOME": str(self.root / "desktop"),
                "VTV_ASSET_SEARCH_ENDPOINT": "",
                # Seven minutes is the production default and the right one
                # there; a verification that waited it out would be seven
                # minutes of a person watching a blank terminal, which is
                # how checks stop being run. The behaviour under test is the
                # escalation, not the length of the fuse.
                "VTV_DEVICE_STRANDED_SECONDS": "20",
                "PYTHONPATH": str(ROOT / "src"),
                # Unbuffered, or the child's prints arrive in a lump at exit and
                # a live log is useless for seeing where a failure happened.
                "PYTHONUNBUFFERED": "1",
            }
        )
        #: Where the desktop keeps part-finished jobs. Derived from the same
        #: environment the child gets, so the two cannot disagree — which is
        #: precisely the mistake that defeated the manual attempts.
        self.workspace = self.root / "desktop" / "work" / "jobs"

    # -- processes ---------------------------------------------------------

    def start_server(self, *, access_log: bool = False) -> None:
        """Start the API. `access_log` keeps the request log for counting.

        Off by default because these harnesses run for hours and a four-hour
        render is thousands of progress reports. On when something needs to be
        *measured* from the server's own view — how often a device actually
        polled, say — which is the only honest way to check a poll rate: the
        alternative is asserting about the constant that sets it, which tests
        arithmetic rather than behaviour.
        """
        self.log = self.root / "server.log" if access_log else None
        sink = self.log.open("ab") if self.log else subprocess.DEVNULL
        self.server = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "--factory",
                "vtv.api.app:create_app",
                "--host", "127.0.0.1", "--port", str(self.port),
                "--log-level", "info" if access_log else "warning",
            ],
            cwd=ROOT, env=self.env,
            stdout=sink, stderr=subprocess.STDOUT if access_log else sink,
        )
        for _ in range(120):
            if _get(f"{self.base}/health")[0] == 200:
                return
            time.sleep(0.5)
        raise SystemExit(f"the server did not come up on port {self.port}")

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.kill()
            self.server.wait(timeout=30)
            self.server = None

    def start_worker(self) -> subprocess.Popen:
        """The cloud worker, as its own process.

        Needed because "render in the cloud" is a real path with a real handler
        and nothing had ever run one: the unit tests check that the handler is
        registered under the right kind, which is not the same as checking that
        it draws a video. A job kind nobody executes sits pending forever, and a
        pending job is not something any part of the system reports as broken.
        """
        # **Its output is kept.** It went to `DEVNULL`, which is the same
        # mistake as every silent subprocess in this project's history: a worker
        # that fails to start on one platform then looks exactly like a worker
        # that started and had nothing to do, and the check that depends on it
        # fails with no clue why. Twice.
        self.worker_log = self.root / "worker.log"
        sink = self.worker_log.open("ab")
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "vtv.worker"],
            cwd=ROOT, env=self.env,
            stdout=sink, stderr=subprocess.STDOUT,
        )
        return self.worker

    def job_failures(self) -> str:
        """Why the queue's jobs failed, read from the queue itself.

        The `job.failed` event carries a code; the **row** carries the message,
        and `internal_error` is precisely the code that means "an exception
        nobody anticipated" — the one case where the category says least. Three
        identical `internal_error` lines told us a cloud render failed and
        nothing whatever about why.

        Read straight out of the database because that is where the truth is,
        and because a verification tool that can only see what a log chose to
        print is a verification tool with a blind spot the size of the bug.
        """
        import sqlite3

        db = self.root / "queue.db"
        if not db.exists():
            return "there is no queue database to read"
        try:
            with sqlite3.connect(db) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT kind, state, attempts, last_error FROM jobs "
                    "WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 5"
                ).fetchall()
        except Exception as error:  # noqa: BLE001
            return f"the queue database could not be read: {error}"
        if not rows:
            return "no job in the queue has recorded a failure"

        lines = []
        for row in rows:
            try:
                info = json.loads(str(row["last_error"]))
                message = str(info.get("message") or "")
            except Exception:  # noqa: BLE001
                message = str(row["last_error"])
            lines.append(
                f"{row['kind']} [{row['state']}, attempt {row['attempts']}]: "
                f"{message[:400]}"
            )
        return "\n         ".join(lines)

    def worker_trouble(self) -> str:
        """Why the worker is not doing its job, as far as can be seen."""
        if self.worker is None:
            return "no worker was started"
        code = self.worker.poll()
        tail = ""
        if self.worker_log is not None and self.worker_log.exists():
            text = self.worker_log.read_text(encoding="utf-8", errors="replace")
            tail = text.strip().splitlines()[-12:] and "\n         ".join(
                text.strip().splitlines()[-12:]
            )
        if code is not None:
            return f"the worker exited with status {code}\n         {tail}"
        return f"the worker is running\n         {tail or '(it has said nothing)'}"

    def start_desktop(self, *extra: str) -> subprocess.Popen:
        self.desktop = subprocess.Popen(
            [sys.executable, "-m", "vtv.desktop.cli", "run", "--workers", "1", *extra],
            cwd=ROOT, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        return self.desktop

    def kill_desktop(self) -> None:
        """A hard kill, which is the whole point.

        Not `terminate`, and certainly not an interrupt: Ctrl-C now asks the
        loop to finish the job in hand, which is the correct product behaviour
        and the opposite of what this test needs.
        """
        if self.desktop is not None and self.desktop.poll() is None:
            self.desktop.kill()
            self.desktop.wait(timeout=30)

    def close(self) -> None:
        self.kill_desktop()
        if self.worker is not None and self.worker.poll() is None:
            self.worker.kill()
            self.worker.wait(timeout=30)
        self.stop_server()
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)

    # -- setting the scene -------------------------------------------------

    def seed(self, seconds: float, *extra: str) -> str:
        result = subprocess.run(
            [
                sys.executable, "scripts/seed_device_job.py",
                "--seconds", str(seconds), "--photos",
                "--workdir", str(self.root), *extra,
            ],
            cwd=ROOT, env=self.env, capture_output=True, text=True,
            # A four-hour fixture spends minutes synthesising narration before
            # it prints anything, and a default timeout here would look exactly
            # like a seeding failure.
            timeout=3600,
        )
        if result.returncode != 0:
            raise SystemExit(f"seeding failed:\n{result.stdout}\n{result.stderr}")
        for line in result.stdout.splitlines():
            if line.startswith("project"):
                return line.split(":", 1)[1].strip()
        raise SystemExit(f"could not read a project id from:\n{result.stdout}")

    def pair(self) -> None:
        status, body = _post(f"{self.base}/v1/devices/codes")
        if status != 201:
            raise SystemExit(f"could not mint a pairing code ({status}): {body}")
        code = json.loads(body)["code"]
        result = subprocess.run(
            [
                sys.executable, "-m", "vtv.desktop.cli", "pair",
                "--code", code, "--server", self.base,
                "--name", "recovery check", "--skip-gpu-check",
            ],
            cwd=ROOT, env=self.env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"pairing failed:\n{result.stdout}\n{result.stderr}")

    def queue(self, project: str) -> str:
        status, body = _post(f"{self.base}/v1/projects/{project}/render/device")
        if status != 202:
            raise SystemExit(f"queueing failed ({status}): {body}")
        return json.loads(body)["render_job_id"]

    def wait_for_segments(self, *, at_least: int, timeout: float) -> Segments:
        """Block until the device has finished this many segments."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = Segments.under(self.workspace)
            if len(found.seen) >= at_least:
                return found
            time.sleep(SAMPLE_SECONDS)
        return Segments.under(self.workspace)


# -- the tests -------------------------------------------------------------


def test_resume(scene: Scene, args: argparse.Namespace) -> bool:
    print("\n=== Test 2 — kill the desktop mid-render, then resume ===\n")

    project = scene.seed(args.seconds)
    scene.pair()
    print(f"seeded {args.seconds:.0f}s project {project}")

    scene.start_desktop()
    time.sleep(2.0)
    job = scene.queue(project)
    print(f"queued {job}; waiting for {args.finished_before_kill} finished segments")

    before = scene.wait_for_segments(
        at_least=args.finished_before_kill, timeout=args.render_timeout
    )
    if len(before.seen) < args.finished_before_kill:
        print(
            f"FAIL: only {len(before.seen)} segments were finished before the "
            f"timeout. Try --seconds larger or --render-timeout longer."
        )
        return False

    scene.kill_desktop()
    print(f"killed the desktop with {len(before.seen)} segments on disk:")
    for name, (stamp, size) in sorted(before.seen.items()):
        print(f"    {name}  {size:>9} bytes  mtime_ns={stamp}")

    print(f"\nwaiting {args.lease_wait:.0f}s for the stale claim to be reapable...")
    time.sleep(args.lease_wait)

    print("resuming, and sampling the checkpoints throughout\n")
    started = time.monotonic()
    running = scene.start_desktop("--max-jobs", "1")

    # The measurement. A delivered job deletes its own scratch directory, so a
    # check *after* the resume finds nothing — which is what defeated two
    # attempts by hand. Sampling during the run cannot be defeated that way.
    samples = 0
    redrawn: set[str] = set()
    #: The most segments ever seen together in the workspace. If the resume
    #: never puts more there than the kill left behind, it is drawing somewhere
    #: else — which is exactly what happened on the fourth manual attempt, where
    #: the desktop ran with a different workspace and redrew all 120 seconds
    #: while the checkpoints sat untouched in another directory. An
    #: untouched-checkpoints check alone calls that a pass. This does not.
    high_water = len(before.seen)
    while running.poll() is None:
        now = Segments.under(scene.workspace)
        redrawn.update(before.redrawn(now))
        high_water = max(high_water, len(now.seen))
        samples += 1
        time.sleep(SAMPLE_SECONDS)
    elapsed = time.monotonic() - started
    output = (running.stdout.read() if running.stdout else "") or ""

    finished = [line for line in output.splitlines() if "done in" in line]
    print("\n".join(f"    {line.strip()}" for line in finished) or "    (no result line)")
    print(f"\n    resume took {elapsed:.0f}s")
    print(f"    sampled the checkpoints {samples} times during it")
    print(f"    segments in the workspace at its fullest: {high_water}")

    if not finished:
        print("\nFAIL: the resumed run produced no completed job.")
        print(output[-2000:])
        return False
    if redrawn:
        print(f"\nFAIL: these finished segments were redrawn: {sorted(redrawn)}")
        print("The resume started from scratch instead of using the checkpoints.")
        return False
    if high_water <= len(before.seen):
        print(
            "\nFAIL: the workspace never held more than the "
            f"{len(before.seen)} segments the kill left behind, so the resume "
            "drew somewhere else. Untouched checkpoints prove nothing in that "
            "case — they were simply not the ones being used."
        )
        return False

    print(
        f"\nPASS: the resume wrote into the same workspace (it reached "
        f"{high_water} segments) and none of the {len(before.seen)} it "
        "inherited was touched. It drew only what was missing."
    )
    return True


def test_offline(scene: Scene, args: argparse.Namespace) -> bool:
    print("\n=== Test 3 — the server goes away mid-render ===\n")
    print("Simulated by stopping the server, so the device gets connection-")
    print("refused where a real Wi-Fi drop would hang until timeout. Same code")
    print("path, different timing — a manual adapter test is still worth one run.\n")

    project = scene.seed(args.seconds)
    scene.pair()
    scene.start_desktop()
    time.sleep(2.0)
    job = scene.queue(project)
    print(f"queued {job}; waiting for the device to start drawing")

    working = scene.wait_for_segments(at_least=1, timeout=args.render_timeout)
    if not working.seen:
        print("FAIL: the device never started rendering.")
        return False
    print(f"it has {len(working.seen)} segment(s); pulling the server away now")

    scene.stop_server()
    before_outage = len(Segments.under(scene.workspace).seen)
    time.sleep(args.outage)
    during = Segments.under(scene.workspace)
    alive = scene.desktop is not None and scene.desktop.poll() is None

    print(f"\n  server down for {args.outage:.0f}s")
    print(f"  segments finished during the outage : {len(during.seen) - before_outage}")
    print(f"  the desktop is still running        : {alive}")

    if not alive:
        print("\nFAIL: the desktop died when the server became unreachable.")
        return False
    if len(during.seen) <= before_outage:
        print(
            "\nINCONCLUSIVE: no segment finished during the outage, so this did "
            "not prove that rendering continues offline. Try --outage longer."
        )
        return False

    print("\n  bringing the server back")
    scene.start_server()

    # Waiting for the process to exit would prove nothing — it has no job limit
    # and would still be polling at the timeout either way. Delivery is the
    # signal worth waiting for: a job that reaches the server has its scratch
    # directory removed, so the directory going away *is* the receipt.
    job_dir = scene.workspace / job
    deadline = time.monotonic() + args.reconnect_timeout
    delivered = False
    while time.monotonic() < deadline:
        if not job_dir.exists():
            delivered = True
            break
        time.sleep(SAMPLE_SECONDS)
    waited = args.reconnect_timeout - max(deadline - time.monotonic(), 0.0)

    alive = scene.desktop.poll() is None
    status, body = _get(f"{scene.base}/v1/devices/capacity")
    print(f"  job delivered after the server returned: {delivered} ({waited:.0f}s)")
    print(f"  the desktop is still running           : {alive}")
    print(f"  server sees                            : {body if status == 200 else status}")

    if not alive:
        print("\nFAIL: the desktop stopped rather than carrying on.")
        return False
    if not delivered:
        print(
            f"\nFAIL: {args.reconnect_timeout:.0f}s after the server came back the "
            "job was still sitting in the workspace undelivered."
        )
        return False

    print(
        "\nPASS: drawing continued with no server, the process survived, and the "
        "job was delivered once the server returned."
    )
    print(
        "  Note: this outage was shorter than the lease. A longer one has the "
        "work correctly discarded instead, because by then another machine may "
        "already own the job."
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--test", choices=("resume", "offline", "both"), default="both")
    parser.add_argument("--port", type=int, default=8931,
                        help="a port of its own, so a server you are already running is untouched")
    parser.add_argument("--seconds", type=float, default=150.0,
                        help="length of the seeded video; long enough that a kill lands mid-render")
    parser.add_argument("--finished-before-kill", type=int, default=2)
    parser.add_argument("--render-timeout", type=float, default=600.0)
    parser.add_argument("--lease-wait", type=float, default=75.0,
                        help="the server will not reap a claim sooner than this")
    parser.add_argument("--outage", type=float, default=45.0)
    parser.add_argument("--reconnect-timeout", type=float, default=240.0)
    parser.add_argument("--keep", action="store_true", help="leave the sandbox on disk")
    args = parser.parse_args(argv)

    results: dict[str, bool] = {}
    for name in (("resume", "offline") if args.test == "both" else (args.test,)):
        scene = Scene(port=args.port, keep=args.keep)
        try:
            scene.start_server()
            results[name] = (
                test_resume(scene, args) if name == "resume"
                else test_offline(scene, args)
            )
        finally:
            if args.keep:
                print(f"\nsandbox kept at {scene.root}")
            scene.close()
        # A fresh port per scenario, so a socket in TIME_WAIT from the first
        # cannot make the second look like a server that would not start.
        args.port += 1

    print("\n" + "=" * 60)
    for name, passed in results.items():
        print(f"  {name:<10} {'PASS' if passed else 'FAIL'}")
    print("=" * 60)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
