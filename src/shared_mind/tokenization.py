"""Versioned protocol for optional, exact context token accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


PROTOCOL_VERSION = "exact-token-counter@1"


class TokenCounterError(ValueError):
    """Raised when an exact counter cannot provide trustworthy output."""


@dataclass(frozen=True)
class TokenizerMetadata:
    """Pinned identity of the tokenizer used for a hard token budget."""

    name: str
    version: str
    fingerprint: str
    model: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("name", "version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if (
            not isinstance(self.fingerprint, str)
            or len(self.fingerprint) != 71
            or not self.fingerprint.startswith("sha256:")
        ):
            raise ValueError("fingerprint must be a sha256 hash")
        try:
            int(self.fingerprint[7:], 16)
        except ValueError as exc:
            raise ValueError("fingerprint must be a sha256 hash") from exc
        if self.model is not None and (
            not isinstance(self.model, str) or not self.model.strip()
        ):
            raise ValueError("model must be null or a non-empty string")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "model": self.model,
        }


@runtime_checkable
class ExactTokenCounter(Protocol):
    """Small dependency-injection boundary for model-specific tokenizers."""

    metadata: TokenizerMetadata

    def count_tokens(self, text: str) -> int:
        """Return the exact token count for *text*."""


def validated_token_count(counter: object, text: str) -> int:
    """Count twice and fail closed on invalid or nondeterministic adapters."""

    metadata = getattr(counter, "metadata", None)
    count_tokens = getattr(counter, "count_tokens", None)
    if not isinstance(metadata, TokenizerMetadata) or not callable(count_tokens):
        raise TokenCounterError("invalid exact token counter")
    try:
        first = count_tokens(text)
        second = count_tokens(text)
    except Exception as exc:
        raise TokenCounterError("exact token counter failed") from exc
    for value in (first, second):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TokenCounterError("exact token counter returned an invalid count")
    if first != second:
        raise TokenCounterError("exact token counter is nondeterministic")
    return first


__all__ = [
    "ExactTokenCounter",
    "PROTOCOL_VERSION",
    "TokenCounterError",
    "TokenizerMetadata",
    "validated_token_count",
]
