"""Read one transactionally consistent snapshot from an independent process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from shared_mind import Kernel
from shared_mind.canonical import canonical_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    arguments = parser.parse_args()
    registry = json.loads(arguments.registry.read_text(encoding="utf-8"))
    kernel = Kernel(arguments.database, registry)
    try:
        kernel.connection.execute("BEGIN")
        payload = {
            "accepted_receipts": kernel.connection.execute(
                "SELECT COUNT(*) FROM receipts WHERE ledger_seq IS NOT NULL"
            ).fetchone()[0],
            "ledger": kernel.connection.execute(
                "SELECT COUNT(*) FROM ledger"
            ).fetchone()[0],
            "pid": os.getpid(),
            "receipts": kernel.connection.execute(
                "SELECT COUNT(*) FROM receipts"
            ).fetchone()[0],
            "sources": kernel.connection.execute(
                "SELECT COUNT(*) FROM sources"
            ).fetchone()[0],
            "state_root": kernel.state_root(),
        }
        kernel.connection.execute("COMMIT")
        print(canonical_json(payload), flush=True)
        return 0
    finally:
        kernel.close()


if __name__ == "__main__":
    raise SystemExit(main())
