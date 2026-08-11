from __future__ import annotations

import copy
import hashlib
import importlib
import json
import unittest
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = (
    ROOT / "tests" / "fixtures" / "remote_policy" / "atlas-remote-policy.v1.json"
)


class RemotePolicyCompilationValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with POLICY_PATH.open("r", encoding="utf-8") as handle:
            cls.policy = json.load(handle)
        cls.module = importlib.import_module("shared_mind.remote_policy")

    def test_trust_bindings_require_complete_non_empty_string_identity(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "missing issuer prevents None-equals-None matching": lambda document: document[
                "trust_bindings"
            ][0].pop("issuer"),
            "null issuer prevents None-equals-None matching": lambda document: document[
                "trust_bindings"
            ][0].__setitem__("issuer", None),
            "empty subject": lambda document: document["trust_bindings"][
                0
            ].__setitem__("subject", ""),
            "non-string actor": lambda document: document["trust_bindings"][
                0
            ].__setitem__("actor_id", ["service:atlas-sync"]),
            "malformed entry is not silently discarded": lambda document: document[
                "trust_bindings"
            ].append(None),
        }

        for name, mutate in mutations.items():
            with self.subTest(case=name):
                self._assert_rejected_deterministically(mutate)

    def test_every_nested_collection_has_an_exact_shape(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "source label entry must be an object": lambda document: document[
                "source_labels"
            ].append("unexpected"),
            "data classes must be a list": lambda document: document[
                "source_labels"
            ][0].__setitem__("data_classes", "PUBLIC"),
            "data class entries must be strings": lambda document: document[
                "source_labels"
            ][0]["data_classes"].append({"class": "PUBLIC"}),
            "capability entry must be an object": lambda document: document[
                "capability_scopes"
            ].append([]),
            "operation types must be a list": lambda document: document[
                "capability_scopes"
            ][0].__setitem__("operation_types", "READ_SOURCE"),
            "actor ids must contain only strings": lambda document: document[
                "capability_scopes"
            ][0]["actor_ids"].append(None),
            "source roots must be a list": lambda document: document[
                "capability_scopes"
            ][0].__setitem__("source_roots", {"source://atlas/public/": True}),
            "sensitivities must be a list": lambda document: document[
                "capability_scopes"
            ][0].__setitem__("allowed_sensitivities", None),
            "disclosure must be an object": lambda document: document[
                "capability_scopes"
            ][0].__setitem__("disclosure", []),
            "allow fields must be a list": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"].__setitem__("allow_fields", {"content": True}),
            "redaction paths must be a list": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"].__setitem__("redact_paths", "content.credentials"),
            "tuple is not a JSON list": lambda document: document[
                "capability_scopes"
            ][0].__setitem__("operation_types", ("READ_SOURCE",)),
            "set is not a JSON list": lambda document: document[
                "capability_scopes"
            ][0].__setitem__("operation_types", {"READ_SOURCE"}),
            "JSON object keys must be strings": lambda document: document[
                "endpoint_pin"
            ].__setitem__(1, "unexpected"),
            "non-finite numbers are not canonical JSON": lambda document: document[
                "endpoint_pin"
            ].__setitem__("weight", float("nan")),
        }

        for name, mutate in mutations.items():
            with self.subTest(case=name):
                self._assert_rejected_deterministically(mutate)

    def test_source_roots_must_be_unambiguous_absolute_prefixes(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "empty label root would match every source": lambda document: document[
                "source_labels"
            ][0].__setitem__("source_root", ""),
            "authority-less label root": lambda document: document[
                "source_labels"
            ][0].__setitem__("source_root", "source://"),
            "unterminated label root has a prefix collision": lambda document: document[
                "source_labels"
            ][0].__setitem__("source_root", "source://atlas/public"),
            "dot-segment label root": lambda document: document["source_labels"][
                0
            ].__setitem__("source_root", "source://atlas/public/../internal/"),
            "repeatedly encoded dot-segment root": lambda document: _replace_source_root(
                document,
                "source://atlas/public/",
                "source://atlas/public/%252e%252e/internal/",
            ),
            "empty capability root would authorize every source": lambda document: document[
                "capability_scopes"
            ][0]["source_roots"].__setitem__(0, ""),
            "unterminated capability root has a prefix collision": lambda document: document[
                "capability_scopes"
            ][0]["source_roots"].__setitem__(0, "source://atlas/public"),
            "duplicate label roots are ambiguous": lambda document: document[
                "source_labels"
            ].append(copy.deepcopy(document["source_labels"][0])),
            "scope root must resolve to a configured label": lambda document: document[
                "capability_scopes"
            ][0]["source_roots"].append("source://atlas/unlabeled/"),
        }

        for name, mutate in mutations.items():
            with self.subTest(case=name):
                self._assert_rejected_deterministically(mutate)

    def test_nested_source_labels_remain_a_valid_most_specific_match(self) -> None:
        compiled = self.module.compile_policy(copy.deepcopy(self.policy))

        self.assertEqual(
            "sha256:5001b233cd456bd847741824288f4aa310a92bcef838e096edede7a7fdc58e82",
            compiled.policy_hash,
        )

    def test_repeatedly_encoded_source_escape_is_denied_before_labeling(self) -> None:
        compiled = self.module.compile_policy(copy.deepcopy(self.policy))
        decision = self.module.evaluate_request(
            compiled,
            {
                "request_version": "remote-policy-request@1",
                "request_id": "remote_request_encoded_escape_001",
                "claimed_actor_id": "service:atlas-sync",
                "endpoint_id": "remote_atlas_001",
                "protocol_version": "shared-mind-remote@1",
                "adapter_version": "atlas-adapter@3",
                "registry_version": "predicate-registry@1",
                "capability": "source.read",
                "operation_type": "READ_SOURCE",
                "source_ref": (
                    "source://atlas/internal/%252e%252e/customer/accounts.json"
                ),
                "requested_fields": ["title", "content", "source_id"],
            },
            authenticated_binding={
                "binding_id": "trust_atlas_service_001",
                "issuer": "issuer:shared-mind-test",
                "subject": "workload:atlas-sync",
                "actor_id": "service:atlas-sync",
            },
        ).as_dict()

        self.assertEqual("DENY", decision["outcome"])
        self.assertEqual(["SOURCE_SCOPE_DENIED"], decision["reason_codes"])
        self.assertEqual(
            {"source_root": None, "sensitivity": None, "data_classes": []},
            decision["source_label"],
        )

    def test_request_version_is_exact_and_precedes_other_denials(self) -> None:
        compiled = self.module.compile_policy(copy.deepcopy(self.policy))
        cases: tuple[tuple[str, Any], ...] = (
            ("missing", None),
            ("unsupported", "remote-policy-request@999"),
            ("non-string", 1),
        )

        for name, request_version in cases:
            with self.subTest(case=name):
                request = self._valid_request()
                if request_version is None:
                    request.pop("request_version")
                else:
                    request["request_version"] = request_version
                request["endpoint_id"] = "remote_unpinned_999"
                request["capability"] = "source.admin"

                decision = self.module.evaluate_request(
                    compiled,
                    request,
                    authenticated_binding=self._trusted_binding(),
                ).as_dict()

                self.assertEqual("DENY", decision["outcome"])
                self.assertEqual(
                    ["REMOTE_REQUEST_VERSION_MISMATCH"],
                    decision["reason_codes"],
                )
        self.assertIn("REMOTE_REQUEST_VERSION_MISMATCH", self.module.REASON_CODES)

    def test_disclosure_paths_are_unique_grounded_and_non_overlapping(self) -> None:
        mutations: dict[str, Callable[[dict[str, Any]], None]] = {
            "duplicate allow field": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"]["allow_fields"].append("content"),
            "duplicate redaction path": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"]["redact_paths"].append("content.credentials"),
            "parent and child redactions overlap": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"]["redact_paths"].extend(
                ["content.owner", "content.owner.email"]
            ),
            "redaction root is not disclosed": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"]["redact_paths"].append("credentials.token"),
            "redaction has an empty path segment": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"]["redact_paths"].append("content..token"),
            "redaction must address a nested value": lambda document: document[
                "capability_scopes"
            ][0]["disclosure"]["redact_paths"].append("content"),
        }

        for name, mutate in mutations.items():
            with self.subTest(case=name):
                self._assert_rejected_deterministically(mutate)

    def test_compiled_policy_snapshot_is_deeply_immutable(self) -> None:
        compiled = self.module.compile_policy(copy.deepcopy(self.policy))

        with self.assertRaises(TypeError):
            compiled._document["default_effect"] = "ALLOW"
        with self.assertRaises(TypeError):
            compiled._document["trust_bindings"][0]["actor_id"] = "service:intruder"
        with self.assertRaises(TypeError):
            compiled._document["capability_scopes"][0]["actor_ids"][0] = (
                "service:intruder"
            )

    def test_forced_compiled_snapshot_replacement_fails_closed(self) -> None:
        compiled = self.module.compile_policy(copy.deepcopy(self.policy))
        forged = copy.deepcopy(self.policy)
        forged["capability_scopes"][0]["actor_ids"] = ["service:intruder"]
        object.__setattr__(compiled, "_document", forged)

        with self.assertRaisesRegex(ValueError, "integrity"):
            self.module.evaluate_request(
                compiled,
                {
                    "request_id": "remote_request_integrity_001",
                    "claimed_actor_id": "service:intruder",
                },
                authenticated_binding={
                    "binding_id": "trust_intruder",
                    "issuer": "issuer:intruder",
                    "subject": "workload:intruder",
                    "actor_id": "service:intruder",
                },
            )

    def test_decision_snapshot_is_defensive_and_id_bound(self) -> None:
        decision = self._allow_decision()
        first = decision.as_dict()
        first["endpoint_pin"]["adapter_version"] = "forged@999"
        first["disclosure"]["allowed_fields"].append("api_token")

        unchanged = decision.as_dict()
        self.assertEqual("atlas-adapter@3", unchanged["endpoint_pin"]["adapter_version"])
        self.assertNotIn("api_token", unchanged["disclosure"]["allowed_fields"])
        self.assertEqual(_expected_decision_id(unchanged), unchanged["decision_id"])

        with self.assertRaises(TypeError):
            decision._document["endpoint_pin"]["adapter_version"] = "forged@999"

    def test_forced_decision_snapshot_replacement_fails_closed(self) -> None:
        decision = self._allow_decision()
        forged = decision.as_dict()
        forged["outcome"] = "DENY"
        object.__setattr__(decision, "_document", forged)

        with self.assertRaisesRegex(ValueError, "integrity"):
            decision.as_dict()

    def _allow_decision(self) -> Any:
        compiled = self.module.compile_policy(copy.deepcopy(self.policy))
        return self.module.evaluate_request(
            compiled,
            self._valid_request(),
            authenticated_binding=self._trusted_binding(),
        )

    @staticmethod
    def _valid_request() -> dict[str, Any]:
        return {
            "request_version": "remote-policy-request@1",
            "request_id": "remote_request_atlas_001",
            "claimed_actor_id": "service:atlas-sync",
            "endpoint_id": "remote_atlas_001",
            "protocol_version": "shared-mind-remote@1",
            "adapter_version": "atlas-adapter@3",
            "registry_version": "predicate-registry@1",
            "capability": "source.read",
            "operation_type": "READ_SOURCE",
            "source_ref": "source://atlas/internal/runbooks/database.md",
            "requested_fields": ["title", "content", "source_id"],
        }

    @staticmethod
    def _trusted_binding() -> dict[str, str]:
        return {
            "binding_id": "trust_atlas_service_001",
            "issuer": "issuer:shared-mind-test",
            "subject": "workload:atlas-sync",
            "actor_id": "service:atlas-sync",
        }

    def _assert_rejected_deterministically(
        self, mutate: Callable[[dict[str, Any]], None]
    ) -> None:
        messages: list[str] = []
        for _ in range(2):
            document = copy.deepcopy(self.policy)
            mutate(document)
            with self.assertRaises(ValueError) as caught:
                self.module.compile_policy(document)
            messages.append(str(caught.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertTrue(messages[0])


def _expected_decision_id(document: dict[str, Any]) -> str:
    payload = copy.deepcopy(document)
    payload.pop("decision_id")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"remote_policy_decision_{digest[:32]}"


def _replace_source_root(document: dict[str, Any], old: str, new: str) -> None:
    for label in document["source_labels"]:
        if label["source_root"] == old:
            label["source_root"] = new
    for scope in document["capability_scopes"]:
        scope["source_roots"] = [
            new if source_root == old else source_root
            for source_root in scope["source_roots"]
        ]


if __name__ == "__main__":
    unittest.main()
