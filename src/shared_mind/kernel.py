from __future__ import annotations

import base64
import binascii
import copy
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes

from .canonical import canonical_json, sha256_bytes, sha256_json
from .continuity import (
    ContinuityConflict,
    ContinuityValidationError,
    apply_event as apply_continuity_event,
    apply_operation as apply_continuity_operation,
    create_schema as create_continuity_schema,
    required_reads as continuity_required_reads,
    state_rows as continuity_state_rows,
    validate_guard as validate_continuity_guard,
    validate_read as validate_continuity_read,
)
from .validation import build_contract_validator


@dataclass(frozen=True)
class Receipt:
    proposal_id: str
    outcome: str
    reason_codes: tuple[str, ...]
    ledger_seq: int | None
    state_root: str
    conflict_ids: tuple[str, ...] = ()


class ValidationFailure(Exception):
    def __init__(self, code: str):
        self.code = code


class TransactionConflict(Exception):
    def __init__(self, code: str):
        self.code = code


class Kernel:
    """SQLite implementation of the first Atlas vertical slice."""

    SUPPORTED_VERSIONS = {
        "schema": "1.0.0",
        "conflict_rules": "conflict-rules@1",
        "projection": "markdown-projection@1",
    }

    def __init__(
        self,
        database: str | Path,
        registry: dict[str, Any],
        *,
        schema: dict[str, Any] | None = None,
    ) -> None:
        self.database = str(database)
        self.registry = registry
        self.predicates = {item["key"]: item for item in registry["predicates"]}
        self.contract_validator = build_contract_validator(schema)
        registry_errors = list(self.contract_validator.iter_errors(registry))
        if registry_errors:
            raise ValueError(f"Invalid predicate registry: {registry_errors[0].message}")
        self.connection = sqlite3.connect(self.database, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sources (
              revision_id TEXT PRIMARY KEY,
              content_hash TEXT NOT NULL,
              document TEXT NOT NULL,
              content BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claims (
              claim_id TEXT PRIMARY KEY,
              proposition_hash TEXT NOT NULL,
              proposition TEXT NOT NULL,
              document TEXT NOT NULL,
              status TEXT NOT NULL,
              version INTEGER NOT NULL,
              superseded_by TEXT
            );
            CREATE TABLE IF NOT EXISTS evidence (
              evidence_link_id TEXT PRIMARY KEY,
              claim_id TEXT NOT NULL REFERENCES claims(claim_id),
              source_revision_id TEXT NOT NULL REFERENCES sources(revision_id),
              document TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conflicts (
              conflict_id TEXT PRIMARY KEY,
              family_key TEXT NOT NULL,
              kind TEXT NOT NULL,
              member_digest TEXT NOT NULL,
              members TEXT NOT NULL,
              status TEXT NOT NULL,
              episode INTEGER NOT NULL,
              version INTEGER NOT NULL,
              resolution TEXT,
              opened_seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              prev_hash TEXT,
              entry_hash TEXT NOT NULL UNIQUE,
              proposal_hash TEXT NOT NULL,
              proposal TEXT NOT NULL,
              events TEXT NOT NULL,
              pre_state_root TEXT NOT NULL,
              state_root TEXT NOT NULL,
              committed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              idempotency_key TEXT NOT NULL,
              proposal_hash TEXT NOT NULL,
              proposal_id TEXT NOT NULL,
              outcome TEXT NOT NULL,
              reason_codes TEXT NOT NULL,
              ledger_seq INTEGER,
              state_root TEXT NOT NULL,
              conflict_ids TEXT NOT NULL
            );
            """
        )
        self._migrate_schema()
        create_continuity_schema(self.connection)
        self._create_immutability_triggers()

    def _migrate_schema(self) -> None:
        conflict_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(conflicts)")
        }
        if "version" not in conflict_columns:
            self.connection.execute(
                "ALTER TABLE conflicts ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
            )
        if "resolution" not in conflict_columns:
            self.connection.execute("ALTER TABLE conflicts ADD COLUMN resolution TEXT")
        if "opened_seq" not in conflict_columns:
            self.connection.execute(
                "ALTER TABLE conflicts ADD COLUMN opened_seq INTEGER NOT NULL DEFAULT 1"
            )

        ledger_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(ledger)")
        }
        if "pre_state_root" not in ledger_columns:
            self.connection.execute("ALTER TABLE ledger ADD COLUMN pre_state_root TEXT")
        if "committed_at" not in ledger_columns:
            self.connection.execute("ALTER TABLE ledger ADD COLUMN committed_at TEXT")

        receipt_columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(receipts)")
        }
        if "id" not in receipt_columns:
            self.connection.executescript(
                """
                ALTER TABLE receipts RENAME TO receipts_legacy;
                CREATE TABLE receipts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  idempotency_key TEXT NOT NULL,
                  proposal_hash TEXT NOT NULL,
                  proposal_id TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  reason_codes TEXT NOT NULL,
                  ledger_seq INTEGER,
                  state_root TEXT NOT NULL,
                  conflict_ids TEXT NOT NULL
                );
                INSERT INTO receipts(
                  idempotency_key, proposal_hash, proposal_id, outcome,
                  reason_codes, ledger_seq, state_root, conflict_ids
                )
                SELECT idempotency_key, proposal_hash, proposal_id, outcome,
                       reason_codes, ledger_seq, state_root, conflict_ids
                FROM receipts_legacy;
                DROP TABLE receipts_legacy;
                """
            )

    def _create_immutability_triggers(self) -> None:
        """Reject ordinary DML that violates append-only/immutable records.

        SQLite database owners can deliberately drop triggers for forensic
        repair, but application code and accidental SQL cannot rewrite the
        ledger, receipts, or an existing source revision.
        """

        self.connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS ledger_no_update
            BEFORE UPDATE ON ledger
            BEGIN
              SELECT RAISE(ABORT, 'LEDGER_APPEND_ONLY');
            END;
            CREATE TRIGGER IF NOT EXISTS ledger_no_delete
            BEFORE DELETE ON ledger
            BEGIN
              SELECT RAISE(ABORT, 'LEDGER_APPEND_ONLY');
            END;
            CREATE TRIGGER IF NOT EXISTS receipts_no_update
            BEFORE UPDATE ON receipts
            BEGIN
              SELECT RAISE(ABORT, 'RECEIPT_APPEND_ONLY');
            END;
            CREATE TRIGGER IF NOT EXISTS receipts_no_delete
            BEFORE DELETE ON receipts
            BEGIN
              SELECT RAISE(ABORT, 'RECEIPT_APPEND_ONLY');
            END;
            CREATE TRIGGER IF NOT EXISTS sources_no_update
            BEFORE UPDATE ON sources
            BEGIN
              SELECT RAISE(ABORT, 'SOURCE_REVISION_IMMUTABLE');
            END;
            CREATE TRIGGER IF NOT EXISTS sources_no_delete
            BEFORE DELETE ON sources
            BEGIN
              SELECT RAISE(ABORT, 'SOURCE_REVISION_IMMUTABLE');
            END;
            CREATE TRIGGER IF NOT EXISTS sources_no_duplicate_insert
            BEFORE INSERT ON sources
            WHEN EXISTS (
              SELECT 1 FROM sources WHERE revision_id = NEW.revision_id
            )
            BEGIN
              SELECT RAISE(ABORT, 'SOURCE_REVISION_IMMUTABLE');
            END;
            """
        )

    def register_source(self, source: dict[str, Any], content: bytes) -> None:
        self._validate_contract_object(source)
        if sha256_bytes(content) != source["content_hash"]:
            raise ValidationFailure("SOURCE_CONTENT_HASH_MISMATCH")
        document = canonical_json(source)
        with self.connection:
            row = self.connection.execute(
                "SELECT content_hash FROM sources WHERE revision_id = ?", (source["revision_id"],)
            ).fetchone()
            if row and row["content_hash"] != source["content_hash"]:
                raise ValidationFailure("SOURCE_REVISION_IMMUTABILITY_VIOLATION")
            if row:
                return
            self.connection.execute(
                "INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?)",
                (source["revision_id"], source["content_hash"], document, content),
            )

    def commit(self, proposal: Any) -> Receipt:
        proposal_id = proposal.get("proposal_id", "") if isinstance(proposal, dict) else ""
        key = proposal.get("idempotency_key", "") if isinstance(proposal, dict) else ""
        try:
            proposal_hash = sha256_json(proposal)
        except (TypeError, ValueError):
            return Receipt(
                proposal_id,
                "VALIDATION_ERROR",
                ("MALFORMED_PROPOSAL",),
                None,
                self.state_root(),
            )

        outcome = "COMMITTED"
        reasons: tuple[str, ...] = ()
        conflict_ids: tuple[str, ...] = ()
        ledger_seq: int | None = None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            prior = self.connection.execute(
                "SELECT * FROM receipts WHERE idempotency_key = ? ORDER BY id LIMIT 1",
                (key,),
            ).fetchone()
            if prior:
                self.connection.execute("ROLLBACK")
                if prior["proposal_hash"] != proposal_hash:
                    receipt = Receipt(
                        proposal_id,
                        "VALIDATION_ERROR",
                        ("IDEMPOTENCY_KEY_REUSE",),
                        None,
                        self.state_root(),
                    )
                    self._insert_receipt(
                        key,
                        proposal_hash,
                        proposal_id,
                        receipt.outcome,
                        receipt.reason_codes,
                        None,
                        receipt.state_root,
                        (),
                    )
                    return receipt
                return self._row_to_receipt(prior)
            self._validate_contract_object(proposal)
            self._validate_versions(proposal)
            self._validate_required_operation_reads(proposal)
            self._validate_reads_and_guards(proposal)
            pre_root = self.state_root()
            next_seq = self._next_ledger_seq()
            events: list[dict[str, Any]] = []
            new_conflicts: list[str] = []
            self._current_proposer = proposal["proposer"]
            self._current_opened_seq = next_seq
            for operation in proposal["operations"]:
                self._apply_operation(operation, events, new_conflicts)
            conflict_ids = tuple(sorted(set(new_conflicts)))
            if conflict_ids:
                outcome = "FACT_CONFLICT"
            post_root = self.state_root()
            previous = self.connection.execute("SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            committed_at = proposal["proposed_at"]
            envelope = self._ledger_envelope(
                seq=next_seq,
                prev_hash=previous["entry_hash"] if previous else None,
                proposal_hash=proposal_hash,
                pre_state_root=pre_root,
                post_state_root=post_root,
                versions=proposal["versions"],
                events=events,
                committed_at=committed_at,
            )
            entry_hash = sha256_json(envelope)
            cursor = self.connection.execute(
                """INSERT INTO ledger(
                     seq, prev_hash, entry_hash, proposal_hash, proposal, events,
                     pre_state_root, state_root, committed_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    next_seq,
                    envelope["prev_hash"],
                    entry_hash,
                    proposal_hash,
                    canonical_json(proposal),
                    canonical_json(events),
                    pre_root,
                    post_root,
                    committed_at,
                ),
            )
            ledger_seq = int(cursor.lastrowid)
            self._insert_receipt(key, proposal_hash, proposal["proposal_id"], outcome, (), ledger_seq, post_root, conflict_ids)
            self.connection.execute("COMMIT")
        except TransactionConflict as exc:
            self._rollback_if_needed()
            outcome, reasons = "TRANSACTION_CONFLICT", (exc.code,)
            post_root = self.state_root()
            self._insert_receipt(key, proposal_hash, proposal_id, outcome, reasons, None, post_root, ())
        except sqlite3.IntegrityError as exc:
            self._rollback_if_needed()
            outcome, reasons = "VALIDATION_ERROR", (self._integrity_reason(exc),)
            post_root = self.state_root()
            self._insert_receipt(
                key,
                proposal_hash,
                proposal_id,
                outcome,
                reasons,
                None,
                post_root,
                (),
            )
        except (ValidationFailure, KeyError, TypeError, ValueError) as exc:
            self._rollback_if_needed()
            code = exc.code if isinstance(exc, ValidationFailure) else "MALFORMED_PROPOSAL"
            outcome, reasons = "VALIDATION_ERROR", (code,)
            post_root = self.state_root()
            self._insert_receipt(key, proposal_hash, proposal_id, outcome, reasons, None, post_root, ())
        except Exception:
            self._rollback_if_needed()
            raise
        return Receipt(proposal_id, outcome, reasons, ledger_seq, post_root, conflict_ids)

    def _insert_receipt(self, key: str, proposal_hash: str, proposal_id: str, outcome: str, reasons: tuple[str, ...], ledger_seq: int | None, root: str, conflicts: tuple[str, ...]) -> None:
        # The caller owns the transaction. On accepted mutations this write must
        # be atomic with ledger and materialized-state updates; rejected writes
        # run in SQLite autocommit mode after the mutation transaction rolls back.
        self.connection.execute(
            """INSERT INTO receipts(
                 idempotency_key, proposal_hash, proposal_id, outcome,
                 reason_codes, ledger_seq, state_root, conflict_ids
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, proposal_hash, proposal_id, outcome, canonical_json(reasons), ledger_seq, root, canonical_json(conflicts)),
        )

    def _validate_versions(self, proposal: dict[str, Any]) -> None:
        versions = proposal["versions"]
        if versions["schema"] != self.SUPPORTED_VERSIONS["schema"]:
            raise ValidationFailure("UNSUPPORTED_SCHEMA_VERSION")
        if versions["predicate_registry"] != self.registry["version"]:
            raise ValidationFailure("UNSUPPORTED_PREDICATE_REGISTRY")
        if versions["conflict_rules"] != self.SUPPORTED_VERSIONS["conflict_rules"]:
            raise ValidationFailure("UNSUPPORTED_CONFLICT_RULES_VERSION")
        if versions["guard_dsl"] != self.registry["guard_dsl_version"]:
            raise ValidationFailure("UNSUPPORTED_GUARD_DSL_VERSION")
        if versions["projection"] != self.SUPPORTED_VERSIONS["projection"]:
            raise ValidationFailure("UNSUPPORTED_PROJECTION_VERSION")

    def _validate_contract_object(self, value: Any) -> None:
        if next(self.contract_validator.iter_errors(value), None) is not None:
            raise ValidationFailure("SCHEMA_VALIDATION_FAILED")

    def _validate_required_operation_reads(self, proposal: dict[str, Any]) -> None:
        aggregate_reads = {
            (read["aggregate_type"], read["aggregate_id"])
            for read in proposal["reads"]
            if read["kind"] == "AGGREGATE"
        }
        missing_read_codes = {
            "DECISION_RECORD": "MISSING_REQUIRED_DECISION_READ",
            "OPEN_QUESTION": "MISSING_REQUIRED_QUESTION_READ",
            "WORK_ITEM": "MISSING_REQUIRED_WORK_ITEM_READ",
        }
        for operation in proposal["operations"]:
            if (
                operation["op"] in {"SUPERSEDE_CLAIM", "RETRACT_CLAIM"}
                and ("CLAIM", operation["target_claim_id"]) not in aggregate_reads
            ):
                raise ValidationFailure("MISSING_REQUIRED_CLAIM_READ")
            if (
                operation["op"] == "RESOLVE_CONFLICT"
                and ("CONFLICT", operation["conflict_id"]) not in aggregate_reads
            ):
                raise ValidationFailure("MISSING_REQUIRED_CONFLICT_READ")
            for required in continuity_required_reads(operation):
                if (required.aggregate_type, required.aggregate_id) not in aggregate_reads:
                    raise ValidationFailure(missing_read_codes[required.aggregate_type])

    def _validate_reads_and_guards(self, proposal: dict[str, Any]) -> None:
        for read in proposal.get("reads", []):
            if read["kind"] == "COLLECTION":
                if self._active_set_digest(read["family_key"]) != read["expected_digest"]:
                    raise TransactionConflict("ACTIVE_SET_DIGEST_MISMATCH")
                continue
            try:
                if validate_continuity_read(self.connection, read):
                    continue
            except ContinuityConflict as exc:
                raise TransactionConflict(exc.code) from exc
            aggregate_type = read["aggregate_type"]
            if aggregate_type == "CLAIM":
                row = self.connection.execute(
                    "SELECT version FROM claims WHERE claim_id = ?",
                    (read["aggregate_id"],),
                ).fetchone()
                if not row or row["version"] != read["expected_version"]:
                    raise TransactionConflict("CLAIM_VERSION_MISMATCH")
            elif aggregate_type == "CONFLICT":
                row = self.connection.execute(
                    "SELECT version FROM conflicts WHERE conflict_id = ?",
                    (read["aggregate_id"],),
                ).fetchone()
                if not row or row["version"] != read["expected_version"]:
                    raise TransactionConflict("CONFLICT_VERSION_MISMATCH")
            elif aggregate_type == "SOURCE_REVISION":
                exists = self.connection.execute(
                    "SELECT 1 FROM sources WHERE revision_id = ?",
                    (read["aggregate_id"],),
                ).fetchone()
                version = 1 if exists else 0
                if version != read["expected_version"]:
                    raise TransactionConflict("SOURCE_REVISION_VERSION_MISMATCH")
        for guard in proposal.get("guards", []):
            try:
                if validate_continuity_guard(self.connection, guard):
                    continue
            except ContinuityConflict as exc:
                raise TransactionConflict(exc.code) from exc
            if guard["op"] == "CLAIM_STATUS_EQ":
                row = self.connection.execute("SELECT status FROM claims WHERE claim_id = ?", (guard["claim_id"],)).fetchone()
                if not row or row["status"] != guard["expected_status"]:
                    raise TransactionConflict("CLAIM_STATUS_MISMATCH")
            elif guard["op"] == "CLAIM_VERSION_EQ":
                row = self.connection.execute("SELECT version FROM claims WHERE claim_id = ?", (guard["claim_id"],)).fetchone()
                if not row or row["version"] != guard["expected_version"]:
                    raise TransactionConflict("CLAIM_VERSION_MISMATCH")
            elif guard["op"] == "CONFLICT_STATUS_EQ":
                row = self.connection.execute(
                    "SELECT status FROM conflicts WHERE conflict_id = ?",
                    (guard["conflict_id"],),
                ).fetchone()
                if not row or row["status"] != guard["expected_status"]:
                    raise TransactionConflict("CONFLICT_STATUS_MISMATCH")
            elif guard["op"] == "CONFLICT_MEMBER_DIGEST_EQ":
                row = self.connection.execute(
                    "SELECT member_digest FROM conflicts WHERE conflict_id = ?",
                    (guard["conflict_id"],),
                ).fetchone()
                if not row or row["member_digest"] != guard["expected_digest"]:
                    raise TransactionConflict("CONFLICT_MEMBER_DIGEST_MISMATCH")
            elif guard["op"] == "ACTIVE_SET_DIGEST_EQ":
                if self._active_set_digest(guard["family_key"]) != guard["expected_digest"]:
                    raise TransactionConflict("ACTIVE_SET_DIGEST_MISMATCH")
            elif guard["op"] == "SOURCE_HASH_EQ":
                row = self.connection.execute(
                    "SELECT content_hash FROM sources WHERE revision_id = ?",
                    (guard["source_revision_id"],),
                ).fetchone()
                if not row or row["content_hash"] != guard["expected_hash"]:
                    raise TransactionConflict("SOURCE_HASH_MISMATCH")
            elif guard["op"] == "NO_ACTIVE_CLAIM":
                if self._active_family_members(guard["family_key"]):
                    raise TransactionConflict("ACTIVE_CLAIM_EXISTS")

    def _apply_operation(
        self,
        operation: dict[str, Any],
        events: list[dict[str, Any]],
        conflict_ids: list[str],
    ) -> None:
        proposer = self._current_proposer
        opened_seq = self._current_opened_seq
        kind = operation["op"]
        try:
            if apply_continuity_operation(self.connection, operation, events):
                return
        except ContinuityConflict as exc:
            raise TransactionConflict(exc.code) from exc
        except ContinuityValidationError as exc:
            raise ValidationFailure(exc.code) from exc
        if kind == "REGISTER_SOURCE_REVISION":
            self._register_source_revision(operation["source_revision"], events)
        elif kind == "ASSERT_CLAIM":
            self._assert_claim(
                operation["claim"],
                operation["initial_evidence"],
                events,
                conflict_ids,
                opened_seq=opened_seq,
            )
        elif kind == "ATTACH_EVIDENCE":
            self._attach_evidence(operation["evidence_link"], events)
        elif kind == "SUPERSEDE_CLAIM":
            target = self.connection.execute("SELECT status FROM claims WHERE claim_id = ?", (operation["target_claim_id"],)).fetchone()
            if not target or target["status"] != "ACTIVE":
                raise TransactionConflict("CLAIM_STATUS_MISMATCH")
            self._assert_claim(
                operation["replacement_claim"],
                operation["initial_evidence"],
                events,
                conflict_ids,
                ignored_conflict_claim_ids=frozenset({operation["target_claim_id"]}),
                opened_seq=opened_seq,
            )
            self.connection.execute("UPDATE claims SET status = 'SUPERSEDED', version = version + 1, superseded_by = ? WHERE claim_id = ?", (operation["replacement_claim"]["claim_id"], operation["target_claim_id"]))
            events.append(
                {
                    "event_type": "CLAIM_SUPERSEDED",
                    "target_claim_id": operation["target_claim_id"],
                    "replacement_claim_id": operation["replacement_claim"]["claim_id"],
                    "rationale": operation["rationale"],
                }
            )
        elif kind == "RETRACT_CLAIM":
            self._retract_claim(operation, proposer, events)
        elif kind == "RESOLVE_CONFLICT":
            self._resolve_conflict(operation, proposer, events)
        else:
            raise ValidationFailure("UNSUPPORTED_OPERATION")

    def _register_source_revision(
        self, source: dict[str, Any], events: list[dict[str, Any]]
    ) -> None:
        existing = self.connection.execute(
            "SELECT content_hash, content FROM sources WHERE revision_id = ?",
            (source["revision_id"],),
        ).fetchone()
        if existing and existing["content_hash"] != source["content_hash"]:
            raise ValidationFailure("SOURCE_REVISION_IMMUTABILITY_VIOLATION")
        content = (
            bytes(existing["content"])
            if existing
            else self._content_from_blob_ref(source["blob_ref"])
        )
        if sha256_bytes(content) != source["content_hash"]:
            raise ValidationFailure("SOURCE_CONTENT_HASH_MISMATCH")
        if not existing:
            self.connection.execute(
                "INSERT INTO sources VALUES (?, ?, ?, ?)",
                (
                    source["revision_id"],
                    source["content_hash"],
                    canonical_json(source),
                    content,
                ),
            )
        events.append(
            {"event_type": "SOURCE_REVISION_REGISTERED", "source_revision": source}
        )

    @staticmethod
    def _content_from_blob_ref(blob_ref: str) -> bytes:
        if not blob_ref.startswith("data:") or "," not in blob_ref:
            raise ValidationFailure("SOURCE_CONTENT_UNAVAILABLE")
        header, payload = blob_ref[5:].split(",", 1)
        try:
            if header.endswith(";base64"):
                return base64.b64decode(payload, validate=True)
            return unquote_to_bytes(payload)
        except (binascii.Error, ValueError) as exc:
            raise ValidationFailure("SOURCE_CONTENT_UNAVAILABLE") from exc

    def _retract_claim(
        self,
        operation: dict[str, Any],
        proposer: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        row = self.connection.execute(
            "SELECT status, document FROM claims WHERE claim_id = ?",
            (operation["target_claim_id"],),
        ).fetchone()
        if not row or row["status"] != "ACTIVE":
            raise TransactionConflict("CLAIM_STATUS_MISMATCH")
        asserted_by = json.loads(row["document"])["asserted_by"]
        if not self._actor_is_authorized(proposer, asserted_by):
            raise ValidationFailure("ACTOR_NOT_AUTHORIZED")
        for evidence_link_id in operation["evidence_link_ids"]:
            evidence = self.connection.execute(
                "SELECT claim_id FROM evidence WHERE evidence_link_id = ?",
                (evidence_link_id,),
            ).fetchone()
            if not evidence or evidence["claim_id"] != operation["target_claim_id"]:
                raise ValidationFailure("EVIDENCE_REFERENCE_NOT_FOUND")
        self.connection.execute(
            "UPDATE claims SET status = 'RETRACTED', version = version + 1 "
            "WHERE claim_id = ?",
            (operation["target_claim_id"],),
        )
        events.append(
            {
                "event_type": "CLAIM_RETRACTED",
                "target_claim_id": operation["target_claim_id"],
                "actor": proposer,
                "authority_policy_version": operation["authority_policy_version"],
                "rationale": operation["rationale"],
            }
        )

    def _resolve_conflict(
        self,
        operation: dict[str, Any],
        proposer: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        row = self.connection.execute(
            "SELECT * FROM conflicts WHERE conflict_id = ?",
            (operation["conflict_id"],),
        ).fetchone()
        if not row or row["status"] != "OPEN":
            raise TransactionConflict("CONFLICT_STATUS_MISMATCH")
        if row["member_digest"] != operation["expected_member_digest"]:
            raise TransactionConflict("CONFLICT_MEMBER_DIGEST_MISMATCH")
        resolution = operation["resolution"]
        if resolution["resolution_epoch"] != row["episode"]:
            raise TransactionConflict("CONFLICT_RESOLUTION_EPOCH_MISMATCH")
        if not self._actor_is_authorized(proposer, resolution["resolver"]):
            raise ValidationFailure("ACTOR_NOT_AUTHORIZED")
        members = set(json.loads(row["members"]))
        selected = set(resolution["selected_claim_ids"])
        rejected = set(resolution["rejected_claim_ids"])
        if selected & rejected or selected | rejected != members:
            raise ValidationFailure("INVALID_CONFLICT_RESOLUTION")
        for evidence_link_id in resolution["evidence_link_ids"]:
            evidence = self.connection.execute(
                "SELECT claim_id FROM evidence WHERE evidence_link_id = ?",
                (evidence_link_id,),
            ).fetchone()
            if not evidence or evidence["claim_id"] not in members:
                raise ValidationFailure("EVIDENCE_REFERENCE_NOT_FOUND")
        document = canonical_json(resolution)
        self.connection.execute(
            "UPDATE conflicts SET status = 'RESOLVED', version = version + 1, "
            "resolution = ? WHERE conflict_id = ?",
            (document, operation["conflict_id"]),
        )
        events.append(
            {
                "event_type": "CONFLICT_RESOLVED",
                "conflict_id": operation["conflict_id"],
                "resolution": resolution,
            }
        )

    @staticmethod
    def _actor_is_authorized(actor: dict[str, Any], authority: dict[str, Any]) -> bool:
        return actor["actor_type"] == "HUMAN" or actor["actor_id"] == authority["actor_id"]

    def _assert_claim(
        self,
        claim: dict[str, Any],
        evidence: list[dict[str, Any]],
        events: list[dict[str, Any]],
        conflict_ids: list[str],
        *,
        ignored_conflict_claim_ids: frozenset[str] = frozenset(),
        opened_seq: int,
    ) -> None:
        proposition = claim["proposition"]
        if sha256_json(proposition) != claim["proposition_hash"]:
            raise ValidationFailure("PROPOSITION_HASH_MISMATCH")
        predicate = self.predicates.get(proposition["predicate"])
        if not predicate:
            raise ValidationFailure("UNKNOWN_PREDICATE")
        if proposition["subject"]["entity_type"] not in predicate["subject_types"]:
            raise ValidationFailure("SUBJECT_TYPE_MISMATCH")
        self._validate_predicate_semantics(proposition, predicate, evidence)
        minimum = predicate["evidence_policy"]["minimum_evidence_links"]
        if len(evidence) < minimum:
            raise ValidationFailure("INSUFFICIENT_EVIDENCE")
        self.connection.execute("INSERT INTO claims VALUES (?, ?, ?, ?, 'ACTIVE', 1, NULL)", (claim["claim_id"], claim["proposition_hash"], canonical_json(proposition), canonical_json(claim)))
        for link in evidence:
            self._validate_and_insert_evidence(link, claim["claim_id"])
        events.append(
            {
                "event_type": "CLAIM_ASSERTED",
                "claim": claim,
                "initial_evidence": evidence,
            }
        )
        for conflict_id in self._open_conflicts(
            claim,
            predicate,
            events,
            ignored_conflict_claim_ids,
            opened_seq=opened_seq,
        ):
            conflict_ids.append(conflict_id)

    def _attach_evidence(self, link: dict[str, Any], events: list[dict[str, Any]]) -> None:
        row = self.connection.execute(
            "SELECT status, proposition FROM claims WHERE claim_id = ?",
            (link["claim_id"],),
        ).fetchone()
        if not row or row["status"] != "ACTIVE":
            raise TransactionConflict("CLAIM_STATUS_MISMATCH")
        proposition = json.loads(row["proposition"])
        predicate = self.predicates.get(proposition["predicate"])
        if not predicate:
            raise ValidationFailure("UNKNOWN_PREDICATE")
        self._validate_evidence_interpretations(predicate, [link])
        self._validate_and_insert_evidence(link, link["claim_id"])
        self.connection.execute("UPDATE claims SET version = version + 1 WHERE claim_id = ?", (link["claim_id"],))
        events.append({"event_type": "EVIDENCE_ATTACHED", "evidence_link": link})

    def _validate_and_insert_evidence(self, link: dict[str, Any], claim_id: str) -> None:
        if link["claim_id"] != claim_id:
            raise ValidationFailure("EVIDENCE_CLAIM_MISMATCH")
        source = self.connection.execute("SELECT content FROM sources WHERE revision_id = ?", (link["source_revision_id"],)).fetchone()
        if not source:
            raise ValidationFailure("SOURCE_REVISION_NOT_FOUND")
        selector = link["selector"]
        excerpt = bytes(source["content"])[selector["start_byte"]:selector["end_byte"]]
        if excerpt.decode("utf-8") != selector["excerpt"] or sha256_bytes(excerpt) != selector["excerpt_hash"]:
            raise ValidationFailure("EVIDENCE_SELECTOR_MISMATCH")
        self.connection.execute("INSERT INTO evidence VALUES (?, ?, ?, ?)", (link["evidence_link_id"], claim_id, link["source_revision_id"], canonical_json(link)))

    def _open_conflicts(
        self,
        incoming: dict[str, Any],
        predicate: dict[str, Any],
        events: list[dict[str, Any]],
        ignored_claim_ids: frozenset[str] = frozenset(),
        *,
        opened_seq: int,
    ) -> list[str]:
        opened: list[str] = []
        p = incoming["proposition"]
        family = self._family_key(p, predicate)
        rows = self.connection.execute("SELECT claim_id, proposition FROM claims WHERE status = 'ACTIVE' AND claim_id <> ?", (incoming["claim_id"],)).fetchall()
        members_by_kind: dict[str, set[str]] = {}
        for row in rows:
            if row["claim_id"] in ignored_claim_ids:
                continue
            other = json.loads(row["proposition"])
            if self._family_key(other, predicate) != family or not self._overlaps(p["valid_time"], other["valid_time"]):
                continue
            kind = None
            if p["object"] == other["object"] and p["polarity"] != other["polarity"]:
                kind = "POLARITY_CONFLICT"
            elif predicate["cardinality"] == "ONE" and p["polarity"] == other["polarity"] == "POSITIVE" and p["object"] != other["object"]:
                kind = "EXCLUSIVE_VALUE_CONFLICT"
            if not kind:
                continue
            members_by_kind.setdefault(kind, {incoming["claim_id"]}).add(row["claim_id"])

        for kind, new_members in sorted(members_by_kind.items()):
            current = self.connection.execute(
                "SELECT * FROM conflicts WHERE family_key = ? AND kind = ? "
                "AND status = 'OPEN' ORDER BY conflict_id LIMIT 1",
                (family, kind),
            ).fetchone()
            if current:
                members = sorted(set(json.loads(current["members"])) | new_members)
                member_digest = sha256_json(members)
                self.connection.execute(
                    "UPDATE conflicts SET members = ?, member_digest = ?, "
                    "version = version + 1 WHERE conflict_id = ?",
                    (canonical_json(members), member_digest, current["conflict_id"]),
                )
                conflict_id = current["conflict_id"]
            else:
                current = self.connection.execute(
                    "SELECT * FROM conflicts WHERE family_key = ? AND kind = ? "
                    "AND status = 'RESOLVED' ORDER BY episode DESC, conflict_id LIMIT 1",
                    (family, kind),
                ).fetchone()
                members = sorted(new_members)
                member_digest = sha256_json(members)
                if current:
                    conflict_id = current["conflict_id"]
                    self.connection.execute(
                        "UPDATE conflicts SET members = ?, member_digest = ?, "
                        "status = 'OPEN', episode = episode + 1, version = version + 1, "
                        "resolution = NULL, opened_seq = ? WHERE conflict_id = ?",
                        (
                            canonical_json(members),
                            member_digest,
                            opened_seq,
                            conflict_id,
                        ),
                    )
                else:
                    conflict_id = "conflict_" + member_digest.split(":", 1)[1][:24]
                    self.connection.execute(
                        """INSERT INTO conflicts(
                             conflict_id, family_key, kind, member_digest, members,
                             status, episode, version, resolution, opened_seq
                           ) VALUES (?, ?, ?, ?, ?, 'OPEN', 1, 1, NULL, ?)""",
                        (
                            conflict_id,
                            family,
                            kind,
                            member_digest,
                            canonical_json(members),
                            opened_seq,
                        ),
                    )
            conflict = self.connection.execute(
                "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
            ).fetchone()
            events.append(
                {"event_type": "CONFLICT_OPENED", "conflict": self._conflict_document(conflict)}
            )
            opened.append(conflict_id)
        return opened

    @staticmethod
    def _conflict_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "object_type": "CONFLICT",
            "conflict_id": row["conflict_id"],
            "family_key": row["family_key"],
            "episode": row["episode"],
            "kind": row["kind"],
            "member_claim_ids": json.loads(row["members"]),
            "member_digest": row["member_digest"],
            "status": row["status"],
            "opened_seq": row["opened_seq"],
            "resolution": json.loads(row["resolution"]) if row["resolution"] else None,
        }

    def _active_family_members(self, family_key: str) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        for row in self.connection.execute(
            "SELECT claim_id, proposition_hash, proposition, version "
            "FROM claims WHERE status = 'ACTIVE' ORDER BY claim_id"
        ):
            proposition = json.loads(row["proposition"])
            predicate = self.predicates.get(proposition["predicate"])
            if predicate and self._family_key(proposition, predicate) == family_key:
                members.append(
                    {
                        "claim_id": row["claim_id"],
                        "proposition_hash": row["proposition_hash"],
                        "version": row["version"],
                    }
                )
        return members

    def _active_set_digest(self, family_key: str) -> str:
        return sha256_json(self._active_family_members(family_key))

    def _family_key(self, proposition: dict[str, Any], predicate: dict[str, Any]) -> str:
        values = []
        for path in predicate["family_key_fields"]:
            value: Any = proposition
            for part in path.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            values.append(value)
        return sha256_json(values)

    def _validate_predicate_semantics(
        self,
        proposition: dict[str, Any],
        predicate: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> None:
        expected_object = predicate["object"]
        actual_object = proposition["object"]
        if actual_object["kind"] != expected_object["kind"]:
            raise ValidationFailure("OBJECT_KIND_MISMATCH")
        if expected_object["kind"] == "entity":
            if actual_object["entity_type"] not in expected_object["entity_types"]:
                raise ValidationFailure("OBJECT_ENTITY_TYPE_MISMATCH")
        elif actual_object["value"] not in expected_object["enum_values"]:
            raise ValidationFailure("OBJECT_ENUM_VALUE_NOT_ALLOWED")

        scope = proposition["scope"]
        allowed_fields = set(predicate["scope"]["allowed_fields"])
        if any(value is not None and field not in allowed_fields for field, value in scope.items()):
            raise ValidationFailure("SCOPE_FIELD_NOT_ALLOWED")
        for field in predicate["scope"]["required_fields"]:
            if scope.get(field) is None:
                raise ValidationFailure("REQUIRED_SCOPE_MISSING")

        valid_time = proposition["valid_time"]
        start = self._parse_time(valid_time["from"])
        end = self._parse_time(valid_time["to"])
        if predicate["temporal"] == "REQUIRED" and start is None:
            raise ValidationFailure("VALID_TIME_REQUIRED")
        if start is not None and end is not None and start >= end:
            raise ValidationFailure("INVALID_VALID_TIME_RANGE")
        self._validate_evidence_interpretations(predicate, evidence)

    @staticmethod
    def _validate_evidence_interpretations(
        predicate: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> None:
        allowed = set(predicate["evidence_policy"]["allowed_interpretations"])
        if any(link["interpretation"] not in allowed for link in evidence):
            raise ValidationFailure("EVIDENCE_INTERPRETATION_NOT_ALLOWED")

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    @staticmethod
    def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
        minimum = datetime.min.replace(tzinfo=timezone.utc)
        maximum = datetime.max.replace(tzinfo=timezone.utc)
        left_start = Kernel._parse_time(left["from"]) or minimum
        right_start = Kernel._parse_time(right["from"]) or minimum
        start = max(left_start, right_start)
        left_end = Kernel._parse_time(left["to"]) or maximum
        right_end = Kernel._parse_time(right["to"]) or maximum
        return start < min(left_end, right_end)

    def _next_ledger_seq(self) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM ledger"
            ).fetchone()[0]
        )

    @staticmethod
    def _ledger_envelope(
        *,
        seq: int,
        prev_hash: str | None,
        proposal_hash: str,
        pre_state_root: str,
        post_state_root: str,
        versions: dict[str, Any],
        events: list[dict[str, Any]],
        committed_at: str,
    ) -> dict[str, Any]:
        return {
            "seq": seq,
            "prev_hash": prev_hash,
            "proposal_hash": proposal_hash,
            "pre_state_root": pre_state_root,
            "post_state_root": post_state_root,
            "versions": versions,
            "events": events,
            "committed_at": committed_at,
        }

    def verify_ledger(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        errors = self._verify_ledger_rows(rows)
        head_hash = rows[-1]["entry_hash"] if rows else None
        current_root = self.state_root()
        if rows and rows[-1]["state_root"] != current_root:
            errors.append(f"MATERIALIZED_STATE_ROOT_MISMATCH:{rows[-1]['seq']}")
        if not errors and rows:
            replayed: Kernel | None = None
            try:
                replayed = self.replay(":memory:")
                if replayed.state_root() != current_root:
                    errors.append("REPLAY_STATE_ROOT_MISMATCH")
            except ValidationFailure as exc:
                errors.append(exc.code)
            finally:
                if replayed is not None:
                    replayed.close()
        return {
            "valid": not errors,
            "checked_entries": len(rows),
            "head_hash": head_hash,
            "state_root": current_root,
            "errors": tuple(errors),
        }

    def _verify_ledger_rows(self, rows: list[sqlite3.Row]) -> list[str]:
        errors: list[str] = []
        expected_prev: str | None = None
        for expected_seq, row in enumerate(rows, start=1):
            seq = int(row["seq"])
            if seq != expected_seq:
                errors.append(f"LEDGER_SEQUENCE_GAP:{seq}")
            if row["prev_hash"] != expected_prev:
                errors.append(f"PREVIOUS_HASH_MISMATCH:{seq}")
            try:
                proposal = json.loads(row["proposal"])
                events = json.loads(row["events"])
                if sha256_json(proposal) != row["proposal_hash"]:
                    errors.append(f"PROPOSAL_HASH_MISMATCH:{seq}")
                if not row["pre_state_root"] or not row["committed_at"]:
                    errors.append(f"LEGACY_LEDGER_ENTRY_NOT_REPLAYABLE:{seq}")
                else:
                    envelope = self._ledger_envelope(
                        seq=seq,
                        prev_hash=row["prev_hash"],
                        proposal_hash=row["proposal_hash"],
                        pre_state_root=row["pre_state_root"],
                        post_state_root=row["state_root"],
                        versions=proposal["versions"],
                        events=events,
                        committed_at=row["committed_at"],
                    )
                    if sha256_json(envelope) != row["entry_hash"]:
                        errors.append(f"ENTRY_HASH_MISMATCH:{seq}")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                errors.append(f"MALFORMED_LEDGER_ENTRY:{seq}")
            expected_prev = row["entry_hash"]
        return errors

    def replay(self, database: str | Path) -> Kernel:
        if str(database) == self.database and str(database) != ":memory:":
            raise ValidationFailure("REPLAY_TARGET_MUST_DIFFER")
        rows = self.connection.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        errors = self._verify_ledger_rows(rows)
        if errors:
            raise ValidationFailure(errors[0])
        target = Kernel(database, copy.deepcopy(self.registry))
        try:
            if any(
                target.connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in (
                    "sources",
                    "claims",
                    "evidence",
                    "conflicts",
                    "decision_records",
                    "open_questions",
                    "work_items",
                    "ledger",
                )
            ):
                raise ValidationFailure("REPLAY_TARGET_NOT_EMPTY")
            expected_prev: str | None = None
            for row in rows:
                target.connection.execute("BEGIN IMMEDIATE")
                if target.state_root() != row["pre_state_root"]:
                    raise ValidationFailure(f"REPLAY_PRE_STATE_ROOT_MISMATCH:{row['seq']}")
                events = json.loads(row["events"])
                for event in events:
                    target._apply_replay_event(event)
                if target.state_root() != row["state_root"]:
                    raise ValidationFailure(f"REPLAY_POST_STATE_ROOT_MISMATCH:{row['seq']}")
                if row["prev_hash"] != expected_prev:
                    raise ValidationFailure(f"PREVIOUS_HASH_MISMATCH:{row['seq']}")
                target.connection.execute(
                    """INSERT INTO ledger(
                         seq, prev_hash, entry_hash, proposal_hash, proposal, events,
                         pre_state_root, state_root, committed_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["seq"],
                        row["prev_hash"],
                        row["entry_hash"],
                        row["proposal_hash"],
                        row["proposal"],
                        row["events"],
                        row["pre_state_root"],
                        row["state_root"],
                        row["committed_at"],
                    ),
                )
                proposal = json.loads(row["proposal"])
                conflict_ids = tuple(
                    sorted(
                        {
                            event["conflict"]["conflict_id"]
                            for event in events
                            if event.get("event_type") == "CONFLICT_OPENED"
                        }
                    )
                )
                target._insert_receipt(
                    proposal["idempotency_key"],
                    row["proposal_hash"],
                    proposal["proposal_id"],
                    "FACT_CONFLICT" if conflict_ids else "COMMITTED",
                    (),
                    row["seq"],
                    row["state_root"],
                    conflict_ids,
                )
                target.connection.execute("COMMIT")
                expected_prev = row["entry_hash"]
            return target
        except Exception:
            target._rollback_if_needed()
            target.close()
            raise

    def _apply_replay_event(self, event: dict[str, Any]) -> None:
        try:
            if apply_continuity_event(self.connection, event):
                return
        except (ContinuityConflict, ContinuityValidationError) as exc:
            raise ValidationFailure(exc.code) from exc
        event_type = event.get("event_type")
        if event_type == "SOURCE_REVISION_REGISTERED":
            self._register_source_revision(event["source_revision"], [])
        elif event_type == "CLAIM_ASSERTED":
            self._replay_assert_claim(event["claim"], event["initial_evidence"])
        elif event_type == "EVIDENCE_ATTACHED":
            self._attach_evidence(event["evidence_link"], [])
        elif event_type == "CLAIM_SUPERSEDED":
            cursor = self.connection.execute(
                "UPDATE claims SET status = 'SUPERSEDED', version = version + 1, "
                "superseded_by = ? WHERE claim_id = ? AND status = 'ACTIVE'",
                (event["replacement_claim_id"], event["target_claim_id"]),
            )
            if cursor.rowcount != 1:
                raise ValidationFailure("REPLAY_EVENT_PRECONDITION_FAILED")
        elif event_type == "CLAIM_RETRACTED":
            cursor = self.connection.execute(
                "UPDATE claims SET status = 'RETRACTED', version = version + 1 "
                "WHERE claim_id = ? AND status = 'ACTIVE'",
                (event["target_claim_id"],),
            )
            if cursor.rowcount != 1:
                raise ValidationFailure("REPLAY_EVENT_PRECONDITION_FAILED")
        elif event_type == "CONFLICT_OPENED":
            self._replay_open_conflict(event["conflict"])
        elif event_type == "CONFLICT_RESOLVED":
            cursor = self.connection.execute(
                "UPDATE conflicts SET status = 'RESOLVED', version = version + 1, "
                "resolution = ? WHERE conflict_id = ? AND status = 'OPEN'",
                (canonical_json(event["resolution"]), event["conflict_id"]),
            )
            if cursor.rowcount != 1:
                raise ValidationFailure("REPLAY_EVENT_PRECONDITION_FAILED")
        else:
            raise ValidationFailure("UNSUPPORTED_LEDGER_EVENT")

    def _replay_assert_claim(
        self, claim: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> None:
        proposition = claim["proposition"]
        if sha256_json(proposition) != claim["proposition_hash"]:
            raise ValidationFailure("PROPOSITION_HASH_MISMATCH")
        predicate = self.predicates.get(proposition["predicate"])
        if not predicate:
            raise ValidationFailure("UNKNOWN_PREDICATE")
        if proposition["subject"]["entity_type"] not in predicate["subject_types"]:
            raise ValidationFailure("SUBJECT_TYPE_MISMATCH")
        self._validate_predicate_semantics(proposition, predicate, evidence)
        if len(evidence) < predicate["evidence_policy"]["minimum_evidence_links"]:
            raise ValidationFailure("INSUFFICIENT_EVIDENCE")
        self.connection.execute(
            "INSERT INTO claims VALUES (?, ?, ?, ?, 'ACTIVE', 1, NULL)",
            (
                claim["claim_id"],
                claim["proposition_hash"],
                canonical_json(proposition),
                canonical_json(claim),
            ),
        )
        for link in evidence:
            self._validate_and_insert_evidence(link, claim["claim_id"])

    def _replay_open_conflict(self, conflict: dict[str, Any]) -> None:
        existing = self.connection.execute(
            "SELECT version FROM conflicts WHERE conflict_id = ?",
            (conflict["conflict_id"],),
        ).fetchone()
        version = existing["version"] + 1 if existing else 1
        values = (
            conflict["family_key"],
            conflict["kind"],
            conflict["member_digest"],
            canonical_json(conflict["member_claim_ids"]),
            conflict["status"],
            conflict["episode"],
            version,
            canonical_json(conflict["resolution"]) if conflict["resolution"] else None,
            conflict["opened_seq"],
            conflict["conflict_id"],
        )
        if existing:
            self.connection.execute(
                """UPDATE conflicts SET
                     family_key = ?, kind = ?, member_digest = ?, members = ?,
                     status = ?, episode = ?, version = ?, resolution = ?, opened_seq = ?
                   WHERE conflict_id = ?""",
                values,
            )
        else:
            self.connection.execute(
                """INSERT INTO conflicts(
                     family_key, kind, member_digest, members, status, episode,
                     version, resolution, opened_seq, conflict_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )

    def state_root(self) -> str:
        state: dict[str, list[Any]] = {}
        for table in ("sources", "claims", "evidence", "conflicts"):
            rows = self.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            state[table] = [dict(row) | ({"content": sha256_bytes(bytes(row["content"]))} if table == "sources" else {}) for row in rows]
        state.update(continuity_state_rows(self.connection))
        return sha256_json(state)

    def read_epistemic_context(self, subject_id: str, predicate_key: str, environment: str) -> dict[str, Any]:
        claims = []
        for row in self.connection.execute("SELECT * FROM claims WHERE status = 'ACTIVE' ORDER BY claim_id"):
            proposition = json.loads(row["proposition"])
            if proposition["subject"]["entity_id"] == subject_id and proposition["predicate"] == predicate_key and proposition["scope"]["environment"] == environment:
                evidence = [json.loads(item["document"]) for item in self.connection.execute("SELECT document FROM evidence WHERE claim_id = ? ORDER BY evidence_link_id", (row["claim_id"],))]
                claims.append({"claim": json.loads(row["document"]), "status": row["status"], "version": row["version"], "evidence": evidence})
        claim_ids = {item["claim"]["claim_id"] for item in claims}
        conflicts = [dict(row) | {"members": json.loads(row["members"])} for row in self.connection.execute("SELECT * FROM conflicts WHERE status = 'OPEN' ORDER BY conflict_id") if claim_ids.intersection(json.loads(row["members"]))]
        head = self.connection.execute("SELECT seq FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
        return {"ledger_seq": head["seq"] if head else 0, "state_root": self.state_root(), "claims": claims, "conflicts": conflicts, "has_open_conflict": bool(conflicts)}

    @staticmethod
    def _row_to_receipt(row: sqlite3.Row) -> Receipt:
        return Receipt(row["proposal_id"], row["outcome"], tuple(json.loads(row["reason_codes"])), row["ledger_seq"], row["state_root"], tuple(json.loads(row["conflict_ids"])))

    def _rollback_if_needed(self) -> None:
        if self.connection.in_transaction:
            self.connection.execute("ROLLBACK")

    @staticmethod
    def _integrity_reason(error: sqlite3.IntegrityError) -> str:
        message = str(error)
        if "UNIQUE constraint failed" in message:
            return "DUPLICATE_OBJECT_ID"
        if "FOREIGN KEY constraint failed" in message:
            return "REFERENCE_NOT_FOUND"
        return "INTEGRITY_CONSTRAINT_VIOLATION"
