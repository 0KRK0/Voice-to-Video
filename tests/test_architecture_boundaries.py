"""Architecture rules, enforced by the build rather than by memory.

Every principle in this file is one that erodes quietly. Nobody deliberately
imports a vendor SDK into the domain model; it happens because one function
needed one thing at one deadline, and eighteen months later the Visual Director
cannot be tested without an API key.

So the rules are executable. If someone violates one, this test fails before the
pull request is reviewed.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = SRC / "vtv"

#: Third-party packages the core is permitted to depend on. The list is short by
#: design and adding to it should require an argument, not a commit.
ALLOWED_THIRD_PARTY = {"pydantic", "typing_extensions", "annotated_types"}

#: Packages that make up the core. Vendor code may never appear in any of them.
#: `adapters` and `api` are deliberately absent: they are where vendor code is
#: *supposed* to live, and the whole point of the ports design is to confine it
#: to exactly those two directories.
CORE_PACKAGES = ("contracts", "ports", "pipeline", "animation", "examples", "evaluation")

#: Names that must never appear in an import inside a core package. These are
#: SDKs, HTTP clients and infrastructure drivers: everything that belongs in an
#: adapter, behind a port.
FORBIDDEN_IMPORTS = {
    "anthropic",
    "openai",
    "google",
    "replicate",
    "elevenlabs",
    "stability_sdk",
    "boto3",
    "botocore",
    "azure",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "redis",
    "celery",
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "asyncpg",
    "fastapi",
    "starlette",
    "flask",
    "django",
}


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py"))


def imported_roots(path: Path) -> set[str]:
    """Top-level module names imported by a file, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import; stays within the package
                continue
            if node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


