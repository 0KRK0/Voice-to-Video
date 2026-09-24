"""What a customer actually types.

    vtv-desktop pair --code ABC-DEF --server https://app.example.com
    vtv-desktop hardware
    vtv-desktop run
    vtv-desktop status
    vtv-desktop unpair

Four verbs and one of them is a diagnostic. That is the whole surface, and it is
small on purpose: this program runs on a machine somebody uses for something
else, and every option is a thing they have to decide about a background process
they would rather not think about.

## Why `hardware` exists as its own command

Because "why isn't my graphics card being used" is the question this feature
will generate more than any other, and the answer has to be available without
pairing, without a job, and without reading a log. It prints what was measured
and, when the card was refused, the sentence saying why.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import time
import sys
from pathlib import Path
from typing import Any

from vtv.contracts.errors import VTVError
from vtv.desktop import state
from vtv.desktop.agent import Agent
from vtv.desktop.cache import AssetCache
from vtv.desktop.client import IDLE_POLL_SECONDS, DeviceClient
from vtv.desktop.hardware import describe, detect
from vtv.observability.events import tolerant_streams


def _workspace(given: str | None) -> Path:
    return Path(given) if given else state.config_root() / "work"


async def _pair(args: argparse.Namespace) -> int:
    """Link this computer to an account, by browser or by typed code."""
    hardware = detect(probe_gpu=not args.skip_gpu_check)
    print(describe(hardware))
    print()

    name = args.name or _default_name()
    if not args.code:
        return await _pair_in_browser(args, hardware, name)
    return await _pair_with_code(args, hardware, name)


async def _pair_in_browser(
    args: argparse.Namespace, hardware: Any, name: str
) -> int:
    """Sign in the way every other desktop application does.

    Run it, a browser opens, you approve, it is paired. No code to carry from
    one machine to another, and nothing to be signed in as *before* starting —
    which is the case the typed-code flow cannot serve at all: one person, one
    laptop, who has never opened the web app on this machine.

    ## Why the browser is opened rather than required

    `webbrowser.open` fails silently on a headless box, over SSH, and inside a
    container — all of which are places somebody legitimately wants a render
    node. So the URL and the code are printed first and always, and the browser
    is a convenience on top. A flow that only works when a browser launches is a
    flow that strands exactly the machines most worth having.
    """
    import httpx

    async with httpx.AsyncClient(
        base_url=args.server.rstrip("/"), timeout=30.0
    ) as client:
        response = await client.post(
            "/v1/devices/pair/start",
            json={"name": name, "hardware": hardware.model_dump(mode="json")},
        )
        if response.status_code >= 400:
            print(f"could not start pairing ({response.status_code}): "
                  f"{response.text[:200]}")
            return 1
        started = response.json()

        code = str(started["user_code"])
        url = str(started.get("verification_uri_complete") or started["verification_uri"])
        interval = float(started.get("interval") or 3)

        print(f"Approve this computer at:  {url}")
        print(f"and check the code shown there is:  {code}")
        print()
        _open_browser(url)
        print("waiting for approval (Ctrl-C to stop)...", flush=True)

        deadline = time.monotonic() + _pairing_window(started)
        while time.monotonic() < deadline:
            collected = await client.post(
                "/v1/devices/pair/collect",
                json={
                    "device_code": started["device_code"],
                    "hardware": hardware.model_dump(mode="json"),
                },
            )
            if collected.status_code == 201:
                return _remember(args, collected.json(), name)
            if collected.status_code != 202:
                # Anything but "still waiting" is terminal: expired, already
                # collected, or a request that no longer exists. Saying so beats
                # polling a dead code until the window closes.
                print(f"\npairing failed ({collected.status_code}): "
                      f"{collected.text[:200]}")
                return 1
            await asyncio.sleep(interval)

    print("\nthe pairing request expired. Run this again to get a new code.")
    return 1


def _pairing_window(started: dict[str, Any]) -> float:
    """How long to keep polling, from the server's own expiry."""
    from datetime import datetime

    from vtv.contracts.base import utc_now

    try:
        expires = datetime.fromisoformat(str(started["expires_at"]))
    except Exception:  # noqa: BLE001 - a missing expiry is not worth failing on
        return 600.0
    return max(5.0, (expires - utc_now()).total_seconds())


def _open_browser(url: str) -> None:
    """Best effort, and silent about it either way.

    A failure here is not a failure of pairing — the URL is already on screen —
    so it must not print an error that makes somebody think it is.
    """
    import webbrowser

    with contextlib.suppress(Exception):
        webbrowser.open(url)


