from __future__ import annotations

import importlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from shared_mind import Kernel
from shared_mind.canonical import canonical_json, sha256_json
from shared_mind.projection import (
    DEFAULT_CONTEXT_BUDGET_BYTES,
    ContextBudgetError,
    build_context_pack,
)


ROOT = Path(__file__).resolve().parents[1]


def _tokenization_api() -> Any:
    return importlib.import_module("shared_mind.tokenization")


class _Counter:
    def __init__(
        self, metadata: object, count: Callable[[str], object]
    ) -> None:
        self.metadata = metadata
        self._count = count

    def count_tokens(self, text: str) -> object:
        return self._count(text)


class _NondeterministicCounter:
    def __init__(self, metadata: object) -> None:
        self.metadata = metadata
        self._calls: dict[str, int] = {}

    def count_tokens(self, text: str) -> int:
        invocation = self._calls.get(text, 0)
        self._calls[text] = invocation + 1
        return len(text) + invocation


class ExactTokenContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        registry = json.loads(
            (ROOT / "contracts" / "atlas-predicate-registry.v1.json").read_text(
                encoding="utf-8"
            )
        )
        self.kernel = Kernel(Path(self.temp.name) / "kernel.sqlite3", registry)
        self.addCleanup(self.kernel.close)

    def test_protocol_metadata_and_validated_count_are_explicit(self) -> None:
        tokenization = _tokenization_api()
        metadata = tokenization.TokenizerMetadata(
            name="unicode-codepoints",
            version="unicode-15.1",
            fingerprint="sha256:" + "a" * 64,
            model=None,
        )
        counter = _Counter(metadata, len)

        self.assertIsInstance(counter, tokenization.ExactTokenCounter)
        self.assertEqual("exact-token-counter@1", tokenization.PROTOCOL_VERSION)
        self.assertEqual(
            {
                "name": "unicode-codepoints",
                "version": "unicode-15.1",
                "fingerprint": "sha256:" + "a" * 64,
                "model": None,
            },
            metadata.to_dict(),
        )
        self.assertEqual(3, tokenization.validated_token_count(counter, "기억🙂"))

        invalid_metadata = (
            {"name": "", "version": "1", "fingerprint": "sha256:" + "a" * 64},
            {"name": "tokens", "version": "", "fingerprint": "sha256:" + "a" * 64},
            {"name": "tokens", "version": "1", "fingerprint": "not-a-hash"},
            {
                "name": "tokens",
                "version": "1",
                "fingerprint": "sha256:" + "a" * 64,
                "model": "",
            },
        )
        for values in invalid_metadata:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    tokenization.TokenizerMetadata(**values)

    def test_exact_count_matches_final_unicode_canonical_json_and_is_deterministic(
        self,
    ) -> None:
        tokenization = _tokenization_api()
        metadata = self._metadata(
            "unicode-codepoints", "unicode-15.1", "b", model="model:한글🙂"
        )
        counter = _Counter(metadata, len)
        purpose = "다음 AI가 근거와 충돌을 이어받습니다. 🧠"

        first = build_context_pack(
            self.kernel,
            budget_bytes=8_000,
            budget_tokens=4_000,
            purpose=purpose,
            token_counter=counter,
        )
        second = build_context_pack(
            self.kernel.connection,
            budget_bytes=8_000,
            budget_tokens=4_000,
            purpose=purpose,
            token_counter=counter,
        )

        rendered = canonical_json(first)
        truncation = first["truncation"]
        self.assertEqual(first, second)
        self.assertEqual("handoff-context@3", first["context_pack_version"])
        self.assertEqual("context-selection@3", truncation["selection_rule_version"])
        self.assertTrue(truncation["token_estimate_exact"])
        self.assertEqual(len(rendered), truncation["rendered_tokens"])
        self.assertEqual(
            tokenization.validated_token_count(counter, rendered),
            truncation["rendered_tokens"],
        )
        self.assertEqual(len(rendered.encode("utf-8")), truncation["rendered_bytes"])
        self.assertEqual("exact-token-counter@1", truncation["token_counter_protocol"])
        self.assertEqual("canonical-context-json", truncation["token_count_scope"])
        self.assertEqual(metadata.to_dict(), truncation["tokenizer"])

    def test_exact_byte_and_token_limits_are_both_hard_caps(self) -> None:
        metadata = self._metadata("ascii-codepoints", "1.0.0", "c")
        counter = _Counter(metadata, len)
        self._insert_claims(24, payload_size=400)

        context = build_context_pack(
            self.kernel,
            budget_bytes=4_000,
            budget_tokens=2_500,
            purpose="Keep both exact-token and byte limits.",
            token_counter=counter,
        )

        rendered = canonical_json(context)
        self.assertLessEqual(len(rendered.encode("utf-8")), 4_000)
        self.assertLessEqual(len(rendered), 2_500)
        self.assertEqual(len(rendered), context["truncation"]["rendered_tokens"])
        self.assertGreater(
            context["truncation"]["omitted_counts"]["current_claims"], 0
        )

    def test_token_only_exact_mode_keeps_the_default_byte_hard_limit(self) -> None:
        metadata = self._metadata("single-token", "1.0.0", "d")
        counter = _Counter(metadata, lambda _: 1)
        self._insert_claims(100, payload_size=800)

        context = build_context_pack(
            self.kernel,
            budget_tokens=100,
            purpose="A token counter must not disable the byte ceiling.",
            token_counter=counter,
        )

        rendered = canonical_json(context).encode("utf-8")
        truncation = context["truncation"]
        self.assertEqual(DEFAULT_CONTEXT_BUDGET_BYTES, truncation["budget_bytes"])
        self.assertIsNone(truncation["requested_budget_bytes"])
        self.assertLessEqual(len(rendered), DEFAULT_CONTEXT_BUDGET_BYTES)
        self.assertEqual(1, truncation["rendered_tokens"])
        self.assertGreater(truncation["omitted_counts"]["current_claims"], 0)

    def test_exact_token_overflow_of_mandatory_context_fails_closed(self) -> None:
        metadata = self._metadata("unicode-codepoints", "15.1", "e")
        counter = _Counter(metadata, len)

        with self.assertRaises(ContextBudgetError) as caught:
            build_context_pack(
                self.kernel,
                budget_tokens=1,
                purpose="Mandatory purpose must not be silently dropped.",
                token_counter=counter,
            )

        self.assertEqual(1, caught.exception.budget_tokens)
        self.assertGreater(caught.exception.required_tokens, 1)
        self.assertIn("token", str(caught.exception))

    def test_invalid_or_nondeterministic_counters_fail_closed(self) -> None:
        tokenization = _tokenization_api()
        metadata = self._metadata("invalid-result-probe", "1.0.0", "f")

        def explode(_: str) -> int:
            raise RuntimeError("tokenizer unavailable")

        factories: tuple[tuple[str, Callable[[], object]], ...] = (
            ("exception", lambda: _Counter(metadata, explode)),
            ("negative", lambda: _Counter(metadata, lambda _: -1)),
            ("boolean", lambda: _Counter(metadata, lambda _: True)),
            ("float", lambda: _Counter(metadata, lambda _: 1.5)),
            ("string", lambda: _Counter(metadata, lambda _: "1")),
            ("nondeterministic", lambda: _NondeterministicCounter(metadata)),
            ("missing-metadata", object),
        )
        for name, factory in factories:
            with self.subTest(name=name):
                counter = factory()
                with self.assertRaises(tokenization.TokenCounterError):
                    tokenization.validated_token_count(counter, "{}")
                with self.assertRaises(tokenization.TokenCounterError):
                    build_context_pack(
                        self.kernel,
                        budget_tokens=4_000,
                        purpose="Fail closed on an invalid exact counter.",
                        token_counter=factory(),
                    )

    def test_no_adapter_preserves_the_versioned_estimator(self) -> None:
        implicit = build_context_pack(
            self.kernel,
            budget_tokens=700,
            purpose="Retain dependency-free estimation.",
        )
        explicit = build_context_pack(
            self.kernel.connection,
            budget_tokens=700,
            purpose="Retain dependency-free estimation.",
            token_counter=None,
        )

        encoded = canonical_json(implicit).encode("utf-8")
        truncation = implicit["truncation"]
        self.assertEqual(implicit, explicit)
        self.assertEqual("handoff-context@3", implicit["context_pack_version"])
        self.assertEqual("context-selection@3", truncation["selection_rule_version"])
        self.assertFalse(truncation["token_estimate_exact"])
        self.assertEqual("ceil(utf8_bytes/4)", truncation["token_estimator"])
        self.assertEqual(
            "utf8-bytes-token-estimator@1",
            truncation["token_estimator_version"],
        )
        self.assertEqual(math.ceil(len(encoded) / 4), truncation["estimated_tokens"])

    @staticmethod
    def _metadata(
        name: str, version: str, fingerprint_character: str, *, model: str | None = None
    ) -> Any:
        tokenization = _tokenization_api()
        return tokenization.TokenizerMetadata(
            name=name,
            version=version,
            fingerprint="sha256:" + fingerprint_character * 64,
            model=model,
        )

    def _insert_claims(self, count: int, *, payload_size: int) -> None:
        with self.kernel._authorized_writes():
            for index in range(count):
                claim_id = f"claim_token_budget_{index:04d}"
                proposition = {
                    "subject": {"entity_id": f"benchmark:token:{index:04d}"},
                    "predicate": "benchmark.token_payload@1",
                    "scope": {"environment": "test"},
                    "object": {
                        "kind": "SCALAR",
                        "value": f"{index:04d}:" + "x" * payload_size,
                    },
                }
                document = {
                    "object_type": "CLAIM",
                    "claim_id": claim_id,
                    "proposition": proposition,
                }
                self.kernel.connection.execute(
                    """
                    INSERT INTO claims(
                      claim_id, proposition_hash, proposition, document,
                      status, version, superseded_by
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', 1, NULL)
                    """,
                    (
                        claim_id,
                        sha256_json(proposition),
                        canonical_json(proposition),
                        canonical_json(document),
                    ),
                )


if __name__ == "__main__":
    unittest.main()
