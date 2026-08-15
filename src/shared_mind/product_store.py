"""Local product-layer persistence for Shared Mind.

The kernel database remains the authority for sources, factual claims,
conflicts, decisions, questions, and work items.  This module owns only
review staging, disposable derived views/indexes, shared procedural Skills,
and product telemetry.  Every mutation is recorded in an append-only,
hash-chained product audit log so derived/product state can be inspected and
reconciled without pretending it is kernel truth.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .canonical import canonical_json, sha256_json
from .product_contract import validate_product_object


PRODUCT_STORE_VERSION = 1
PRODUCT_DATABASE_FILENAME = "product.sqlite3"


class ProductStoreError(Exception):
    """Stable product persistence failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class ProductStore:
    """SQLite-backed product state outside the canonical kernel tables."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._fts_enabled = False
        self._create_schema()

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS product_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ingest_batches (
              batch_id TEXT PRIMARY KEY,
              manifest_hash TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED')),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              document TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ingest_items (
              batch_id TEXT NOT NULL,
              item_id TEXT NOT NULL,
              source_path TEXT NOT NULL,
              source_id TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              media_type TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('PENDING','IMPORTED','UNCHANGED','FAILED','SKIPPED')),
              revision_id TEXT,
              error_code TEXT,
              document TEXT NOT NULL,
              PRIMARY KEY(batch_id, item_id),
              FOREIGN KEY(batch_id) REFERENCES ingest_batches(batch_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS ingest_items_fingerprint
              ON ingest_items(source_id, fingerprint, status)
            """,
            """
            CREATE TABLE IF NOT EXISTS drafts (
              draft_id TEXT PRIMARY KEY,
              batch_id TEXT,
              draft_kind TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('DRAFT','REVIEWED','REJECTED','COMMITTED','EXPIRED','FAILED')),
              version INTEGER NOT NULL CHECK(version >= 1),
              dependency_digest TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              expires_at TEXT,
              document TEXT NOT NULL,
              provenance TEXT NOT NULL,
              receipt TEXT,
              UNIQUE(batch_id, dependency_digest, draft_kind)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS drafts_queue
              ON drafts(status, draft_kind, created_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS artifacts (
              artifact_id TEXT PRIMARY KEY,
              artifact_type TEXT NOT NULL,
              scope TEXT NOT NULL,
              title TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('READY','STALE','FAILED','DEPRECATED')),
              version INTEGER NOT NULL CHECK(version >= 1),
              dependency_digest TEXT NOT NULL,
              builder_version TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              document TEXT NOT NULL,
              provenance TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS artifacts_type_status
              ON artifacts(artifact_type, status, scope)
            """,
            """
            CREATE TABLE IF NOT EXISTS skills (
              skill_id TEXT NOT NULL,
              version INTEGER NOT NULL CHECK(version >= 1),
              status TEXT NOT NULL CHECK(status IN ('DRAFT','TESTED','APPROVED','DEPRECATED')),
              content_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              document TEXT NOT NULL,
              provenance TEXT NOT NULL,
              PRIMARY KEY(skill_id, version)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS one_approved_skill_version
              ON skills(skill_id) WHERE status = 'APPROVED'
            """,
            """
            CREATE TABLE IF NOT EXISTS retrieval_documents (
              document_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              metadata TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS retrieval_kind ON retrieval_documents(kind)
            """,
            """
            CREATE TABLE IF NOT EXISTS links (
              source_id TEXT NOT NULL,
              target_id TEXT NOT NULL,
              relation TEXT NOT NULL,
              metadata TEXT NOT NULL,
              PRIMARY KEY(source_id, target_id, relation)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS links_target ON links(target_id, relation)
            """,
            """
            CREATE TABLE IF NOT EXISTS code_symbols (
              symbol_id TEXT PRIMARY KEY,
              source_revision_id TEXT NOT NULL,
              file_path TEXT NOT NULL,
              name TEXT NOT NULL,
              qualified_name TEXT NOT NULL,
              symbol_kind TEXT NOT NULL,
              start_line INTEGER NOT NULL,
              end_line INTEGER NOT NULL,
              signature TEXT,
              document TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS code_symbol_name ON code_symbols(name, qualified_name)
            """,
            """
            CREATE TABLE IF NOT EXISTS code_edges (
              source_symbol_id TEXT NOT NULL,
              target_symbol_id TEXT NOT NULL,
              edge_kind TEXT NOT NULL,
              metadata TEXT NOT NULL,
              PRIMARY KEY(source_symbol_id, target_symbol_id, edge_kind)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS telemetry (
              event_id TEXT PRIMARY KEY,
              event_type TEXT NOT NULL,
              object_id TEXT,
              success INTEGER,
              occurred_at TEXT NOT NULL,
              document TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS telemetry_type_time
              ON telemetry(event_type, occurred_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS product_proposals (
              proposal_id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL UNIQUE,
              proposal_hash TEXT NOT NULL,
              proposed_at TEXT NOT NULL,
              document TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS product_receipts (
              receipt_id TEXT PRIMARY KEY,
              proposal_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              proposal_hash TEXT NOT NULL,
              outcome TEXT NOT NULL CHECK(outcome IN ('COMMITTED','VALIDATION_ERROR','TRANSACTION_CONFLICT')),
              decided_at TEXT NOT NULL,
              document TEXT NOT NULL,
              FOREIGN KEY(proposal_id) REFERENCES product_proposals(proposal_id)
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS product_proposals_no_update
            BEFORE UPDATE ON product_proposals
            BEGIN SELECT RAISE(ABORT, 'PRODUCT_PROPOSALS_APPEND_ONLY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS product_proposals_no_delete
            BEFORE DELETE ON product_proposals
            BEGIN SELECT RAISE(ABORT, 'PRODUCT_PROPOSALS_APPEND_ONLY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS product_receipts_no_update
            BEFORE UPDATE ON product_receipts
            BEGIN SELECT RAISE(ABORT, 'PRODUCT_RECEIPTS_APPEND_ONLY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS product_receipts_no_delete
            BEFORE DELETE ON product_receipts
            BEGIN SELECT RAISE(ABORT, 'PRODUCT_RECEIPTS_APPEND_ONLY'); END
            """,
            """
            CREATE TABLE IF NOT EXISTS product_audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              prev_hash TEXT,
              event_hash TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              object_id TEXT,
              occurred_at TEXT NOT NULL,
              payload TEXT NOT NULL
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS product_audit_no_update
            BEFORE UPDATE ON product_audit
            BEGIN SELECT RAISE(ABORT, 'PRODUCT_AUDIT_APPEND_ONLY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS product_audit_no_delete
            BEFORE DELETE ON product_audit
            BEGIN SELECT RAISE(ABORT, 'PRODUCT_AUDIT_APPEND_ONLY'); END
            """,
        )
        with self.transaction():
            for statement in statements:
                self.connection.execute(statement)
            self.connection.execute(
                "INSERT OR IGNORE INTO product_meta(key, value) VALUES('store_version', ?)",
                (str(PRODUCT_STORE_VERSION),),
            )
            try:
                self.connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(
                      document_id UNINDEXED, title, body, tokenize='unicode61'
                    )
                    """
                )
            except sqlite3.OperationalError:
                self._fts_enabled = False
            else:
                self._fts_enabled = True

    def append_audit(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        object_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = occurred_at or utc_now()
        row = self.connection.execute(
            "SELECT seq, event_hash FROM product_audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["event_hash"] if row else None
        event = {
            "event_type": event_type,
            "object_id": object_id,
            "occurred_at": timestamp,
            "payload": dict(payload),
            "prev_hash": prev_hash,
        }
        event_hash = sha256_json(event)
        self.connection.execute(
            """
            INSERT INTO product_audit(prev_hash, event_hash, event_type, object_id, occurred_at, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                prev_hash,
                event_hash,
                event_type,
                object_id,
                timestamp,
                canonical_json(dict(payload)),
            ),
        )
        seq = int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        return {"seq": seq, "event_hash": event_hash, "prev_hash": prev_hash}

    def verify_audit(self) -> dict[str, Any]:
        previous: str | None = None
        count = 0
        for row in self.connection.execute("SELECT * FROM product_audit ORDER BY seq"):
            payload = json.loads(row["payload"])
            event = {
                "event_type": row["event_type"],
                "object_id": row["object_id"],
                "occurred_at": row["occurred_at"],
                "payload": payload,
                "prev_hash": previous,
            }
            expected = sha256_json(event)
            if row["prev_hash"] != previous or row["event_hash"] != expected:
                return {
                    "valid": False,
                    "count": count,
                    "first_invalid_seq": row["seq"],
                }
            previous = row["event_hash"]
            count += 1
        return {"valid": True, "count": count, "head_hash": previous}

    def get_task_capture_receipt(self, trace_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT payload FROM product_audit
            WHERE event_type='TASK_TRACE_CAPTURED' AND object_id=?
            ORDER BY seq DESC LIMIT 1
            """,
            (trace_id,),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        receipt = payload.get("capture_receipt")
        return dict(receipt) if isinstance(receipt, Mapping) else None

    # ------------------------------------------------------------------
    # Product mutation proposals (shared procedural state)

    def commit_product_proposal(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically apply a version-guarded product proposal.

        The kernel remains authoritative for factual/project state.  This
        product proposal boundary gives shared procedural Skills the same
        idempotent, reviewable mutation discipline without creating an
        Agent-specific memory partition.
        """

        normalized = dict(proposal)
        issues = validate_product_object(normalized, "ProductMutationProposal")
        proposal_hash = sha256_json(normalized)
        state_before = self.product_state_hash()
        if issues:
            # A durable rejection receipt requires the proposal identity fields
            # that are themselves part of the schema.  Do not synthesize an
            # identity for malformed input: fail closed before any product
            # table or audit row can change, and expose the validation detail
            # through a stable machine-readable error code.
            raise ProductStoreError(
                "PRODUCT_PROPOSAL_INVALID",
                canonical_json(
                    {
                        "reason_code": "PRODUCT_SCHEMA_VALIDATION_FAILED",
                        "issues": issues,
                        "proposal_hash": proposal_hash,
                        "product_state_hash": state_before,
                    }
                ),
            )
        prior = self.get_product_receipt_by_key(str(normalized["idempotency_key"]))
        if prior is not None:
            if prior["proposal_hash"] == proposal_hash:
                return prior
            return {
                **prior,
                "outcome": "VALIDATION_ERROR",
                "reason_codes": ["PRODUCT_IDEMPOTENCY_KEY_REUSE"],
                "product_state_hash_before": state_before,
                "product_state_hash_after": state_before,
            }
        expected = normalized.get("expected_product_state_hash")
        if expected is not None and expected != state_before:
            return self._persist_product_rejection(
                normalized,
                proposal_hash=proposal_hash,
                outcome="TRANSACTION_CONFLICT",
                reason_codes=("PRODUCT_STATE_HASH_MISMATCH",),
                state_hash=state_before,
            )
        try:
            with self.transaction():
                self._insert_product_proposal(normalized, proposal_hash)
                for operation in normalized["operations"]:
                    self._apply_product_skill_operation(
                        operation, occurred_at=str(normalized["proposed_at"])
                    )
                state_after = self.product_state_hash()
                receipt = self._product_receipt(
                    normalized,
                    proposal_hash=proposal_hash,
                    outcome="COMMITTED",
                    reason_codes=(),
                    state_before=state_before,
                    state_after=state_after,
                )
                self._insert_product_receipt(receipt)
                self.append_audit(
                    "PRODUCT_PROPOSAL_COMMITTED",
                    {"proposal": normalized, "receipt": receipt},
                    object_id=str(normalized["proposal_id"]),
                    occurred_at=receipt["decided_at"],
                )
                return receipt
        except ProductStoreError as exc:
            outcome = (
                "TRANSACTION_CONFLICT"
                if exc.code in {
                    "SKILL_VERSION_MISMATCH",
                    "SKILL_STATUS_MISMATCH",
                    "PRODUCT_STATE_HASH_MISMATCH",
                }
                else "VALIDATION_ERROR"
            )
            return self._persist_product_rejection(
                normalized,
                proposal_hash=proposal_hash,
                outcome=outcome,
                reason_codes=(exc.code,),
                state_hash=state_before,
            )

    def get_product_receipt_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT document FROM product_receipts WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return json.loads(row["document"]) if row else None

    def list_product_proposals(self, *, outcome: str | None = None) -> list[dict[str, Any]]:
        if outcome is None:
            rows = self.connection.execute(
                """
                SELECT p.document AS proposal, r.document AS receipt
                FROM product_proposals p JOIN product_receipts r USING(proposal_id)
                ORDER BY r.rowid
                """
            )
        else:
            rows = self.connection.execute(
                """
                SELECT p.document AS proposal, r.document AS receipt
                FROM product_proposals p JOIN product_receipts r USING(proposal_id)
                WHERE r.outcome=? ORDER BY r.rowid
                """,
                (outcome,),
            )
        return [
            {"proposal": json.loads(row["proposal"]), "receipt": json.loads(row["receipt"])}
            for row in rows
        ]

    def verify_skill_replay(self) -> dict[str, Any]:
        replayed: dict[tuple[str, int], dict[str, Any]] = {}
        for item in self.list_product_proposals(outcome="COMMITTED"):
            for operation in item["proposal"]["operations"]:
                self._replay_skill_operation(
                    replayed, operation, occurred_at=item["proposal"]["proposed_at"]
                )
        actual = {
            (item["skill_id"], int(item["version"])): item
            for item in self.list_skills()
        }
        replay_rows = [replayed[key] for key in sorted(replayed)]
        actual_rows = [actual[key] for key in sorted(actual)]
        return {
            "valid": canonical_json(replay_rows) == canonical_json(actual_rows),
            "proposal_count": len(self.list_product_proposals(outcome="COMMITTED")),
            "replayed_skill_count": len(replay_rows),
            "actual_skill_count": len(actual_rows),
            "replayed_hash": sha256_json(replay_rows),
            "actual_hash": sha256_json(actual_rows),
        }

    def _insert_product_proposal(
        self, proposal: Mapping[str, Any], proposal_hash: str
    ) -> None:
        try:
            self.connection.execute(
                """
                INSERT INTO product_proposals(
                  proposal_id, idempotency_key, proposal_hash, proposed_at, document
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    proposal["proposal_id"],
                    proposal["idempotency_key"],
                    proposal_hash,
                    proposal["proposed_at"],
                    canonical_json(dict(proposal)),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ProductStoreError(
                "PRODUCT_PROPOSAL_ID_REUSE", str(proposal["proposal_id"])
            ) from exc

    def _insert_product_receipt(self, receipt: Mapping[str, Any]) -> None:
        issues = validate_product_object(dict(receipt), "ProductMutationReceipt")
        if issues:
            raise ProductStoreError(
                "PRODUCT_RECEIPT_INVALID", canonical_json(issues)
            )
        self.connection.execute(
            """
            INSERT INTO product_receipts(
              receipt_id, proposal_id, idempotency_key, proposal_hash, outcome,
              decided_at, document
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt["receipt_id"],
                receipt["proposal_id"],
                receipt["idempotency_key"],
                receipt["proposal_hash"],
                receipt["outcome"],
                receipt["decided_at"],
                canonical_json(dict(receipt)),
            ),
        )

    def _persist_product_rejection(
        self,
        proposal: Mapping[str, Any],
        *,
        proposal_hash: str,
        outcome: str,
        reason_codes: Sequence[str],
        state_hash: str,
    ) -> dict[str, Any]:
        prior = self.get_product_receipt_by_key(str(proposal.get("idempotency_key", "")))
        if prior is not None:
            return prior
        receipt = self._product_receipt(
            proposal,
            proposal_hash=proposal_hash,
            outcome=outcome,
            reason_codes=reason_codes,
            state_before=state_hash,
            state_after=state_hash,
        )
        with self.transaction():
            self._insert_product_proposal(proposal, proposal_hash)
            self._insert_product_receipt(receipt)
            self.append_audit(
                "PRODUCT_PROPOSAL_REJECTED",
                {"proposal": dict(proposal), "receipt": receipt},
                object_id=str(proposal.get("proposal_id")),
                occurred_at=receipt["decided_at"],
            )
        return receipt

    @staticmethod
    def _product_receipt(
        proposal: Mapping[str, Any],
        *,
        proposal_hash: str,
        outcome: str,
        reason_codes: Sequence[str],
        state_before: str,
        state_after: str,
    ) -> dict[str, Any]:
        digest = sha256_json(
            {
                "proposal_hash": proposal_hash,
                "outcome": outcome,
                "reason_codes": sorted(reason_codes),
                "state_before": state_before,
                "state_after": state_after,
            }
        ).split(":", 1)[1]
        return {
            "object_type": "PRODUCT_MUTATION_RECEIPT",
            "receipt_id": f"product_receipt_{digest[:24]}",
            "proposal_id": proposal["proposal_id"],
            "proposal_hash": proposal_hash,
            "idempotency_key": proposal["idempotency_key"],
            "outcome": outcome,
            "reason_codes": sorted(set(reason_codes)),
            "product_state_hash_before": state_before,
            "product_state_hash_after": state_after,
            "operation_count": len(proposal.get("operations", [])),
            "decided_at": proposal["proposed_at"],
        }

    def _apply_product_skill_operation(
        self, operation: Mapping[str, Any], *, occurred_at: str
    ) -> None:
        kind = operation["op"]
        if kind in {"CREATE_SKILL", "IMPORT_SKILL"}:
            self.put_skill(operation["skill"])
            return
        skill_id = str(operation["skill_id"])
        version = int(operation.get("version", operation.get("expected_version", 0)))
        current = self.get_skill(skill_id, version=version)
        if current is None:
            raise ProductStoreError(
                "SKILL_NOT_FOUND", f"Skill not found: {skill_id}@{version}"
            )
        if kind == "REVISE_SKILL":
            latest = self.get_skill(skill_id)
            if latest is None or int(latest["version"]) != int(operation["expected_version"]):
                raise ProductStoreError(
                    "SKILL_VERSION_MISMATCH",
                    f"Skill {skill_id} version changed.",
                )
            replacement = operation["replacement_skill"]
            if (
                replacement["skill_id"] != skill_id
                or int(replacement["version"]) != int(operation["expected_version"]) + 1
            ):
                raise ProductStoreError(
                    "SKILL_REVISION_INVALID", "Replacement Skill identity/version mismatch."
                )
            self.put_skill(replacement)
            return
        expected_status = str(operation["expected_status"])
        if current["status"] != expected_status:
            raise ProductStoreError(
                "SKILL_STATUS_MISMATCH",
                f"Skill {skill_id}@{version} is {current['status']}, expected {expected_status}.",
            )
        if kind == "MARK_SKILL_TESTED":
            self.update_skill_status(
                skill_id, version, "TESTED", expected_status="DRAFT", occurred_at=occurred_at
            )
            self.append_audit(
                "SKILL_TEST_EVIDENCE",
                {
                    "skill_id": skill_id,
                    "version": version,
                    "evidence": dict(operation["test_evidence"]),
                },
                object_id=skill_id,
            )
        elif kind == "APPROVE_SKILL":
            self.update_skill_status(
                skill_id, version, "APPROVED", expected_status="TESTED", occurred_at=occurred_at
            )
            self.append_audit(
                "SKILL_APPROVED",
                {
                    "skill_id": skill_id,
                    "version": version,
                    "approval": dict(operation["approval"]),
                },
                object_id=skill_id,
            )
        elif kind == "DEPRECATE_SKILL":
            self.update_skill_status(
                skill_id,
                version,
                "DEPRECATED",
                expected_status=expected_status,
                occurred_at=occurred_at,
            )
            self.append_audit(
                "SKILL_DEPRECATED",
                {
                    "skill_id": skill_id,
                    "version": version,
                    "rationale": operation["rationale"],
                },
                object_id=skill_id,
            )
        else:
            raise ProductStoreError("PRODUCT_OPERATION_UNSUPPORTED", str(kind))

    @staticmethod
    def _replay_skill_operation(
        state: dict[tuple[str, int], dict[str, Any]],
        operation: Mapping[str, Any],
        *,
        occurred_at: str,
    ) -> None:
        kind = operation["op"]
        if kind in {"CREATE_SKILL", "IMPORT_SKILL"}:
            skill = dict(operation["skill"])
            if skill["status"] == "APPROVED":
                for key, existing in state.items():
                    if key[0] == skill["skill_id"] and existing["status"] == "APPROVED":
                        existing["status"] = "DEPRECATED"
            state[(skill["skill_id"], int(skill["version"]))] = skill
            return
        if kind == "REVISE_SKILL":
            skill = dict(operation["replacement_skill"])
            state[(skill["skill_id"], int(skill["version"]))] = skill
            return
        key = (str(operation["skill_id"]), int(operation["version"]))
        current = state[key]
        current = dict(current)
        if kind == "MARK_SKILL_TESTED":
            current["status"] = "TESTED"
        elif kind == "APPROVE_SKILL":
            for other_key, existing in state.items():
                if other_key[0] == key[0] and existing["status"] == "APPROVED":
                    existing["status"] = "DEPRECATED"
                    existing["updated_at"] = occurred_at
            current["status"] = "APPROVED"
        elif kind == "DEPRECATE_SKILL":
            current["status"] = "DEPRECATED"
        current["updated_at"] = occurred_at
        state[key] = current

    # ------------------------------------------------------------------
    # Ingest and draft staging

    def put_batch(self, batch: Mapping[str, Any]) -> None:
        now = batch.get("updated_at") or utc_now()
        existing = self.connection.execute(
            "SELECT batch_id FROM ingest_batches WHERE batch_id = ?", (batch["batch_id"],)
        ).fetchone()
        self.connection.execute(
            """
            INSERT INTO ingest_batches(batch_id, manifest_hash, status, created_at, updated_at, document)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
              status=excluded.status,
              updated_at=excluded.updated_at,
              document=excluded.document
            """,
            (
                batch["batch_id"],
                batch["manifest_hash"],
                batch["status"],
                batch["created_at"],
                now,
                canonical_json(dict(batch)),
            ),
        )
        self.append_audit(
            "INGEST_BATCH_UPDATED" if existing else "INGEST_BATCH_CREATED",
            dict(batch),
            object_id=str(batch["batch_id"]),
            occurred_at=now,
        )

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT document FROM ingest_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        return json.loads(row["document"]) if row else None

    def list_batches(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT document FROM ingest_batches WHERE status = ? ORDER BY created_at, batch_id",
                (status,),
            )
        else:
            rows = self.connection.execute(
                "SELECT document FROM ingest_batches ORDER BY created_at, batch_id"
            )
        return [json.loads(row["document"]) for row in rows]

    def put_ingest_item(self, item: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO ingest_items(
              batch_id, item_id, source_path, source_id, fingerprint, media_type,
              status, revision_id, error_code, document
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id, item_id) DO UPDATE SET
              status=excluded.status,
              revision_id=excluded.revision_id,
              error_code=excluded.error_code,
              document=excluded.document
            """,
            (
                item["batch_id"],
                item["item_id"],
                item["source_path"],
                item["source_id"],
                item["fingerprint"],
                item["media_type"],
                item["status"],
                item.get("revision_id"),
                item.get("error_code"),
                canonical_json(dict(item)),
            ),
        )
        self.append_audit(
            "INGEST_ITEM_UPDATED",
            dict(item),
            object_id=f"{item['batch_id']}:{item['item_id']}",
        )

    def list_ingest_items(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT document FROM ingest_items WHERE batch_id = ? ORDER BY item_id",
            (batch_id,),
        )
        return [json.loads(row["document"]) for row in rows]

    def was_imported(self, source_id: str, fingerprint: str) -> bool:
        return (
            self.connection.execute(
                """
                SELECT 1 FROM ingest_items
                WHERE source_id = ? AND fingerprint = ? AND status IN ('IMPORTED','UNCHANGED')
                LIMIT 1
                """,
                (source_id, fingerprint),
            ).fetchone()
            is not None
        )

    def put_draft(self, draft: Mapping[str, Any]) -> bool:
        existing = self.connection.execute(
            """
            SELECT draft_id FROM drafts
            WHERE batch_id IS ? AND dependency_digest = ? AND draft_kind = ?
            """,
            (draft.get("batch_id"), draft["dependency_digest"], draft["draft_kind"]),
        ).fetchone()
        if existing:
            return False
        self.connection.execute(
            """
            INSERT INTO drafts(
              draft_id, batch_id, draft_kind, status, version, dependency_digest,
              created_at, updated_at, expires_at, document, provenance, receipt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                draft["draft_id"],
                draft.get("batch_id"),
                draft["draft_kind"],
                draft["status"],
                draft["version"],
                draft["dependency_digest"],
                draft["created_at"],
                draft["updated_at"],
                draft.get("expires_at"),
                canonical_json(dict(draft["document"])),
                canonical_json(dict(draft["provenance"])),
            ),
        )
        self.append_audit(
            "DRAFT_CREATED", dict(draft), object_id=str(draft["draft_id"])
        )
        return True

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        return self._draft_row(row) if row else None

    def list_drafts(
        self,
        *,
        status: str | None = None,
        draft_kind: str | None = None,
        batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status = ?")
            values.append(status)
        if draft_kind:
            clauses.append("draft_kind = ?")
            values.append(draft_kind)
        if batch_id:
            clauses.append("batch_id = ?")
            values.append(batch_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM drafts{where} ORDER BY created_at, draft_id", values
        )
        return [self._draft_row(row) for row in rows]

    def update_draft(
        self,
        draft_id: str,
        *,
        status: str | None = None,
        document: Mapping[str, Any] | None = None,
        receipt: Mapping[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current = self.get_draft(draft_id)
        if current is None:
            raise ProductStoreError("DRAFT_NOT_FOUND", f"Draft not found: {draft_id}")
        if expected_version is not None and current["version"] != expected_version:
            raise ProductStoreError(
                "DRAFT_VERSION_MISMATCH",
                f"Draft {draft_id} is version {current['version']}, expected {expected_version}.",
            )
        next_version = current["version"] + (1 if document is not None else 0)
        next_status = status or current["status"]
        next_document = dict(document) if document is not None else current["document"]
        now = utc_now()
        self.connection.execute(
            """
            UPDATE drafts SET status = ?, version = ?, updated_at = ?, document = ?, receipt = ?
            WHERE draft_id = ?
            """,
            (
                next_status,
                next_version,
                now,
                canonical_json(next_document),
                canonical_json(dict(receipt)) if receipt is not None else None,
                draft_id,
            ),
        )
        event_payload = {
            "draft_id": draft_id,
            "previous_status": current["status"],
            "status": next_status,
            "previous_version": current["version"],
            "version": next_version,
            "receipt": dict(receipt) if receipt is not None else None,
        }
        self.append_audit("DRAFT_UPDATED", event_payload, object_id=draft_id, occurred_at=now)
        updated = self.get_draft(draft_id)
        assert updated is not None
        return updated

    @staticmethod
    def _draft_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "draft_id": row["draft_id"],
            "batch_id": row["batch_id"],
            "draft_kind": row["draft_kind"],
            "status": row["status"],
            "version": row["version"],
            "dependency_digest": row["dependency_digest"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
            "document": json.loads(row["document"]),
            "provenance": json.loads(row["provenance"]),
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
        }

    # ------------------------------------------------------------------
    # Derived memory artifacts

    def put_artifact(self, artifact: Mapping[str, Any]) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT version, dependency_digest, status FROM artifacts WHERE artifact_id = ?",
            (artifact["artifact_id"],),
        ).fetchone()
        if row and row["dependency_digest"] == artifact["dependency_digest"]:
            existing = self.get_artifact(str(artifact["artifact_id"]))
            assert existing is not None
            return existing
        version = int(row["version"]) + 1 if row else int(artifact.get("version", 1))
        now = artifact.get("updated_at") or utc_now()
        normalized = dict(artifact)
        normalized["version"] = version
        normalized["updated_at"] = now
        normalized.setdefault("created_at", now)
        self.connection.execute(
            """
            INSERT INTO artifacts(
              artifact_id, artifact_type, scope, title, status, version,
              dependency_digest, builder_version, created_at, updated_at,
              document, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
              artifact_type=excluded.artifact_type,
              scope=excluded.scope,
              title=excluded.title,
              status=excluded.status,
              version=excluded.version,
              dependency_digest=excluded.dependency_digest,
              builder_version=excluded.builder_version,
              updated_at=excluded.updated_at,
              document=excluded.document,
              provenance=excluded.provenance
            """,
            (
                normalized["artifact_id"],
                normalized["artifact_type"],
                normalized["scope"],
                normalized["title"],
                normalized["status"],
                normalized["version"],
                normalized["dependency_digest"],
                normalized["builder_version"],
                normalized["created_at"],
                normalized["updated_at"],
                canonical_json(dict(normalized["document"])),
                canonical_json(dict(normalized.get("provenance", {}))),
            ),
        )
        self.append_audit(
            "ARTIFACT_BUILT", normalized, object_id=str(normalized["artifact_id"]), occurred_at=now
        )
        return normalized

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        return self._artifact_row(row) if row else None

    def list_artifacts(
        self, *, artifact_type: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if artifact_type:
            clauses.append("artifact_type = ?")
            values.append(artifact_type)
        if status:
            clauses.append("status = ?")
            values.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM artifacts{where} ORDER BY artifact_type, artifact_id", values
        )
        return [self._artifact_row(row) for row in rows]

    def mark_artifacts_stale(self, artifact_ids: Iterable[str]) -> int:
        count = 0
        for artifact_id in sorted(set(artifact_ids)):
            row = self.connection.execute(
                "SELECT status FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            if row and row["status"] != "STALE":
                self.connection.execute(
                    "UPDATE artifacts SET status='STALE', updated_at=? WHERE artifact_id=?",
                    (utc_now(), artifact_id),
                )
                self.append_audit(
                    "ARTIFACT_MARKED_STALE", {"artifact_id": artifact_id}, object_id=artifact_id
                )
                count += 1
        return count

    @staticmethod
    def _artifact_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "artifact_id": row["artifact_id"],
            "artifact_type": row["artifact_type"],
            "scope": row["scope"],
            "title": row["title"],
            "status": row["status"],
            "version": row["version"],
            "dependency_digest": row["dependency_digest"],
            "builder_version": row["builder_version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "document": json.loads(row["document"]),
            "provenance": json.loads(row["provenance"]),
        }

    # ------------------------------------------------------------------
    # Shared Skill catalog

    def put_skill(self, skill: Mapping[str, Any]) -> dict[str, Any]:
        skill_id = str(skill["skill_id"])
        version = int(skill["version"])
        existing = self.connection.execute(
            "SELECT content_hash FROM skills WHERE skill_id=? AND version=?",
            (skill_id, version),
        ).fetchone()
        if existing:
            if existing["content_hash"] != skill["content_hash"]:
                raise ProductStoreError(
                    "SKILL_VERSION_REUSE",
                    f"Skill {skill_id} version {version} already has different content.",
                )
            found = self.get_skill(skill_id, version=version)
            assert found is not None
            return found
        if skill["status"] == "APPROVED":
            approved = self.connection.execute(
                "SELECT version FROM skills WHERE skill_id=? AND status='APPROVED'",
                (skill_id,),
            ).fetchone()
            if approved:
                self.connection.execute(
                    "UPDATE skills SET status='DEPRECATED', updated_at=? WHERE skill_id=? AND version=?",
                    (utc_now(), skill_id, approved["version"]),
                )
        now = skill.get("updated_at") or utc_now()
        normalized = dict(skill)
        normalized.setdefault("created_at", now)
        normalized["updated_at"] = now
        self.connection.execute(
            """
            INSERT INTO skills(skill_id, version, status, content_hash, created_at, updated_at, document, provenance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                version,
                normalized["status"],
                normalized["content_hash"],
                normalized["created_at"],
                normalized["updated_at"],
                canonical_json(dict(normalized["document"])),
                canonical_json(dict(normalized.get("provenance", {}))),
            ),
        )
        self.append_audit("SKILL_VERSION_ADDED", normalized, object_id=skill_id, occurred_at=now)
        return normalized

    def get_skill(
        self,
        skill_id: str,
        *,
        version: int | None = None,
        approved_only: bool = False,
    ) -> dict[str, Any] | None:
        if version is not None:
            row = self.connection.execute(
                "SELECT * FROM skills WHERE skill_id=? AND version=?",
                (skill_id, version),
            ).fetchone()
        elif approved_only:
            row = self.connection.execute(
                "SELECT * FROM skills WHERE skill_id=? AND status='APPROVED'",
                (skill_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM skills WHERE skill_id=? ORDER BY version DESC LIMIT 1",
                (skill_id,),
            ).fetchone()
        return self._skill_row(row) if row else None

    def list_skills(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM skills WHERE status=? ORDER BY skill_id, version", (status,)
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM skills ORDER BY skill_id, version"
            )
        return [self._skill_row(row) for row in rows]

    def update_skill_status(
        self,
        skill_id: str,
        version: int,
        status: str,
        *,
        expected_status: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_skill(skill_id, version=version)
        if current is None:
            raise ProductStoreError("SKILL_NOT_FOUND", f"Skill not found: {skill_id}@{version}")
        if expected_status and current["status"] != expected_status:
            raise ProductStoreError(
                "SKILL_STATUS_MISMATCH",
                f"Skill {skill_id}@{version} is {current['status']}, expected {expected_status}.",
            )
        now = occurred_at or utc_now()
        if status == "APPROVED":
            self.connection.execute(
                """
                UPDATE skills SET status='DEPRECATED', updated_at=?
                WHERE skill_id=? AND status='APPROVED' AND version<>?
                """,
                (now, skill_id, version),
            )
        self.connection.execute(
            "UPDATE skills SET status=?, updated_at=? WHERE skill_id=? AND version=?",
            (status, now, skill_id, version),
        )
        self.append_audit(
            "SKILL_STATUS_CHANGED",
            {
                "skill_id": skill_id,
                "version": version,
                "previous_status": current["status"],
                "status": status,
            },
            object_id=skill_id,
            occurred_at=now,
        )
        updated = self.get_skill(skill_id, version=version)
        assert updated is not None
        return updated

    @staticmethod
    def _skill_row(row: sqlite3.Row) -> dict[str, Any]:
        document = json.loads(row["document"])
        return {
            "object_type": "SKILL_RECORD",
            **document,
            "skill_id": row["skill_id"],
            "version": row["version"],
            "status": row["status"],
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "document": document,
            "provenance": json.loads(row["provenance"]),
        }

    # ------------------------------------------------------------------
    # Retrieval, graph, and code index

    def replace_retrieval_documents(self, documents: Sequence[Mapping[str, Any]]) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM retrieval_documents")
            if self._fts_enabled:
                self.connection.execute("DELETE FROM retrieval_fts")
            for document in sorted(documents, key=lambda item: str(item["document_id"])):
                self.connection.execute(
                    """
                    INSERT INTO retrieval_documents(document_id, kind, title, body, fingerprint, metadata, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document["document_id"],
                        document["kind"],
                        document["title"],
                        document["body"],
                        document["fingerprint"],
                        canonical_json(dict(document.get("metadata", {}))),
                        document.get("updated_at") or utc_now(),
                    ),
                )
                if self._fts_enabled:
                    self.connection.execute(
                        "INSERT INTO retrieval_fts(document_id, title, body) VALUES (?, ?, ?)",
                        (document["document_id"], document["title"], document["body"]),
                    )
            self.append_audit(
                "RETRIEVAL_INDEX_REBUILT", {"count": len(documents), "fts": self._fts_enabled}
            )

    def search(
        self,
        query: str,
        *,
        kinds: Sequence[str] = (),
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 200:
            raise ProductStoreError("INVALID_SEARCH_LIMIT", "Search limit must be 1..200.")
        normalized_query = query.strip()
        if not normalized_query:
            return []
        kind_filter = ""
        values: list[Any] = []
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            kind_filter = f" AND d.kind IN ({placeholders})"
            values.extend(kinds)
        if self._fts_enabled:
            # FTS MATCH does not accept bound column names, only the query is bound.
            sql = (
                "SELECT d.*, bm25(retrieval_fts) AS score "
                "FROM retrieval_fts JOIN retrieval_documents d USING(document_id) "
                f"WHERE retrieval_fts MATCH ?{kind_filter} "
                "ORDER BY score, d.document_id LIMIT ?"
            )
            rows = self.connection.execute(sql, [normalized_query, *values, limit]).fetchall()
            return [self._search_row(row, score=-float(row["score"])) for row in rows]
        tokens = sorted({token.casefold() for token in normalized_query.split() if token})
        rows = self.connection.execute(
            "SELECT * FROM retrieval_documents ORDER BY document_id"
        ).fetchall()
        scored: list[tuple[int, str, sqlite3.Row]] = []
        for row in rows:
            if kinds and row["kind"] not in kinds:
                continue
            haystack = f"{row['title']}\n{row['body']}".casefold()
            score = sum(haystack.count(token) for token in tokens)
            if score:
                scored.append((score, row["document_id"], row))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [self._search_row(row, score=float(score)) for score, _, row in scored[:limit]]

    @staticmethod
    def _search_row(row: sqlite3.Row, *, score: float) -> dict[str, Any]:
        return {
            "document_id": row["document_id"],
            "kind": row["kind"],
            "title": row["title"],
            "body": row["body"],
            "fingerprint": row["fingerprint"],
            "metadata": json.loads(row["metadata"]),
            "score": score,
        }

    def replace_links(self, links: Sequence[Mapping[str, Any]]) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM links")
            for link in sorted(
                links,
                key=lambda item: (
                    str(item["source_id"]),
                    str(item["target_id"]),
                    str(item["relation"]),
                ),
            ):
                self.connection.execute(
                    "INSERT INTO links(source_id, target_id, relation, metadata) VALUES (?, ?, ?, ?)",
                    (
                        link["source_id"],
                        link["target_id"],
                        link["relation"],
                        canonical_json(dict(link.get("metadata", {}))),
                    ),
                )
            self.append_audit("LINK_GRAPH_REBUILT", {"count": len(links)})

    def list_links(
        self, *, object_id: str | None = None, relation: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if object_id:
            clauses.append("(source_id=? OR target_id=?)")
            values.extend((object_id, object_id))
        if relation:
            clauses.append("relation=?")
            values.append(relation)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM links{where} ORDER BY source_id, target_id, relation", values
        )
        return [
            {
                "source_id": row["source_id"],
                "target_id": row["target_id"],
                "relation": row["relation"],
                "metadata": json.loads(row["metadata"]),
            }
            for row in rows
        ]

    def replace_code_index(
        self,
        symbols: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
    ) -> None:
        with self.transaction():
            self.connection.execute("DELETE FROM code_edges")
            self.connection.execute("DELETE FROM code_symbols")
            for symbol in sorted(symbols, key=lambda item: str(item["symbol_id"])):
                self.connection.execute(
                    """
                    INSERT INTO code_symbols(
                      symbol_id, source_revision_id, file_path, name, qualified_name,
                      symbol_kind, start_line, end_line, signature, document
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol["symbol_id"],
                        symbol["source_revision_id"],
                        symbol["file_path"],
                        symbol["name"],
                        symbol["qualified_name"],
                        symbol["symbol_kind"],
                        symbol["start_line"],
                        symbol["end_line"],
                        symbol.get("signature"),
                        canonical_json(dict(symbol)),
                    ),
                )
            known = {str(symbol["symbol_id"]) for symbol in symbols}
            for edge in sorted(
                edges,
                key=lambda item: (
                    str(item["source_symbol_id"]),
                    str(item["target_symbol_id"]),
                    str(item["edge_kind"]),
                ),
            ):
                if edge["source_symbol_id"] not in known or edge["target_symbol_id"] not in known:
                    continue
                self.connection.execute(
                    "INSERT INTO code_edges(source_symbol_id, target_symbol_id, edge_kind, metadata) VALUES (?, ?, ?, ?)",
                    (
                        edge["source_symbol_id"],
                        edge["target_symbol_id"],
                        edge["edge_kind"],
                        canonical_json(dict(edge.get("metadata", {}))),
                    ),
                )
            self.append_audit(
                "CODE_INDEX_REBUILT", {"symbols": len(symbols), "edges": len(edges)}
            )

    def get_symbol(self, symbol_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT document FROM code_symbols WHERE symbol_id=?", (symbol_id,)
        ).fetchone()
        return json.loads(row["document"]) if row else None

    def find_symbols(self, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT document FROM code_symbols
            WHERE name LIKE ? OR qualified_name LIKE ?
            ORDER BY qualified_name, symbol_id LIMIT ?
            """,
            (f"%{name}%", f"%{name}%", limit),
        )
        return [json.loads(row["document"]) for row in rows]

    def code_edges(self, symbol_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM code_edges
            WHERE source_symbol_id=? OR target_symbol_id=?
            ORDER BY edge_kind, source_symbol_id, target_symbol_id
            """,
            (symbol_id, symbol_id),
        )
        return [
            {
                "source_symbol_id": row["source_symbol_id"],
                "target_symbol_id": row["target_symbol_id"],
                "edge_kind": row["edge_kind"],
                "metadata": json.loads(row["metadata"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Telemetry and catalog

    def record_telemetry(self, event: Mapping[str, Any]) -> bool:
        try:
            self.connection.execute(
                """
                INSERT INTO telemetry(event_id, event_type, object_id, success, occurred_at, document)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["event_type"],
                    event.get("object_id"),
                    None if event.get("success") is None else int(bool(event["success"])),
                    event["occurred_at"],
                    canonical_json(dict(event)),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        self.append_audit(
            "TELEMETRY_RECORDED", dict(event), object_id=event.get("object_id")
        )
        return True

    def list_telemetry(self, *, event_type: str | None = None) -> list[dict[str, Any]]:
        if event_type:
            rows = self.connection.execute(
                "SELECT document FROM telemetry WHERE event_type=? ORDER BY occurred_at, event_id",
                (event_type,),
            )
        else:
            rows = self.connection.execute(
                "SELECT document FROM telemetry ORDER BY occurred_at, event_id"
            )
        return [json.loads(row["document"]) for row in rows]

    def product_state_hash(self) -> str:
        state: dict[str, Any] = {}
        for table, order in (
            ("ingest_batches", "batch_id"),
            ("ingest_items", "batch_id, item_id"),
            ("drafts", "draft_id"),
            ("artifacts", "artifact_id"),
            ("skills", "skill_id, version"),
            ("retrieval_documents", "document_id"),
            ("links", "source_id, target_id, relation"),
            ("code_symbols", "symbol_id"),
            ("code_edges", "source_symbol_id, target_symbol_id, edge_kind"),
        ):
            rows = self.connection.execute(f"SELECT * FROM {table} ORDER BY {order}")
            state[table] = [dict(row) for row in rows]
        return sha256_json(state)

    def checkpoint(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


__all__ = [
    "PRODUCT_DATABASE_FILENAME",
    "PRODUCT_STORE_VERSION",
    "ProductStore",
    "ProductStoreError",
    "utc_now",
]