def _remember(args: argparse.Namespace, payload: dict[str, Any], name: str) -> int:
    path = state.save(
        state.Pairing(
            server=args.server.rstrip("/"),
            device_id=str(payload["device_id"]),
            token=str(payload["token"]),
            name=str(payload.get("name") or name),
        )
    )
    print(f"\npaired as {payload.get('name') or name}")
    print(f"identity stored at {path}")
    print("Start rendering with:  vtv-desktop run")
    return 0


async def _pair_with_code(
    args: argparse.Namespace, hardware: Any, name: str
) -> int:
    """Redeem a code somebody minted in the web app and carried over here."""
    import httpx

    async with httpx.AsyncClient(
        base_url=args.server.rstrip("/"), timeout=30.0
    ) as client:
        response = await client.post(
            "/v1/devices/pair",
            json={
                "code": args.code,
                "name": name,
                "hardware": hardware.model_dump(mode="json"),
            },
        )
    if response.status_code >= 400:
        # The server refuses every bad pairing identically — expired, wrong,
        # already used all read the same — so the useful thing to say is what to
        # do next rather than to guess which it was.
        print(f"pairing failed ({response.status_code}): {response.text[:200]}")
        print("Codes last a few minutes and work once. Generate another and retry.")
        return 1

    return _remember(args, response.json(), name)


def _default_name() -> str:
    """Something a person will recognise in a list of their computers."""
    import platform

    host = platform.node().split(".")[0]
    return host or f"{platform.system()} computer"


async def _run(args: argparse.Namespace) -> int:
    pairing = state.load()
    workspace = _workspace(args.workspace)
    client = DeviceClient(server=pairing.server, token=pairing.token)
    agent = Agent(
        client=client,
        workspace=workspace,
        device_id=pairing.device_id,
        cache=AssetCache(workspace / "assets", budget_bytes=args.cache_bytes),
        workers=args.workers,
        idle_seconds=args.idle_seconds,
    )

    # A closed laptop is handled by the lease expiring. A Ctrl-C is not, and
    # should not be: the job in hand may be minutes from finishing and throwing
    # it away would waste all of it. So the signal asks the loop to stop after
    # the current job, and a second one is the operating system's business.
    graceful = _catch_interrupts(agent.stop)

    print(f"{pairing.name or pairing.device_id} is available for rendering")
    print(f"server    : {pairing.server}")
    # Where part-finished jobs and the asset cache live. Printed because it is
    # the first thing anybody goes looking for — checkpoints after a crash, disk
    # usage, "why is my cache empty" — and because two terminals with different
    # `VTV_DESKTOP_HOME` values silently use two different directories, which
    # looks exactly like a bug and is not one.
    print(f"workspace : {workspace}")
    print(describe(detect()))
    print(
        "\nwaiting for work "
        + (
            "(Ctrl-C to stop after the current job)"
            if graceful
            # Said plainly rather than printed anyway. The first version
            # promised a graceful stop on every platform and could not deliver
            # one here, which is worse than offering nothing: somebody mid-way
            # through a forty-minute render presses Ctrl-C expecting it to
            # finish, and it does not.
            else "(Ctrl-C stops immediately; finished segments are kept)"
        )
        + "\n",
        flush=True,
    )

    try:
        await agent.run(max_jobs=args.max_jobs)
    finally:
        # In the `finally`, so the tally survives a Ctrl-C. It was after the
        # `try` and therefore never printed on the one exit route people
        # actually use, which is how a session that had rendered something
        # reported nothing at all.
        await client.aclose()
        print(
            f"\nstopped. {agent.completed} completed, {agent.failed} failed.",
            flush=True,
        )
    return 0


