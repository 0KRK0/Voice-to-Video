"""The exported JSON Schemas must match the code.

This test is what makes ``schemas/`` trustworthy. The frontend generates its
types from those files and language models are handed them as output
constraints; if they can drift from the pydantic models, both of those consumers
are quietly working from a contract that no longer exists.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from vtv.contracts import CONTRACTS_VERSION, ROOT_DOCUMENTS
from vtv.schema_export import check_all, render_all, repository_root

SCHEMA_DIR = repository_root() / "schemas"


class ExportedSchemasAreCurrent(unittest.TestCase):
    def test_no_drift_between_code_and_committed_schemas(self) -> None:
        problems = check_all(SCHEMA_DIR)
        self.assertEqual(
            problems,
            [],
            "committed schemas are stale — run 'make schemas' and commit the "
            "result:\n" + "\n".join(problems),
        )

    def test_every_root_document_is_exported(self) -> None:
        rendered = render_all()
        for model in ROOT_DOCUMENTS:
            self.assertIn(f"{model.document_name}.json", rendered)

    def test_root_documents_have_distinct_names(self) -> None:
        names = [model.document_name for model in ROOT_DOCUMENTS]
        self.assertEqual(len(names), len(set(names)))

    def test_the_index_lists_every_document(self) -> None:
        index = json.loads((SCHEMA_DIR / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["contracts_version"], CONTRACTS_VERSION)
        self.assertEqual(
            set(index["documents"]),
            {model.document_name for model in ROOT_DOCUMENTS},
        )

    def test_schemas_are_valid_json_with_identity_and_version(self) -> None:
        for path in sorted(Path(SCHEMA_DIR).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if path.name == "index.json":
                continue
            self.assertIn("$schema", payload, path.name)
            self.assertIn("$id", payload, path.name)
            self.assertEqual(payload["x-contracts-version"], CONTRACTS_VERSION)

    def test_discriminated_unions_survive_export(self) -> None:
        # If the discriminator is lost in export, a language model handed the
        # schema has no way to know which variant to produce.
        payload = json.loads(
            (SCHEMA_DIR / "visual_plan.json").read_text(encoding="utf-8")
        )
        text = json.dumps(payload)
        self.assertIn("discriminator", text)
        self.assertIn("propertyName", text)


if __name__ == "__main__":
    unittest.main()
