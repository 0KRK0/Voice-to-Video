"""Export every root contract as JSON Schema.

The exported files in ``schemas/`` do three jobs at once, which is why they are
committed to the repository rather than generated on demand:

1. **They are the cross-language contract.** The TypeScript frontend and any
   future non-Python service generate their types from these files, so the
   browser and the backend cannot drift apart.
2. **They constrain language-model output.** Providers that support structured
   output are handed the schema directly; the response is then validated against
   the same pydantic model that produced it. The model is not trusted to remember
   a shape described in a prompt.
3. **They make contract changes visible in review.** A pull request that alters a
   schema shows the diff. ``tests/test_schema_export.py`` fails if the checked-in
   files no longer match the code, so a change to a contract can never be merged
   silently.

Run ``python -m vtv.schema_export`` to regenerate, or ``make schemas``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from vtv.contracts import CONTRACTS_VERSION, ROOT_DOCUMENTS
from vtv.contracts.base import RootDocument

#: Where the generated files live, relative to the repository root.
SCHEMA_DIR_NAME = "schemas"


def schema_for(model: type[RootDocument]) -> dict[str, Any]:
    """JSON Schema for one root document, with stable metadata attached."""
    schema: dict[str, Any] = model.model_json_schema(mode="serialization")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://schemas.voice-to-video.dev/{model.document_name}.json"
    schema["x-contracts-version"] = CONTRACTS_VERSION
    return schema


def render_all() -> dict[str, str]:
    """Filename to JSON text, for every root document."""
    rendered: dict[str, str] = {}
    for model in ROOT_DOCUMENTS:
        payload = schema_for(model)
        rendered[f"{model.document_name}.json"] = (
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )

    index = {
        "contracts_version": CONTRACTS_VERSION,
        "documents": sorted(model.document_name for model in ROOT_DOCUMENTS),
    }
    rendered["index.json"] = (
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    return rendered


def repository_root() -> Path:
    """The repository root, found from this file's location."""
    return Path(__file__).resolve().parents[2]


def write_all(target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in render_all().items():
        path = target / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def check_all(target: Path) -> list[str]:
    """Return a list of drift descriptions; empty means the export is current."""
    problems: list[str] = []
    expected = render_all()
    for name, text in expected.items():
        path = target / name
        if not path.exists():
            problems.append(f"{name} is missing")
        elif path.read_text(encoding="utf-8") != text:
            problems.append(f"{name} is out of date")
    for path in sorted(target.glob("*.json")):
        if path.name not in expected:
            problems.append(f"{path.name} is no longer produced and should be removed")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed schemas match the code instead of rewriting them.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repository_root() / SCHEMA_DIR_NAME,
        help="Output directory.",
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = check_all(args.out)
        for problem in problems:
            print(f"schema drift: {problem}", file=sys.stderr)
        if problems:
            print("run 'make schemas' to regenerate", file=sys.stderr)
            return 1
        print(f"schemas are current ({len(ROOT_DOCUMENTS)} documents)")
        return 0

    written = write_all(args.out)
    print(f"wrote {len(written)} schema files to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
