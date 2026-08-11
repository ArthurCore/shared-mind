"""Deterministic, replay-valid fixtures for the DEV-021 context benchmark."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared_mind import Kernel
from shared_mind.canonical import sha256_json


FIXTURE_VERSION = "dev-021-fixture@1"
SUPPORTED_PROFILES = frozenset({"history-heavy", "hot-active"})
ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "contracts" / "atlas-predicate-registry.v1.json"
FIXTURES_PATH = ROOT / "contracts" / "atlas-conformance-fixtures.v1.json"


def build_benchmark_fixture(
    database: str | Path,
    *,
    ledger_entries: int,
    profile: str = "history-heavy",
    seed: int = 21,
) -> dict[str, Any]:
    """Create a deterministic fixture through the public ``Kernel.commit`` API.

    Both profiles keep materialized state deliberately small so a benchmark can
    isolate ledger-history scaling.  ``history-heavy`` finishes its hot work
    item; ``hot-active`` leaves the same item actionable so context-history
    truncation is exercised.
    """

    if isinstance(ledger_entries, bool) or ledger_entries < 1:
        raise ValueError("ledger_entries must be a positive integer")
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"unsupported fixture profile: {profile}")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")
    path = Path(database)
    if path.exists():
        raise FileExistsError(f"benchmark database already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    templates = _proposal_templates()
    work_item_id = f"workitem_dev021_hot_{seed:08d}"
    kernel = Kernel(path, registry)
    try:
        create = _create_work_item_proposal(
            templates["create_work_item_proposal"], work_item_id, seed
        )
        _commit_expected(kernel, create)
        current_status = "TODO"
        for sequence in range(2, ledger_entries + 1):
            final_history_entry = (
                profile == "history-heavy" and sequence == ledger_entries
            )
            new_status = (
                "DONE"
                if final_history_entry
                else ("DOING" if current_status == "TODO" else "TODO")
            )
            proposal = _update_work_item_proposal(
                templates["update_work_item_status_proposal"],
                work_item_id=work_item_id,
                seed=seed,
                sequence=sequence,
                current_status=current_status,
                new_status=new_status,
            )
            _commit_expected(kernel, proposal)
            current_status = new_status

        head = kernel.connection.execute(
            "SELECT seq, entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        tables = (
            "sources",
            "claims",
            "evidence",
            "conflicts",
            "decision_records",
            "open_questions",
            "work_items",
            "ledger",
            "receipts",
        )
        manifest: dict[str, Any] = {
            "fixture_version": FIXTURE_VERSION,
            "profile": profile,
            "seed": seed,
            "generation_api": "Kernel.commit",
            "schema_version": Kernel.SUPPORTED_VERSIONS["schema"],
            "projection_version": Kernel.SUPPORTED_VERSIONS["projection"],
            "ledger_entries": int(head["seq"]) if head is not None else 0,
            "head_entry_hash": head["entry_hash"] if head is not None else None,
            "state_root": kernel.state_root(),
            "table_counts": {
                table: int(
                    kernel.connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                )
                for table in tables
            },
            "hot_object": {
                "object_type": "WORK_ITEM",
                "object_id": work_item_id,
                "history_entries": ledger_entries,
                "status": current_status,
            },
        }
        manifest["manifest_hash"] = sha256_json(manifest)
        return manifest
    finally:
        kernel.close()


def _proposal_templates() -> dict[str, dict[str, Any]]:
    fixture_set = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    return {
        item["name"]: item["object"]
        for item in fixture_set["typed_objects"]
        if item["name"]
        in {"create_work_item_proposal", "update_work_item_status_proposal"}
    }


def _create_work_item_proposal(
    template: dict[str, Any], work_item_id: str, seed: int
) -> dict[str, Any]:
    proposal = copy.deepcopy(template)
    timestamp = _timestamp(seed, 1)
    proposal.update(
        {
            "proposal_id": f"proposal_dev021_create_{seed:08d}",
            "idempotency_key": f"dev021-create-{seed:08d}",
            "proposed_at": timestamp,
            "base_state_root": None,
        }
    )
    operation = proposal["operations"][0]
    operation["op_id"] = f"operation_dev021_create_{seed:08d}"
    work_item = operation["work_item"]
    work_item.update(
        {
            "work_item_id": work_item_id,
            "description": f"Exercise deterministic DEV-021 history seed {seed}.",
            "related_objects": [],
            "status": "TODO",
            "version": 1,
            "blocker": None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )
    return proposal


def _update_work_item_proposal(
    template: dict[str, Any],
    *,
    work_item_id: str,
    seed: int,
    sequence: int,
    current_status: str,
    new_status: str,
) -> dict[str, Any]:
    proposal = copy.deepcopy(template)
    timestamp = _timestamp(seed, sequence)
    proposal.update(
        {
            "proposal_id": f"proposal_dev021_update_{seed:08d}_{sequence:08d}",
            "idempotency_key": f"dev021-update-{seed:08d}-{sequence:08d}",
            "proposed_at": timestamp,
            "base_state_root": None,
        }
    )
    expected_version = sequence - 1
    proposal["reads"][0].update(
        {"aggregate_id": work_item_id, "expected_version": expected_version}
    )
    proposal["guards"][0].update(
        {"work_item_id": work_item_id, "expected_status": current_status}
    )
    proposal["guards"][1].update(
        {"work_item_id": work_item_id, "expected_version": expected_version}
    )
    operation = proposal["operations"][0]
    operation.update(
        {
            "op_id": f"operation_dev021_update_{seed:08d}_{sequence:08d}",
            "target_work_item_id": work_item_id,
            "new_status": new_status,
            "blocker": None,
            "rationale": "Exercise deterministic ledger-history projection.",
            "updated_at": timestamp,
        }
    )
    return proposal


def _timestamp(seed: int, sequence: int) -> str:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=seed)
    return (start + timedelta(seconds=sequence)).isoformat().replace("+00:00", "Z")


def _commit_expected(kernel: Kernel, proposal: dict[str, Any]) -> None:
    receipt = kernel.commit(proposal)
    if receipt.outcome != "COMMITTED":
        raise RuntimeError(
            "benchmark fixture proposal was rejected: "
            f"{proposal['proposal_id']} {receipt.outcome} {receipt.reason_codes}"
        )


__all__ = ["FIXTURE_VERSION", "build_benchmark_fixture"]