def _catch_interrupts(stop: Any) -> bool:
    """Ask for a graceful stop on Ctrl-C. True if this platform allows one.

    Two mechanisms because one is not enough. `loop.add_signal_handler` is the
    right answer and raises `NotImplementedError` on Windows, where the previous
    version silently gave up and left the screen promising a graceful stop that
    could not happen.

    `signal.signal` works on both, and its handler runs on the main thread —
    which is the loop's thread — so setting the event from it is safe. The loop
    notices within one poll interval because `Agent._sleep` waits on that event
    rather than sleeping blindly.
    """
    installed = False
    for name in ("SIGINT", "SIGTERM"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        try:
            asyncio.get_running_loop().add_signal_handler(received, stop)
            installed = True
            continue
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            signal.signal(received, lambda *_: stop())
            installed = True
        except (OSError, ValueError):
            # A non-main thread, or a signal this platform will not let us
            # take. Nothing to do but be honest about it on screen.
            continue
    return installed


async def _status(args: argparse.Namespace) -> int:
    del args
    pairing = state.load()
    workspace = _workspace(None)
    cache = AssetCache(workspace / "assets")
    print(f"device    : {pairing.name or '(unnamed)'}  {pairing.device_id}")
    print(f"server    : {pairing.server}")
    print(f"identity  : {state.token_path()}")
    print(f"cache     : {cache.size_bytes() / 1e6:.1f} MB at {cache.root}")
    print()
    print(describe(detect()))
    return 0


async def _hardware(args: argparse.Namespace) -> int:
    print(describe(detect(probe_gpu=not args.skip_gpu_check)))
    return 0


async def _unpair(args: argparse.Namespace) -> int:
    del args
    removed = state.forget()
    if removed:
        print("this computer will no longer render for that account")
        print("Note: the device is still listed on the account until it is revoked there.")
    else:
        print("this computer was not paired")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vtv-desktop",
        description="Render Voice to Video projects on this computer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser(
        "pair",
        help="link this computer to an account",
        description=(
            "Two ways in. With no --code this opens a browser and waits for "
            "you to approve, which needs nobody to be signed in here first. "
            "With --code it redeems a code somebody minted in the web app, "
            "which is what a machine with no browser needs."
        ),
    )
    pair.add_argument(
        "--code",
        help=(
            "a code minted in the web app. Omit it to sign in through a "
            "browser instead, which is what you want on your own computer."
        ),
    )
    pair.add_argument("--server", required=True, help="e.g. https://app.example.com")
    pair.add_argument("--name", help="what to call this computer (defaults to its hostname)")
    pair.add_argument(
        "--skip-gpu-check",
        action="store_true",
        help="pair without testing the graphics card (it is tested again before every job)",
    )
    pair.set_defaults(run=_pair)

    run = sub.add_parser("run", help="make this computer available for rendering")
    run.add_argument("--workspace", help="where to keep assets and part-finished jobs")
    run.add_argument(
        "--workers",
        type=int,
        help="segments to draw at once (default: sized from this machine)",
    )
    run.add_argument("--max-jobs", type=int, help="stop after this many jobs")
    run.add_argument(
        "--idle-seconds",
        type=float,
        default=IDLE_POLL_SECONDS,
        help=(
            "how often to ask for work when idle (default %(default)s). Raising "
            "it quietens the server's log and delays the start of a render by "
            "the same amount."
        ),
    )
    run.add_argument(
        "--cache-bytes",
        type=int,
        default=AssetCache.__dataclass_fields__["budget_bytes"].default,
        help="ceiling for the downloaded-asset cache",
    )
    run.set_defaults(run=_run)

    status = sub.add_parser("status", help="show this computer's pairing and hardware")
    status.set_defaults(run=_status)

    hardware = sub.add_parser("hardware", help="show what this computer can render with")
    hardware.add_argument("--skip-gpu-check", action="store_true")
    hardware.set_defaults(run=_hardware)

    unpair = sub.add_parser("unpair", help="stop this computer rendering for the account")
    unpair.set_defaults(run=_unpair)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Before anything prints. This program's own banner contains an em-dash and
    # its hardware summary can contain anything a graphics driver chose to call
    # itself, and on Windows a redirected stream is cp1252 — where a character
    # it cannot spell raises out of `print` and takes the command with it. The
    # worker lost every job to exactly this.
    tolerant_streams()
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.run(args))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130
    except VTVError as exc:
        # The project's own errors carry sentences meant for people. Printing
        # the message rather than a traceback is the difference between a
        # customer fixing this themselves and a support ticket.
        print(f"{exc}", file=sys.stderr)
        return 1


def cli() -> None:  # pragma: no cover - process entrypoint
    """The `vtv-desktop` console script, from `[project.scripts]`."""
    raise SystemExit(main())


# Without this, `python -m vtv.desktop.cli hardware` imports this module,
# defines every function in it, reaches the end and exits — silently, with
# status 0, having done nothing at all. No error, no usage, no output.
#
# The console script hides it: `vtv-desktop` calls `cli()` directly and works
# perfectly, so anybody who has pip-installed the package never sees this.
# Anybody running from a source checkout with PYTHONPATH set — which is what a
# verification procedure asks for — sees a prompt come straight back and has no
# idea why.
#
# It was in front of me in my own verification run and I routed around it with
# `python -c "from vtv.desktop.cli import main; ..."` instead of asking why the
# obvious invocation printed nothing. A silent success is the one failure mode
# that never announces itself.
if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "cli", "main"]
