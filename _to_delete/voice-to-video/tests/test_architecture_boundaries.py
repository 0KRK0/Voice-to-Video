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

#: Names that must never appear in an import anywhere under ``src/vtv``. These
#: are SDKs, HTTP clients and infrastructure drivers: everything that belongs in
#: an adapter, behind a port.
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

    def test_no_forbidden_imports_anywhere_in_the_package(self) -> None:
        offenders: list[str] = []
        for path in python_files(PACKAGE):
            forbidden = imported_roots(path) & FORBIDDEN_IMPORTS
            if forbidden:
                offenders.append(f"{path.relative_to(SRC)} imports {sorted(forbidden)}")
        self.assertEqual(
            offenders,
            [],
            "vendor SDKs and infrastructure drivers belong in adapters, behind a "
            "port — never in vtv.contracts, vtv.ports or vtv.examples:\n"
            + "\n".join(offenders),
        )

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
