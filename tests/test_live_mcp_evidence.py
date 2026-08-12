from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_PATH = (
    ROOT
    / "evals"
    / "product_continuity"
    / "results"
    / "mcp-interoperability-live-2026-08-12.json"
)


class LiveMcpInteroperabilityEvidenceTest(unittest.TestCase):
    def test_checked_in_mcp_interoperability_evidence_is_sanitized(self) -> None:
        artifact = self._load_json(ARTIFACT_PATH)

        self.assertEqual(
            {
                "artifact_version",
                "date",
                "shared_workspace_digest",
                "workspace_basis",
                "clients",
                "calls",
                "outcomes",
                "final_state",
                "verification",
                "replay",
            },
            set(artifact),
        )
        self.assertEqual("shared-mind-live-mcp-interoperability@1", artifact["artifact_version"])
        self.assertEqual("2026-08-12", artifact["date"])
        self.assert_no_forbidden_keys(artifact)
        self.assertEqual(
            self._digest(artifact["workspace_basis"]),
            artifact["shared_workspace_digest"],
        )

        self.assertEqual(
            {
                "codex": {
                    "provider": "OpenAI/Codex",
                    "model_snapshot": "gpt-5.5 service snapshot 2026-08-12",
                    "client": {"name": "codex-cli", "version": "0.147.0"},
                },
                "claude": {
                    "provider": "Anthropic/Claude",
                    "model_snapshot": "claude-sonnet-4-5 service snapshot 2026-08-12",
                    "client": {"name": "Claude Code", "version": "2.1.227"},
                },
            },
            artifact["clients"],
        )
        self.assertEqual(
            {
                "codex": {"proposal_commit": 1, "ledger_verify": 1},
                "claude": {"proposal_commit": 1, "ledger_verify": 1},
            },
            artifact["calls"],
        )
        self.assertEqual(
            [
                {
                    "sequence": 1,
                    "outcome": "COMMITTED",
                    "actor": "agent:codex-live",
                    "work_item_id": "workitem_codex_live_acceptance_001",
                    "work_item_status": "TODO",
                },
                {
                    "sequence": 2,
                    "outcome": "COMMITTED",
                    "actor": "agent:claude-live",
                    "work_item_id": "workitem_claude_live_acceptance_001",
                    "work_item_status": "TODO",
                },
            ],
            artifact["outcomes"],
        )
        self.assertEqual(
            {
                "ledger_count": 2,
                "receipt_count": 2,
                "work_item_count": 2,
                "head": "sha256:28073576df607aaa4fde3f9622f33ef6ea2c30c65ae0fdbcc135f87bfbe287f1",
                "state_root": "sha256:9dce4589015fcfa32c88fb1e47394df52dc71d41bcf7657e9d6c13dd274b4550",
                "silent_overwrite_count": 0,
            },
            artifact["final_state"],
        )
        self.assertEqual(
            {"valid": True, "checked_entries": 2, "errors": []},
            artifact["verification"],
        )
        self.assertEqual(
            {
                "source_replay_valid": True,
                "checked_entries": 2,
                "ledger_count": 2,
                "receipt_count": 2,
                "work_item_count": 2,
                "head": "sha256:28073576df607aaa4fde3f9622f33ef6ea2c30c65ae0fdbcc135f87bfbe287f1",
                "state_root": "sha256:9dce4589015fcfa32c88fb1e47394df52dc71d41bcf7657e9d6c13dd274b4550",
                "replay_parity": True,
            },
            artifact["replay"],
        )

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise AssertionError(f"expected JSON object: {path}")
        return value

    @staticmethod
    def _digest(value: Mapping[str, Any]) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def assert_no_forbidden_keys(self, value: Any) -> None:
        forbidden_exact = {
            "account_id",
            "api_key",
            "authorization",
            "credential",
            "credentials",
            "password",
            "path",
            "raw_prompt",
            "raw_response",
            "request_id",
        }
        forbidden_fragments = ("secret",)
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                self.assertNotIn(normalized, forbidden_exact)
                for fragment in forbidden_fragments:
                    self.assertNotIn(fragment, normalized)
                self.assert_no_forbidden_keys(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_forbidden_keys(item)


if __name__ == "__main__":
    unittest.main()
