from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

from .canonical import canonical_json
from .workspace import Workspace, WorkspaceError


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION_ERROR = 3
EXIT_TRANSACTION_CONFLICT = 4
EXIT_INTEGRITY_ERROR = 5
EXIT_CAPABILITY_UNAVAILABLE = 6
EXIT_IO_ERROR = 7
EXIT_INTERNAL_ERROR = 70


class CliUsageError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="shared-mind")
    parser.add_argument(
        "--workspace",
        help="Workspace root or a path inside it (defaults to the current directory).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init")
    init_parser.add_argument("path")
    init_parser.add_argument("--purpose")

    source_parser = commands.add_parser("source")
    source_commands = source_parser.add_subparsers(dest="source_command", required=True)
    source_add = source_commands.add_parser("add")
    source_add.add_argument("path")
    source_add.add_argument("--source-id")

    proposal_parser = commands.add_parser("proposal")
    proposal_commands = proposal_parser.add_subparsers(
        dest="proposal_command", required=True
    )
    proposal_validate = proposal_commands.add_parser("validate")
    proposal_validate.add_argument("path")
    proposal_commit = proposal_commands.add_parser("commit")
    proposal_commit.add_argument("path")
    proposal_commit.add_argument("--json", action="store_true")

    context_parser = commands.add_parser("context")
    context_parser.add_argument("--project")
    context_parser.add_argument("--subject")
    context_parser.add_argument("--budget-tokens", type=_positive_integer)
    context_parser.add_argument("--budget-bytes", type=_positive_integer)

    conflict_parser = commands.add_parser("conflict")
    conflict_commands = conflict_parser.add_subparsers(
        dest="conflict_command", required=True
    )
    conflict_list = conflict_commands.add_parser("list")
    conflict_list.add_argument("--status", choices=("OPEN", "RESOLVED", "REOPENED"))
    conflict_resolve = conflict_commands.add_parser("resolve")
    conflict_resolve.add_argument("conflict_id")
    conflict_resolve.add_argument("--proposal", required=True)

    replay_parser = commands.add_parser("replay")
    replay_parser.add_argument("--verify", action="store_true", required=True)

    project_parser = commands.add_parser("project")
    project_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    del errors  # All operational results, including errors, are JSON on stdout.
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "init":
            workspace = Workspace.initialize(arguments.path, purpose=arguments.purpose)
            return _emit(
                output,
                True,
                "WORKSPACE_INITIALIZED",
                data=workspace.describe(),
            )
        workspace = Workspace.open(arguments.workspace or Path.cwd())
        if arguments.command == "source":
            return _source_command(workspace, arguments, output)
        if arguments.command == "proposal":
            return _proposal_command(workspace, arguments, output)
        if arguments.command == "context":
            return _context_command(workspace, arguments, output)
        if arguments.command == "conflict":
            return _conflict_command(workspace, arguments, output)
        if arguments.command == "replay":
            return _replay_command(workspace, output)
        if arguments.command == "project":
            return _project_command(workspace, arguments, output)
        raise CliUsageError(f"Unsupported command: {arguments.command}")
    except CliUsageError as exc:
        return _emit(output, False, "USAGE_ERROR", message=str(exc), exit_code=EXIT_USAGE)
    except WorkspaceError as exc:
        return _emit(
            output,
            False,
            exc.code,
            message=exc.message,
            exit_code=_workspace_error_exit(exc.code),
        )
    except (OSError, PermissionError) as exc:
        return _emit(
            output,
            False,
            "IO_ERROR",
            message=str(exc),
            exit_code=EXIT_IO_ERROR,
        )
    except Exception as exc:  # pragma: no cover - last-resort stable CLI boundary
        return _emit(
            output,
            False,
            "INTERNAL_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            exit_code=EXIT_INTERNAL_ERROR,
        )


def _source_command(
    workspace: Workspace, arguments: argparse.Namespace, output: TextIO
) -> int:
    source, receipt = workspace.add_source(arguments.path, source_id=arguments.source_id)
    if receipt.outcome not in ("COMMITTED", "FACT_CONFLICT"):
        return _emit_receipt(output, receipt)
    data = dict(source)
    data.update(_receipt_data(receipt))
    return _emit(output, True, "SOURCE_REGISTERED", data=data)


