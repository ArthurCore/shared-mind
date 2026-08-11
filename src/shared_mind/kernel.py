from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_bytes, sha256_json


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

    def __init__(self, database: str | Path, registry: dict[str, Any]):
        self.database = str(database)
        self.registry = registry
        self.predicates = {item["key"]: item for item in registry["predicates"]}
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
              episode INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              prev_hash TEXT,
              entry_hash TEXT NOT NULL UNIQUE,
              proposal_hash TEXT NOT NULL,
              proposal TEXT NOT NULL,
              events TEXT NOT NULL,
              state_root TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
              idempotency_key TEXT PRIMARY KEY,
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

    def register_source(self, source: dict[str, Any], content: bytes) -> None:
        if sha256_bytes(content) != source["content_hash"]:
            raise ValidationFailure("SOURCE_CONTENT_HASH_MISMATCH")
        document = canonical_json(source)
        with self.connection:
            row = self.connection.execute(
                "SELECT content_hash FROM sources WHERE revision_id = ?", (source["revision_id"],)
            ).fetchone()
            if row and row["content_hash"] != source["content_hash"]:
                raise ValidationFailure("SOURCE_REVISION_IMMUTABILITY_VIOLATION")
            self.connection.execute(
                "INSERT OR IGNORE INTO sources VALUES (?, ?, ?, ?)",
                (source["revision_id"], source["content_hash"], document, content),
            )

    def commit(self, proposal: dict[str, Any]) -> Receipt:
        proposal_hash = sha256_json(proposal)
        key = proposal.get("idempotency_key", "")
        prior = self.connection.execute("SELECT * FROM receipts WHERE idempotency_key = ?", (key,)).fetchone()
        if prior:
            if prior["proposal_hash"] != proposal_hash:
                return Receipt(proposal.get("proposal_id", ""), "VALIDATION_ERROR", ("IDEMPOTENCY_KEY_REUSE",), None, self.state_root())
            return self._row_to_receipt(prior)

        outcome = "COMMITTED"
        reasons: tuple[str, ...] = ()
        conflict_ids: tuple[str, ...] = ()
        ledger_seq: int | None = None
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._validate_versions(proposal)
            self._validate_reads_and_guards(proposal)
            events: list[dict[str, Any]] = []
            new_conflicts: list[str] = []
            for operation in proposal["operations"]:
                self._apply_operation(operation, events, new_conflicts)
            conflict_ids = tuple(sorted(set(new_conflicts)))
            if conflict_ids:
                outcome = "FACT_CONFLICT"
            post_root = self.state_root()
            previous = self.connection.execute("SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            envelope = {
                "prev_hash": previous["entry_hash"] if previous else None,
                "proposal_hash": proposal_hash,
                "events": events,
                "state_root": post_root,
            }
            entry_hash = sha256_json(envelope)
            cursor = self.connection.execute(
                "INSERT INTO ledger(prev_hash, entry_hash, proposal_hash, proposal, events, state_root) VALUES (?, ?, ?, ?, ?, ?)",
                (envelope["prev_hash"], entry_hash, proposal_hash, canonical_json(proposal), canonical_json(events), post_root),
            )
            ledger_seq = int(cursor.lastrowid)
            self._insert_receipt(key, proposal_hash, proposal["proposal_id"], outcome, (), ledger_seq, post_root, conflict_ids)
            self.connection.execute("COMMIT")
        except TransactionConflict as exc:
            self.connection.execute("ROLLBACK")
            outcome, reasons = "TRANSACTION_CONFLICT", (exc.code,)
            post_root = self.state_root()
            self._insert_receipt(key, proposal_hash, proposal.get("proposal_id", ""), outcome, reasons, None, post_root, ())
        except (ValidationFailure, KeyError, TypeError, ValueError) as exc:
            self.connection.execute("ROLLBACK")
            code = exc.code if isinstance(exc, ValidationFailure) else "MALFORMED_PROPOSAL"
            outcome, reasons = "VALIDATION_ERROR", (code,)
            post_root = self.state_root()
            self._insert_receipt(key, proposal_hash, proposal.get("proposal_id", ""), outcome, reasons, None, post_root, ())
        return Receipt(proposal.get("proposal_id", ""), outcome, reasons, ledger_seq, post_root, conflict_ids)

    def _insert_receipt(self, key: str, proposal_hash: str, proposal_id: str, outcome: str, reasons: tuple[str, ...], ledger_seq: int | None, root: str, conflicts: tuple[str, ...]) -> None:
        # The caller owns the transaction. On accepted mutations this write must
        # be atomic with ledger and materialized-state updates; rejected writes
        # run in SQLite autocommit mode after the mutation transaction rolls back.
        self.connection.execute(
            "INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, proposal_hash, proposal_id, outcome, canonical_json(reasons), ledger_seq, root, canonical_json(conflicts)),
        )

    def _validate_versions(self, proposal: dict[str, Any]) -> None:
        versions = proposal["versions"]
        if versions["predicate_registry"] != self.registry["version"]:
            raise ValidationFailure("UNSUPPORTED_PREDICATE_REGISTRY")

    def _validate_reads_and_guards(self, proposal: dict[str, Any]) -> None:
        for read in proposal.get("reads", []):
            if read["kind"] == "AGGREGATE" and read["aggregate_type"] == "CLAIM":
                row = self.connection.execute("SELECT version FROM claims WHERE claim_id = ?", (read["aggregate_id"],)).fetchone()
                if not row or row["version"] != read["expected_version"]:
                    raise TransactionConflict("CLAIM_VERSION_MISMATCH")
        for guard in proposal.get("guards", []):
            if guard["op"] == "CLAIM_STATUS_EQ":
                row = self.connection.execute("SELECT status FROM claims WHERE claim_id = ?", (guard["claim_id"],)).fetchone()
                if not row or row["status"] != guard["expected_status"]:
                    raise TransactionConflict("CLAIM_STATUS_MISMATCH")
            elif guard["op"] == "CLAIM_VERSION_EQ":
                row = self.connection.execute("SELECT version FROM claims WHERE claim_id = ?", (guard["claim_id"],)).fetchone()
                if not row or row["version"] != guard["expected_version"]:
                    raise TransactionConflict("CLAIM_VERSION_MISMATCH")

    def _apply_operation(self, operation: dict[str, Any], events: list[dict[str, Any]], conflict_ids: list[str]) -> None:
        kind = operation["op"]
        if kind == "ASSERT_CLAIM":
            self._assert_claim(operation["claim"], operation["initial_evidence"], events, conflict_ids)
        elif kind == "ATTACH_EVIDENCE":
            self._attach_evidence(operation["evidence_link"], events)
        elif kind == "SUPERSEDE_CLAIM":
            target = self.connection.execute("SELECT status FROM claims WHERE claim_id = ?", (operation["target_claim_id"],)).fetchone()
            if not target or target["status"] != "ACTIVE":
                raise TransactionConflict("CLAIM_STATUS_MISMATCH")
            self._assert_claim(operation["replacement_claim"], operation["initial_evidence"], events, conflict_ids)
            self.connection.execute("UPDATE claims SET status = 'SUPERSEDED', version = version + 1, superseded_by = ? WHERE claim_id = ?", (operation["replacement_claim"]["claim_id"], operation["target_claim_id"]))
            events.append({"type": "CLAIM_SUPERSEDED", "claim_id": operation["target_claim_id"], "replacement_claim_id": operation["replacement_claim"]["claim_id"]})
        else:
            raise ValidationFailure("UNSUPPORTED_OPERATION")

    def _assert_claim(self, claim: dict[str, Any], evidence: list[dict[str, Any]], events: list[dict[str, Any]], conflict_ids: list[str]) -> None:
        proposition = claim["proposition"]
        if sha256_json(proposition) != claim["proposition_hash"]:
            raise ValidationFailure("PROPOSITION_HASH_MISMATCH")
        predicate = self.predicates.get(proposition["predicate"])
        if not predicate:
            raise ValidationFailure("UNKNOWN_PREDICATE")
        if proposition["subject"]["entity_type"] not in predicate["subject_types"]:
            raise ValidationFailure("SUBJECT_TYPE_MISMATCH")
        for field in predicate["scope"]["required_fields"]:
            if proposition["scope"].get(field) is None:
                raise ValidationFailure("REQUIRED_SCOPE_MISSING")
        minimum = predicate["evidence_policy"]["minimum_evidence_links"]
        if len(evidence) < minimum:
            raise ValidationFailure("INSUFFICIENT_EVIDENCE")
        self.connection.execute("INSERT INTO claims VALUES (?, ?, ?, ?, 'ACTIVE', 1, NULL)", (claim["claim_id"], claim["proposition_hash"], canonical_json(proposition), canonical_json(claim)))
        for link in evidence:
            self._validate_and_insert_evidence(link, claim["claim_id"])
        events.append({"type": "CLAIM_ASSERTED", "claim_id": claim["claim_id"]})
        for conflict_id in self._open_conflicts(claim, predicate, events):
            conflict_ids.append(conflict_id)

    def _attach_evidence(self, link: dict[str, Any], events: list[dict[str, Any]]) -> None:
        row = self.connection.execute("SELECT status FROM claims WHERE claim_id = ?", (link["claim_id"],)).fetchone()
        if not row or row["status"] != "ACTIVE":
            raise TransactionConflict("CLAIM_STATUS_MISMATCH")
        self._validate_and_insert_evidence(link, link["claim_id"])
        self.connection.execute("UPDATE claims SET version = version + 1 WHERE claim_id = ?", (link["claim_id"],))
        events.append({"type": "EVIDENCE_ATTACHED", "evidence_link_id": link["evidence_link_id"], "claim_id": link["claim_id"]})

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

    def _open_conflicts(self, incoming: dict[str, Any], predicate: dict[str, Any], events: list[dict[str, Any]]) -> list[str]:
        opened: list[str] = []
        p = incoming["proposition"]
        family = self._family_key(p, predicate)
        rows = self.connection.execute("SELECT claim_id, proposition FROM claims WHERE status = 'ACTIVE' AND claim_id <> ?", (incoming["claim_id"],)).fetchall()
        for row in rows:
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
            members = sorted([incoming["claim_id"], row["claim_id"]])
            member_digest = sha256_json(members)
            conflict_id = "conflict_" + member_digest.split(":", 1)[1][:24]
            exists = self.connection.execute("SELECT 1 FROM conflicts WHERE conflict_id = ?", (conflict_id,)).fetchone()
            if not exists:
                self.connection.execute("INSERT INTO conflicts VALUES (?, ?, ?, ?, ?, 'OPEN', 1)", (conflict_id, family, kind, member_digest, canonical_json(members)))
                events.append({"type": "CONFLICT_OPENED", "conflict_id": conflict_id, "kind": kind, "members": members})
                opened.append(conflict_id)
        return opened

    def _family_key(self, proposition: dict[str, Any], predicate: dict[str, Any]) -> str:
        values = []
        for path in predicate["family_key_fields"]:
            value: Any = proposition
            for part in path.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            values.append(value)
        return sha256_json(values)

    @staticmethod
    def _overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
        start = max(datetime.fromisoformat(left["from"].replace("Z", "+00:00")), datetime.fromisoformat(right["from"].replace("Z", "+00:00")))
        left_end = datetime.max.replace(tzinfo=start.tzinfo) if left["to"] is None else datetime.fromisoformat(left["to"].replace("Z", "+00:00"))
        right_end = datetime.max.replace(tzinfo=start.tzinfo) if right["to"] is None else datetime.fromisoformat(right["to"].replace("Z", "+00:00"))
        return start < min(left_end, right_end)

    def state_root(self) -> str:
        state: dict[str, list[Any]] = {}
        for table in ("sources", "claims", "evidence", "conflicts"):
            rows = self.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            state[table] = [dict(row) | ({"content": sha256_bytes(bytes(row["content"]))} if table == "sources" else {}) for row in rows]
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