def imported_modules(path: Path) -> set[str]:
    """Fully-qualified module names imported by a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
    return modules


class NoVendorCodeInTheCore(unittest.TestCase):
    """Rule 3: providers must not leak into business logic."""

    def test_no_forbidden_imports_in_any_core_package(self) -> None:
        offenders: list[str] = []
        for package in CORE_PACKAGES:
            for path in python_files(PACKAGE / package):
                forbidden = imported_roots(path) & FORBIDDEN_IMPORTS
                if forbidden:
                    offenders.append(
                        f"{path.relative_to(SRC)} imports {sorted(forbidden)}"
                    )
        self.assertEqual(
            offenders,
            [],
            "vendor SDKs and infrastructure drivers belong in vtv.adapters or "
            "vtv.api, behind a port — never in the core:\n" + "\n".join(offenders),
        )

    def test_adapters_do_not_import_each_other(self) -> None:
        """Adapters depend on ports, on their vendor, and on shared tooling.

        Never on a sibling *family* — that would make swapping one provider out
        require touching another. Two modules are exceptions, and both are
        shared infrastructure rather than providers:

        * ``adapters.media`` — the ffmpeg wrapper. Several families legitimately
          build on it.
        * ``adapters.endpoints`` — one rule for whether a configured provider
          endpoint is a base URL or a full one. It existed as two rules before,
          one per family, and nothing said which was which: the speech adapters
          wanted the full URL and the text and image adapters wanted the base.
          An operator who set all five to the full URL got working speech and
          404s everywhere else. Duplicating the resolver per family to satisfy
          this rule would recreate exactly the divergence it was written to end.

        Both are pure, vendor-free and stateless. That is the test for whether
        something belongs here: a module that names a vendor, holds state, or
        implements a port is a family, not shared tooling, and adding it to this
        set would hollow out the rule.
        """
        shared = {"media", "endpoints"}
        offenders: list[str] = []
        for path in python_files(PACKAGE / "adapters"):
            parts = path.relative_to(PACKAGE / "adapters").parts
            family = parts[0] if len(parts) > 1 else ""
            for module in imported_modules(path):
                if not module.startswith("vtv.adapters."):
                    continue
                other = module.split(".")[2]
                if other != family and other not in shared:
                    offenders.append(f"{path.relative_to(SRC)} imports {module}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_contracts_and_ports_use_only_stdlib_and_pydantic(self) -> None:
        allowed = set(sys.stdlib_module_names) | ALLOWED_THIRD_PARTY | {"vtv"}
        offenders: list[str] = []
        for directory in (PACKAGE / "contracts", PACKAGE / "ports"):
            for path in python_files(directory):
                extra = imported_roots(path) - allowed
                if extra:
                    offenders.append(
                        f"{path.relative_to(SRC)} imports {sorted(extra)}"
                    )
        self.assertEqual(offenders, [], "\n".join(offenders))


class LayeringIsAcyclic(unittest.TestCase):
    """Contracts sit below ports. Nothing below reaches upward."""

    def test_contracts_do_not_import_ports(self) -> None:
        offenders = [
            str(path.relative_to(SRC))
            for path in python_files(PACKAGE / "contracts")
            if any(module.startswith("vtv.ports") for module in imported_modules(path))
        ]
        self.assertEqual(
            offenders,
            [],
            "vtv.contracts is the lowest layer; it must not know that ports exist",
        )

    def test_ports_import_contracts_but_define_no_models_of_the_pipeline(self) -> None:
        # Ports may exchange contract types and may define small value objects of
        # their own (a search candidate, a job handle), but the pipeline's
        # documents live in one place only.
        forbidden_names = {"Scene", "Timeline", "Project", "Transcript"}
        offenders: list[str] = []
        for path in python_files(PACKAGE / "ports"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in forbidden_names:
                    offenders.append(f"{path.relative_to(SRC)} redefines {node.name}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class ExamplesStayPure(unittest.TestCase):
    """The worked examples must run with no I/O and no providers."""

    def test_examples_only_depend_on_contracts(self) -> None:
        offenders: list[str] = []
        for path in python_files(PACKAGE / "examples"):
            for module in imported_modules(path):
                if (
                    module.startswith("vtv.")
                    and not module.startswith("vtv.contracts")
                    and not module.startswith("vtv.examples")
                ):
                    offenders.append(f"{path.relative_to(SRC)} imports {module}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class EveryContractModuleIsExported(unittest.TestCase):
    """A contract that is not re-exported is a contract people will duplicate."""

    def test_contract_modules_are_reachable_from_the_package(self) -> None:
        import vtv.contracts as contracts

        modules = {
            path.stem
            for path in python_files(PACKAGE / "contracts")
            if path.stem != "__init__"
        }
        missing = [
            name
            for name in modules
            if not hasattr(contracts, "__all__") or not any(
                getattr(contracts, symbol, None) is not None
                for symbol in contracts.__all__
                if getattr(
                    getattr(contracts, symbol, None), "__module__", ""
                ).endswith(f"contracts.{name}")
            )
        ]
        self.assertEqual(
            missing,
            [],
            f"these contract modules export nothing through vtv.contracts: {missing}",
        )


if __name__ == "__main__":
    unittest.main()


class TheSuiteNeverReachesTheInternet(unittest.TestCase):
    """No test may build a `Settings` that leaves asset search pointing outwards.

    `Settings.asset_search_endpoint` defaults to `https://api.openverse.org/v1`,
    which is the right default for a deployment and exactly wrong for a test.
    While the licensed-media rung was unreachable this did not matter, because
    nothing ever called it. The moment the rung worked, the product suite began
    making real requests to Openverse and Wikimedia — slow, flaky, dependent on
    somebody else's uptime, and quietly different depending on what the commons
    happened to hold that day.

    Every test that constructs `Settings` must therefore say so explicitly. A
    keyword is a poor guard on its own; a check that reads every construction in
    the suite is the chokepoint, and it fails on the file that forgot rather
    than on a timeout three months later.
    """

    def test_every_settings_construction_in_the_suite_disables_asset_search(self) -> None:
        tests_dir = Path(__file__).resolve().parent
        offenders: list[str] = []
        for path in sorted(tests_dir.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Name) and node.func.id == "Settings"):
                    continue
                if not any(k.arg == "asset_search_endpoint" for k in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "these Settings(...) constructions would point asset search at the "
            "live commons; pass asset_search_endpoint=\"\" (or a local stub URL): "
            + ", ".join(offenders),
        )
