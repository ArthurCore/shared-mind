from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shared_mind.product import ProductService
from shared_mind.workspace import Workspace


DIRECTIVES = """\
FACT: system:atlas | deployment.database_engine@1 | software:postgresql | production
DECISION: Keep PostgreSQL | Continue with PostgreSQL | Verified runbook | Migrate later
QUESTION: Which maintenance window? | Not yet approved
WORK: P0 | Validate migration runbook
SKILL: Review migration | migration, review | read sources; check conflicts | NON_EMPTY
"""


class ProductTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="shared-mind-product-test-")
        self.base = Path(self.temporary.name)
        self.workspace_root = self.base / "workspace"
        self.workspace = Workspace.initialize(
            self.workspace_root, purpose="Preserve and continue the Atlas project."
        )
        self.service = ProductService(self.workspace)

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def write_source(self, name: str = "notes.md", content: str = DIRECTIVES) -> Path:
        path = self.workspace_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def seed_product(self) -> tuple[dict, dict, dict]:
        source = self.write_source()
        batch = self.service.ingest([source])
        extraction = self.service.extract(batch["batch_id"])
        commit = self.service.commit_batch_drafts(batch["batch_id"])
        self.service.build_memory_views()
        self.service.build_indexes()
        return batch, extraction, commit
