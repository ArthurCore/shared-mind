"""Claude Code and Codex session bootstrap hook adapter."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from ..canonical import canonical_json
from ..session_bootstrap import bootstrap_session
from ..workspace import MAX_JSON_BYTES


class _HookArgumentError(Exception):
    pass


class _HookParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _HookArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _HookParser(prog="shared-mind-session-hook", add_help=False)
    parser.add_argument("client", choices=("claude", "codex"))
    parser.add_argument("action", choices=("start", "prompt"))
    parser.add_argument("--cwd")
    parser.add_argument("--binding")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    try:
        arguments = build_parser().parse_args(argv)
        payload = _read_payload(stdin if stdin is not None else sys.stdin)
        cwd = arguments.cwd or _string(payload.get("cwd")) or Path.cwd().as_posix()
        prompt = _string(payload.get("prompt")) if arguments.action == "prompt" else None
        phase = "UserPromptSubmit" if arguments.action == "prompt" else "SessionStart"
        result = bootstrap_session(
            cwd=cwd,
            prompt=prompt,
            phase=phase,
            binding=arguments.binding,
        )
        document = _hook_output(phase, result)
    except Exception as exc:  # pragma: no cover - stable fail-open hook boundary
        errors.write(f"shared-mind session hook skipped: {type(exc).__name__}: {exc}\n")
        document = {
            "continue": True,
            "systemMessage": f"Shared Mind bootstrap skipped: {type(exc).__name__}",
        }
    output.write(canonical_json(document) + "\n")
    output.flush()
    return 0


def _read_payload(handle: TextIO) -> dict[str, Any]:
    raw = handle.read(MAX_JSON_BYTES + 1)
    if len(raw.encode("utf-8")) > MAX_JSON_BYTES:
        return {}
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _hook_output(phase: str, result: Mapping[str, Any]) -> dict[str, Any]:
    additional_context = result.get("additional_context")
    if isinstance(additional_context, str) and additional_context:
        return {
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": phase,
                "additionalContext": additional_context,
            },
        }
    return {
        "continue": True,
        "systemMessage": f"Shared Mind bootstrap skipped: {result.get('status')}",
    }


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
