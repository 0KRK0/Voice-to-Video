"""Read `var/provider-calls.jsonl` and say where the money went.

    python -m vtv.trace_report                 # everything in the file
    python -m vtv.trace_report --job JOB_ID    # one job
    python -m vtv.trace_report --calls         # every call, in order

The file is written by `observability/trace.py` when
`VTV_TRACE_PROVIDER_CALLS=true`. This module only reads it, so it is safe to
run against a file someone sent you.

The report answers the question "why did a 76-second video cost two dollars"
in the order the answer usually arrives: how many calls, of what kind, to whom,
at what unit price — then which visuals were expensive, then which calls failed
and were paid for anyway.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(
            f"no trace at {path}\n"
            "Set VTV_TRACE_PROVIDER_CALLS=true and run the worker, then try again."
        )
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            # A half-written last line is normal if the worker is still running.
            continue
    return rows


def money(value: float) -> str:
    return f"${value:,.4f}"


def _table(headings: list[str], rows: list[list[str]], aligns: str) -> str:
    if not rows:
        return "  (nothing)\n"
    widths = [
        max(len(headings[i]), max(len(r[i]) for r in rows))
        for i in range(len(headings))
    ]

    def render(cells: list[str]) -> str:
        out = []
        for index, cell in enumerate(cells):
            out.append(
                cell.rjust(widths[index])
                if aligns[index] == "r"
                else cell.ljust(widths[index])
            )
        return "  " + "  ".join(out).rstrip()

    lines = [render(headings), "  " + "  ".join("─" * w for w in widths)]
    lines.extend(render(r) for r in rows)
    return "\n".join(lines) + "\n"


def summarise(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "The trace is empty.\n"

    out: list[str] = []
    total = sum(float(r.get("cost_usd") or 0) for r in rows)
    paid = [r for r in rows if float(r.get("cost_usd") or 0) > 0]
    cached = [r for r in rows if r.get("from_cache")]
    failed = [r for r in rows if r.get("outcome") in {"failed", "refused", "blocked"}]
    retries = [r for r in rows if int(r.get("attempt") or 1) > 1]

    out.append("")
    out.append(f"  {len(rows)} provider call(s), {money(total)} total")
    out.append(
        f"  {len(paid)} charged · {len(cached)} served from cache · "
        f"{len(failed)} failed · {len(retries)} were retries"
    )
    out.append("")

    # -- by kind and provider -------------------------------------------
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get("kind")),
            str(row.get("provider")),
            str(row.get("model") or "—"),
        )
        groups[key].append(row)

    table_rows = []
    for (kind, provider, model), items in sorted(
        groups.items(), key=lambda kv: -sum(float(r.get("cost_usd") or 0) for r in kv[1])
    ):
        spend = sum(float(r.get("cost_usd") or 0) for r in items)
        charged = [r for r in items if float(r.get("cost_usd") or 0) > 0]
        each = spend / len(charged) if charged else 0.0
        table_rows.append([
            kind,
            provider,
            model,
            str(len(items)),
            money(each) if charged else "free",
            money(spend),
            f"{100 * spend / total:.0f}%" if total else "—",
        ])
    out.append("  WHERE THE MONEY WENT")
    out.append(
        _table(
            ["kind", "provider", "model", "calls", "each", "total", "share"],
            table_rows,
            "lllrrrr",
        )
    )

    # -- by visual -------------------------------------------------------
    per_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("unit"):
            per_unit[str(row["unit"])].append(row)
    if per_unit:
        unit_rows = []
        for unit, items in sorted(
            per_unit.items(),
            key=lambda kv: -sum(float(r.get("cost_usd") or 0) for r in kv[1]),
        )[:15]:
            spend = sum(float(r.get("cost_usd") or 0) for r in items)
            kinds = ", ".join(sorted({str(r.get("kind")) for r in items}))
            unit_rows.append([unit, str(len(items)), kinds, money(spend)])
        out.append("  PER VISUAL — most expensive first")
        out.append(_table(["visual", "calls", "kinds", "cost"], unit_rows, "lrlr"))

    # -- by job ----------------------------------------------------------
    per_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        per_job[str(row.get("job") or "—")].append(row)
    if len(per_job) > 1:
        job_rows = []
        for job, items in sorted(
            per_job.items(),
            key=lambda kv: -sum(float(r.get("cost_usd") or 0) for r in kv[1]),
        ):
            spend = sum(float(r.get("cost_usd") or 0) for r in items)
            kind = str(items[0].get("job_kind") or "—")
            job_rows.append([job, kind, str(len(items)), money(spend)])
        out.append("  PER JOB")
        out.append(_table(["job", "kind", "calls", "cost"], job_rows, "llrr"))

    # -- what failed, and whether we paid --------------------------------
    if failed:
        fail_rows = []
        for row in failed[:20]:
            fail_rows.append([
                str(row.get("kind")),
                str(row.get("provider")),
                f"try {row.get('attempt')}",
                str(row.get("error") or row.get("response", {}).get("code") or "")[:64],
            ])
        out.append("  FAILURES — the vendor's own words")
        out.append(_table(["kind", "provider", "attempt", "message"], fail_rows, "llll"))

    # -- the retry tax ---------------------------------------------------
    retry_spend = sum(float(r.get("cost_usd") or 0) for r in retries)
    if retry_spend > 0:
        out.append(
            f"  {money(retry_spend)} of the total was spent on retries. A job "
            "that fails\n  after paying is charged again on every attempt."
        )
        out.append("")

    return "\n".join(out)


def calls(rows: list[dict[str, Any]]) -> str:
    out = ["", "  EVERY CALL, IN ORDER", ""]
    for row in rows:
        mark = {"ok": "·", "cached": "=", "failed": "×", "refused": "!", "blocked": "⊘"}.get(
            str(row.get("outcome")), "?"
        )
        cost = float(row.get("cost_usd") or 0)
        summary = (row.get("request") or {}).get("summary") or row.get("error") or ""
        out.append(
            f"  {mark} {row.get('kind')!s:<14} "
            f"{row.get('provider')!s:<16} "
            f"{(money(cost) if cost else 'free'):>9} "
            f"{row.get('latency_ms') or 0!s:>6}ms  "
            f"{row.get('unit') or row.get('stage') or ''!s:<26} "
            f"{str(summary)[:80]}"
        )
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vtv.trace_report")
    parser.add_argument(
        "path",
        nargs="?",
        default="var/provider-calls.jsonl",
        help="the trace file (default: var/provider-calls.jsonl)",
    )
    parser.add_argument("--job", help="only calls made by this job id")
    parser.add_argument("--project", help="only calls for this project id")
    parser.add_argument(
        "--calls", action="store_true", help="list every call rather than summarising"
    )
    args = parser.parse_args(argv)

    rows = load(Path(args.path))
    if args.job:
        rows = [r for r in rows if str(r.get("job")) == args.job]
    if args.project:
        rows = [r for r in rows if str(r.get("project_id")) == args.project]

    sys.stdout.write(calls(rows) if args.calls else summarise(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