def _proposal_command(
    workspace: Workspace, arguments: argparse.Namespace, output: TextIO
) -> int:
    proposal = workspace.load_json(arguments.path)
    if arguments.proposal_command == "validate":
        issues = workspace.validate_proposal(proposal)
        if issues:
            return _emit(
                output,
                False,
                "PROPOSAL_INVALID",
                errors=[issue.as_dict() for issue in issues],
                message="Proposal validation failed.",
                exit_code=EXIT_VALIDATION_ERROR,
            )
        return _emit(output, True, "PROPOSAL_VALID", data={"valid": True})
    kernel = workspace.open_kernel()
    try:
        receipt = kernel.commit(proposal)
    finally:
        kernel.close()
    return _emit_receipt(output, receipt)


def _conflict_command(
    workspace: Workspace, arguments: argparse.Namespace, output: TextIO
) -> int:
    if arguments.conflict_command == "list":
        conflicts = workspace.list_conflicts(arguments.status)
        return _emit(
            output,
            True,
            "CONFLICTS_LISTED",
            data={"conflicts": conflicts, "count": len(conflicts)},
        )
    proposal = workspace.load_json(arguments.proposal)
    operations = proposal.get("operations", []) if isinstance(proposal, dict) else []
    resolutions = [
        operation
        for operation in operations
        if isinstance(operation, dict) and operation.get("op") == "RESOLVE_CONFLICT"
    ]
    if (
        len(resolutions) != 1
        or resolutions[0].get("conflict_id") != arguments.conflict_id
    ):
        return _emit(
            output,
            False,
            "CONFLICT_PROPOSAL_MISMATCH",
            message="Proposal must contain exactly one matching RESOLVE_CONFLICT operation.",
            exit_code=EXIT_VALIDATION_ERROR,
        )
    kernel = workspace.open_kernel()
    try:
        receipt = kernel.commit(proposal)
    finally:
        kernel.close()
    return _emit_receipt(output, receipt)


def _replay_command(workspace: Workspace, output: TextIO) -> int:
    kernel = workspace.open_kernel()
    try:
        verify = getattr(kernel, "verify_ledger", None)
        if not callable(verify):
            return _capability_unavailable(output, "ledger verification")
        report = _json_value(verify())
    finally:
        kernel.close()
    valid = bool(report.get("valid")) if isinstance(report, dict) else False
    return _emit(
        output,
        valid,
        "LEDGER_VALID" if valid else "LEDGER_INVALID",
        data=report,
        message=None if valid else "Ledger verification failed.",
        exit_code=EXIT_OK if valid else EXIT_INTEGRITY_ERROR,
    )


def _project_command(
    workspace: Workspace, arguments: argparse.Namespace, output: TextIO
) -> int:
    projection = _load_projection()
    if projection is None:
        return _capability_unavailable(output, "deterministic projection")
    function_name = "project_markdown" if arguments.format == "markdown" else "project_json"
    projector = getattr(projection, function_name, None)
    if not callable(projector):
        return _capability_unavailable(output, function_name)
    kernel = workspace.open_kernel()
    try:
        projected = projector(kernel)
    finally:
        kernel.close()
    if arguments.format == "markdown":
        content = projected if isinstance(projected, str) else str(projected)
        filename = "project.md"
    else:
        if isinstance(projected, str):
            document = json.loads(projected)
        else:
            document = _json_value(projected)
        content = canonical_json(document) + "\n"
        filename = "project.json"
    destination = workspace.write_projection(filename, content)
    return _emit(
        output,
        True,
        "PROJECTED",
        data={
            "format": arguments.format,
            "path": destination.relative_to(workspace.root).as_posix(),
            "content": content,
        },
    )


