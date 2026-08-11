"""Bounded subprocess driver for process-crash durability tests.

This helper is intentionally outside the production package.  It invokes the
public ``Kernel.commit`` entrypoint in a separate OS process and exposes file
barriers at the two process-failure boundaries the acceptance suite needs.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from shared_mind import Kernel
from shared_mind.canonical import canonical_json


def _barrier(ready: Path, release: Path, payload: dict[str, Any]) -> None:
    ready.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    deadline = time.monotonic() + 8
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"durability barrier timed out: {ready.name}")
        time.sleep(0.005)


class _BarrierAfterOperationKernel(Kernel):
    """Pause with uncommitted materialized state present in the WAL."""

    def __init__(
        self,
        database: Path,
        registry: dict[str, Any],
        *,
        ready: Path,
        release: Path,
    ) -> None:
        self._durability_ready = ready
        self._durability_release = release
        self._durability_paused = False
        super().__init__(database, registry)

    def _apply_operation(
        self,
        operation: dict[str, Any],
        events: list[dict[str, Any]],
        conflict_ids: list[str],
    ) -> None:
        super()._apply_operation(operation, events, conflict_ids)
        if not self._durability_paused:
            self._durability_paused = True
            _barrier(
                self._durability_ready,
                self._durability_release,
                {"pid": os.getpid(), "stage": "after_operation_before_commit"},
            )


def _receipt_payload(receipt: Any) -> dict[str, Any]:
    return {
        "conflict_ids": list(receipt.conflict_ids),
        "document": receipt.document,
        "ledger_seq": receipt.ledger_seq,
        "outcome": receipt.outcome,
        "pid": os.getpid(),
        "proposal_id": receipt.proposal_id,
        "reason_codes": list(receipt.reason_codes),
        "state_root": receipt.state_root,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument(
        "--barrier-stage",
        choices=("before_commit", "after_operation", "after_commit", "none"),
        required=True,
    )
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--release", type=Path, required=True)
    arguments = parser.parse_args()

    registry = json.loads(arguments.registry.read_text(encoding="utf-8"))
    proposal = json.loads(arguments.proposal.read_text(encoding="utf-8"))
    kernel_type: type[Kernel] = (
        _BarrierAfterOperationKernel
        if arguments.barrier_stage == "after_operation"
        else Kernel
    )
    if kernel_type is _BarrierAfterOperationKernel:
        kernel = kernel_type(
            arguments.database,
            registry,
            ready=arguments.ready,
            release=arguments.release,
        )
    else:
        kernel = kernel_type(arguments.database, registry)
    try:
        if arguments.barrier_stage == "before_commit":
            _barrier(
                arguments.ready,
                arguments.release,
                {"pid": os.getpid(), "stage": "before_commit"},
            )
        receipt = kernel.commit(proposal)
        payload = _receipt_payload(receipt)
        if arguments.barrier_stage == "after_commit":
            payload["stage"] = "after_commit"
            _barrier(arguments.ready, arguments.release, payload)
        print(canonical_json(payload), flush=True)
        return 0
    finally:
        kernel.close()


if __name__ == "__main__":
    raise SystemExit(main())
