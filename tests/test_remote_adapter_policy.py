from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import inspect
import json
import socket
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "remote_policy"
POLICY_PATH = FIXTURE_ROOT / "atlas-remote-policy.v1.json"
CASES_PATH = FIXTURE_ROOT / "atlas-remote-policy-cases.v1.json"
REMOTE_POLICY_AVAILABLE = (
    importlib.util.find_spec("shared_mind.remote_policy") is not None
)


class RemotePolicyFixtureContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = _load_json(POLICY_PATH)
        cls.cases = _load_json(CASES_PATH)

    def test_fixture_pins_deny_default_identity_endpoint_and_registry(self) -> None:
        self.assertEqual("remote-adapter-policy@1", self.policy["policy_version"])
        self.assertEqual("DENY", self.policy["default_effect"])
        self.assertEqual(
            "predicate-registry@1", self.policy["registry_version"]
        )
        self.assertEqual(
            {
                "endpoint_id",
                "origin",
                "protocol_version",
                "adapter_version",
            },
            set(self.policy["endpoint_pin"]),
        )
        trust = self.policy["trust_bindings"][0]
        self.assertEqual(
            {"binding_id", "issuer", "subject", "actor_id"}, set(trust)
        )

    def test_fixture_has_source_labels_disclosure_and_operation_scopes(self) -> None:
        labels = {
            item["source_root"]: (item["sensitivity"], item["data_classes"])
            for item in self.policy["source_labels"]
        }
        self.assertEqual(
            ("RESTRICTED", ["PII"]),
            labels["source://atlas/internal/customer/"],
        )

        scopes = {
            item["capability"]: item
            for item in self.policy["capability_scopes"]
        }
        self.assertEqual({"source.read", "proposal.submit"}, set(scopes))
        self.assertEqual(["READ_SOURCE"], scopes["source.read"]["operation_types"])
        self.assertIn(
            "REGISTER_SOURCE_REVISION",
            scopes["proposal.submit"]["operation_types"],
        )
        self.assertIn("content", scopes["source.read"]["disclosure"]["allow_fields"])
        self.assertEqual(
            ["content.credentials", "content.owner_email"],
            scopes["source.read"]["disclosure"]["redact_paths"],
        )

    def test_fixture_covers_every_hostile_policy_boundary(self) -> None:
        cases = {item["name"]: item for item in self.cases["hostile_cases"]}
        self.assertEqual(
            {
                "actor_mismatch",
                "arbitrary_file_source_scope",
                "missing_trust_binding_self_assertion_is_insufficient",
                "operation_outside_capability_scope",
                "predicate_registry_drift",
                "remote_adapter_version_drift",
                "remote_endpoint_drift",
                "restricted_pii_source",
                "secret_and_pii_field_request",
                "unknown_capability_deny_by_default",
                "untrusted_binding",
            },
            set(cases),
        )
        actual_codes = {
            code
            for case in cases.values()
            for code in case["expected_reason_codes"]
        }
        self.assertEqual(set(self.cases["known_reason_codes"]), actual_codes)
        self.assertIsNone(
            cases["missing_trust_binding_self_assertion_is_insufficient"][
                "binding"
            ]
        )

    def test_fixture_pins_the_canonical_policy_hash(self) -> None:
        self.assertEqual(
            self.cases["expected_policy_hash"], _canonical_hash(self.policy)
        )


class RemotePolicyPublicApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.policy_document = _load_json(POLICY_PATH)
        cls.fixture = _load_json(CASES_PATH)

    def test_planned_public_api_exists_with_out_of_band_binding(self) -> None:
        self.assertTrue(
            REMOTE_POLICY_AVAILABLE,
            "planned pure module shared_mind.remote_policy is not implemented",
        )
        module = importlib.import_module("shared_mind.remote_policy")
        for name in (
            "CompiledRemotePolicy",
            "RemotePolicyDecision",
            "compile_policy",
            "evaluate_request",
        ):
            with self.subTest(public_name=name):
                self.assertTrue(hasattr(module, name), name)

        self.assertEqual(
            ["document"], list(inspect.signature(module.compile_policy).parameters)
        )
        evaluate_parameters = inspect.signature(module.evaluate_request).parameters
        self.assertEqual(
            ["policy", "request", "authenticated_binding"],
            list(evaluate_parameters),
        )
        self.assertEqual(
            inspect.Parameter.KEYWORD_ONLY,
            evaluate_parameters["authenticated_binding"].kind,
        )

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_policy_hash_is_canonical_deterministic_and_input_is_unchanged(
        self,
    ) -> None:
        module = _remote_policy_module()
        original = copy.deepcopy(self.policy_document)
        compiled = module.compile_policy(self.policy_document)
        reordered = module.compile_policy(_reverse_mapping_order(self.policy_document))

        self.assertIsInstance(compiled, module.CompiledRemotePolicy)
        self.assertEqual(self.fixture["expected_policy_hash"], compiled.policy_hash)
        self.assertEqual(compiled.policy_hash, reordered.policy_hash)
        self.assertEqual(original, self.policy_document)

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_allow_decision_is_canonical_grounded_and_repeatable(self) -> None:
        module, compiled = self._compiled()
        request = copy.deepcopy(self.fixture["base_request"])
        binding = copy.deepcopy(self.fixture["authenticated_bindings"]["trusted"])

        with _network_forbidden():
            first = module.evaluate_request(
                compiled, request, authenticated_binding=binding
            )
            second = module.evaluate_request(
                compiled, request, authenticated_binding=binding
            )

        self.assertIsInstance(first, module.RemotePolicyDecision)
        document = first.as_dict()
        self.assertEqual(document, second.as_dict())
        contract = self.fixture["audit_contract"]
        self.assertEqual(set(contract["decision_keys"]), set(document))
        self.assertEqual(
            set(contract["endpoint_pin_keys"]), set(document["endpoint_pin"])
        )
        self.assertEqual(
            set(contract["source_label_keys"]), set(document["source_label"])
        )
        self.assertEqual(
            set(contract["disclosure_keys"]), set(document["disclosure"])
        )
        self.assertEqual(contract["decision_version"], document["decision_version"])
        self.assertTrue(
            document["decision_id"].startswith(contract["decision_id_prefix"])
        )
        self.assertEqual(self.fixture["expected_policy_hash"], document["policy_hash"])
        for key, value in self.fixture["expected_allow"].items():
            with self.subTest(field=key):
                self.assertEqual(value, document[key])

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_actor_claim_never_substitutes_for_authenticated_binding(self) -> None:
        mismatch = self._evaluate_case("actor_mismatch")
        missing = self._evaluate_case(
            "missing_trust_binding_self_assertion_is_insufficient"
        )
        untrusted = self._evaluate_case("untrusted_binding")

        self.assertEqual("service:atlas-sync", mismatch["authenticated_actor_id"])
        self.assertEqual("trust_atlas_service_001", mismatch["trust_binding_id"])
        self.assertIsNone(missing["authenticated_actor_id"])
        self.assertIsNone(missing["trust_binding_id"])
        self.assertIsNone(untrusted["authenticated_actor_id"])
        self.assertIsNone(untrusted["trust_binding_id"])

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_disclosure_is_allowlisted_redacted_and_secret_safe(self) -> None:
        allowed = self._evaluate_allow()
        denied = self._evaluate_case("secret_and_pii_field_request")

        self.assertEqual(self.fixture["expected_allow"]["disclosure"], allowed["disclosure"])
        self.assertEqual(
            ["DISCLOSURE_FIELD_DENIED"], denied["reason_codes"]
        )
        self.assertEqual([], denied["disclosure"]["allowed_fields"])
        self.assertNotIn(
            self.fixture["authenticated_bindings"]["trusted"]["secret_material"],
            _canonical_json(allowed),
        )
        self.assertNotIn(
            self.fixture["base_request"]["source_ref"], _canonical_json(allowed)
        )
        self.assertNotIn("api_token", _canonical_json(denied))

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_source_root_and_sensitivity_are_policy_derived(self) -> None:
        allowed = self._evaluate_allow()
        arbitrary = self._evaluate_case("arbitrary_file_source_scope")
        restricted = self._evaluate_case("restricted_pii_source")

        self.assertEqual(
            self.fixture["expected_allow"]["source_label"],
            allowed["source_label"],
        )
        self.assertEqual(
            {"source_root": None, "sensitivity": None, "data_classes": []},
            arbitrary["source_label"],
        )
        self.assertEqual(
            {
                "source_root": "source://atlas/internal/customer/",
                "sensitivity": "RESTRICTED",
                "data_classes": ["PII"],
            },
            restricted["source_label"],
        )

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_source_scope_rejects_dot_segment_escape_before_labeling(self) -> None:
        request = copy.deepcopy(self.fixture["base_request"])
        request["source_ref"] = "source://atlas/internal/../customer/accounts.json"
        module, compiled = self._compiled()

        decision = module.evaluate_request(
            compiled,
            request,
            authenticated_binding=copy.deepcopy(
                self.fixture["authenticated_bindings"]["trusted"]
            ),
        ).as_dict()

        self.assertEqual("DENY", decision["outcome"])
        self.assertEqual(["SOURCE_SCOPE_DENIED"], decision["reason_codes"])
        self.assertEqual(
            {"source_root": None, "sensitivity": None, "data_classes": []},
            decision["source_label"],
        )

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_capabilities_operations_and_unknown_values_deny_by_default(self) -> None:
        for name, reason in (
            ("unknown_capability_deny_by_default", "UNKNOWN_CAPABILITY"),
            ("operation_outside_capability_scope", "OPERATION_SCOPE_DENIED"),
        ):
            with self.subTest(case=name):
                decision = self._evaluate_case(name)
                self.assertEqual("DENY", decision["outcome"])
                self.assertEqual([reason], decision["reason_codes"])

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_endpoint_adapter_and_registry_drift_are_denied(self) -> None:
        expected = {
            "remote_endpoint_drift": "ENDPOINT_PIN_MISMATCH",
            "remote_adapter_version_drift": "REMOTE_VERSION_PIN_MISMATCH",
            "predicate_registry_drift": "REGISTRY_VERSION_MISMATCH",
        }
        for name, reason in expected.items():
            with self.subTest(case=name):
                decision = self._evaluate_case(name)
                self.assertEqual("DENY", decision["outcome"])
                self.assertEqual([reason], decision["reason_codes"])

    @unittest.skipUnless(
        REMOTE_POLICY_AVAILABLE, "planned remote policy module is absent"
    )
    def test_every_hostile_case_is_denied_without_secret_or_source_echo(self) -> None:
        canaries = {
            binding["secret_material"]
            for binding in self.fixture["authenticated_bindings"].values()
        }
        for case in self.fixture["hostile_cases"]:
            with self.subTest(case=case["name"]):
                document = self._evaluate_case(case["name"])
                serialized = _canonical_json(document)
                self.assertEqual("DENY", document["outcome"])
                self.assertEqual(case["expected_reason_codes"], document["reason_codes"])
                for canary in canaries:
                    self.assertNotIn(canary, serialized)
                self.assertNotIn(case["request_overrides"].get("source_ref", "\0"), serialized)

    def _compiled(self) -> tuple[Any, Any]:
        module = _remote_policy_module()
        return module, module.compile_policy(copy.deepcopy(self.policy_document))

    def _evaluate_allow(self) -> dict[str, Any]:
        module, compiled = self._compiled()
        with _network_forbidden():
            decision = module.evaluate_request(
                compiled,
                copy.deepcopy(self.fixture["base_request"]),
                authenticated_binding=copy.deepcopy(
                    self.fixture["authenticated_bindings"]["trusted"]
                ),
            )
        return decision.as_dict()

    def _evaluate_case(self, name: str) -> dict[str, Any]:
        case = next(
            item for item in self.fixture["hostile_cases"] if item["name"] == name
        )
        request = copy.deepcopy(self.fixture["base_request"])
        request.update(copy.deepcopy(case["request_overrides"]))
        binding_name = case["binding"]
        binding = (
            None
            if binding_name is None
            else copy.deepcopy(self.fixture["authenticated_bindings"][binding_name])
        )
        module, compiled = self._compiled()
        with _network_forbidden():
            decision = module.evaluate_request(
                compiled, request, authenticated_binding=binding
            )
        return decision.as_dict()


def _remote_policy_module() -> Any:
    return importlib.import_module("shared_mind.remote_policy")


def _network_forbidden() -> Any:
    return patch.object(
        socket,
        "create_connection",
        side_effect=AssertionError("remote policy evaluation must be pure and local"),
    )


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AssertionError(f"expected JSON object: {path}")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _reverse_mapping_order(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _reverse_mapping_order(item)
            for key, item in reversed(tuple(value.items()))
        }
    if isinstance(value, list):
        return [_reverse_mapping_order(item) for item in value]
    return value


if __name__ == "__main__":
    unittest.main()