def _context_command(
    workspace: Workspace, arguments: argparse.Namespace, output: TextIO
) -> int:
    if arguments.project is not None or arguments.subject is not None:
        return _emit(
            output,
            False,
            "CONTEXT_FILTER_UNSUPPORTED",
            message=(
                "Project and subject context filters are reserved but are not "
                "implemented by this workspace version."
            ),
            data={"project": arguments.project, "subject": arguments.subject},
            exit_code=EXIT_VALIDATION_ERROR,
        )
    projection = _load_projection()
    if projection is None:
        return _capability_unavailable(output, "context pack")
    build_context = getattr(projection, "build_context_pack", None)
    if not callable(build_context):
        return _capability_unavailable(output, "build_context_pack")
    keyword_arguments = {}
    if arguments.budget_tokens is not None:
        keyword_arguments["budget_tokens"] = arguments.budget_tokens
    if arguments.budget_bytes is not None:
        keyword_arguments["budget_bytes"] = arguments.budget_bytes
    if workspace.purpose is not None:
        keyword_arguments["purpose"] = workspace.purpose
    kernel = workspace.open_kernel()
    try:
        try:
            context = _json_value(build_context(kernel, **keyword_arguments))
        except Exception as exc:
            budget_error = getattr(projection, "ContextBudgetError", None)
            if budget_error is None or not isinstance(exc, budget_error):
                raise
            return _emit(
                output,
                False,
                "CONTEXT_BUDGET_TOO_SMALL",
                message=str(exc),
                data={
                    "required_bytes": exc.required_bytes,
                    "budget_bytes": exc.budget_bytes,
                },
                exit_code=EXIT_VALIDATION_ERROR,
            )
    finally:
        kernel.close()
    return _emit(
        output,
        True,
        "CONTEXT_READY",
        data={
            "context": context,
            "filters": {
                "project": arguments.project,
                "subject": arguments.subject,
            },
        },
    )


def _load_projection() -> Any | None:
    try:
        from . import projection
    except ImportError:
        return None
    return projection


def _emit_receipt(output: TextIO, receipt: Any) -> int:
    outcome = str(receipt.outcome)
    ok = outcome in ("COMMITTED", "FACT_CONFLICT")
    exit_code = {
        "COMMITTED": EXIT_OK,
        "FACT_CONFLICT": EXIT_OK,
        "TRANSACTION_CONFLICT": EXIT_TRANSACTION_CONFLICT,
        "VALIDATION_ERROR": EXIT_VALIDATION_ERROR,
    }.get(outcome, EXIT_INTERNAL_ERROR)
    return _emit(
        output,
        ok,
        outcome,
        data=_receipt_data(receipt),
        message=None if ok else "Proposal was not committed.",
        exit_code=exit_code,
    )


def _receipt_data(receipt: Any) -> dict[str, Any]:
    return {
        "proposal_id": receipt.proposal_id,
        "ledger_sequence": receipt.ledger_seq,
        "state_root": receipt.state_root,
        "reason_codes": list(receipt.reason_codes),
        "conflict_ids": list(receipt.conflict_ids),
    }


def _capability_unavailable(output: TextIO, capability: str) -> int:
    return _emit(
        output,
        False,
        "CAPABILITY_UNAVAILABLE",
        message=f"This kernel does not provide {capability} yet.",
        data={"capability": capability},
        exit_code=EXIT_CAPABILITY_UNAVAILABLE,
    )


def _emit(
    output: TextIO,
    ok: bool,
    code: str,
    *,
    data: Any | None = None,
    errors: list[dict[str, Any]] | None = None,
    message: str | None = None,
    exit_code: int = EXIT_OK,
) -> int:
    result: dict[str, Any] = {"ok": ok, "code": code}
    if message is not None:
        result["message"] = message
    if errors is not None:
        result["errors"] = errors
    if data is not None:
        result["data"] = _json_value(data)
    output.write(canonical_json(result) + "\n")
    output.flush()
    return exit_code


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _json_value(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _workspace_error_exit(code: str) -> int:
    if code.endswith("READ_FAILED") or code.endswith("WRITE_FAILED"):
        return EXIT_IO_ERROR
    return EXIT_VALIDATION_ERROR
