"""JSON-only command line interface for Shared Mind's product layer.

The existing ``shared-mind`` command remains the stable kernel interface.  This
entrypoint exposes the staging, derived-view, Skill, retrieval, governance, and
cold-start workflows without weakening the Proposal-only canonical write
boundary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO

from .canonical import canonical_json
from .product import ProductError, ProductService
from .workspace import Workspace, WorkspaceError


EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INTEGRITY_ERROR = 5
EXIT_IO_ERROR = 7
EXIT_INTERNAL_ERROR = 70


class ProductCliUsageError(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ProductCliUsageError(message)


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _non_negative_number(value: str) -> int | float:
    parsed: int | float = float(value) if any(mark in value for mark in ".eE") else int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _non_negative_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="shared-mind-product")
    parser.add_argument(
        "--workspace",
        help="Workspace root or a path inside it (defaults to the current directory).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("describe")

    ingest = commands.add_parser("ingest")
    ingest.add_argument("paths", nargs="*")
    ingest.add_argument("--conversation", action="append", default=[])
    ingest.add_argument("--no-code", action="store_true")

    extract = commands.add_parser("extract")
    extract.add_argument("batch_id")

    draft = commands.add_parser("draft")
    draft_commands = draft.add_subparsers(dest="draft_command", required=True)
    draft_list = draft_commands.add_parser("list")
    draft_list.add_argument("--status")
    draft_list.add_argument("--kind")
    draft_list.add_argument("--batch-id")
    draft_show = draft_commands.add_parser("show")
    draft_show.add_argument("draft_id")
    draft_edit = draft_commands.add_parser("edit")
    draft_edit.add_argument("draft_id")
    draft_edit.add_argument("document")
    draft_edit.add_argument("--expected-version", required=True, type=_positive)
    draft_reject = draft_commands.add_parser("reject")
    draft_reject.add_argument("draft_id")
    draft_reject.add_argument("--rationale", required=True)
    draft_commit = draft_commands.add_parser("commit")
    draft_commit.add_argument("draft_id")
    draft_commit_batch = draft_commands.add_parser("commit-batch")
    draft_commit_batch.add_argument("batch_id")
    draft_commit_batch.add_argument("--include-model", action="store_true")
    draft_commands.add_parser("expire")

    build = commands.add_parser("build")
    build.add_argument("target", choices=("views", "indexes", "all"), default="all", nargs="?")

    context = commands.add_parser("context")
    context.add_argument("--task", required=True)
    context.add_argument("--purpose")
    context.add_argument("--query")
    context.add_argument("--ref", dest="references", action="append", default=[])
    context.add_argument("--depth", choices=("SUMMARY", "DETAIL", "EVIDENCE"), default="DETAIL")
    context.add_argument("--budget-bytes", type=_positive)
    context.add_argument("--budget-tokens", type=_positive)

    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--kind", dest="kinds", action="append", default=[])
    search.add_argument("--limit", type=_positive, default=20)

    tool = commands.add_parser("tool")
    tool.add_argument("name")
    tool.add_argument("--arguments", default="{}")

    cold = commands.add_parser("cold-start")
    cold.add_argument("paths", nargs="*")
    cold.add_argument("--conversation", action="append", default=[])
    cold.add_argument("--no-auto-commit", action="store_true")
    cold.add_argument("--task", default="Continue the highest-priority project work.")
    cold.add_argument("--budget-bytes", type=_positive, default=64 * 1024)

    capture = commands.add_parser("capture")
    capture.add_argument("task_id")
    capture.add_argument("trace")
    capture.add_argument("--auto-commit", action="store_true")

    observe = commands.add_parser("observe")
    observe_commands = observe.add_subparsers(dest="observe_command", required=True)
    observe_start = observe_commands.add_parser("start")
    observe_start.add_argument("--session", required=True)
    observe_start.add_argument("--task", required=True)
    observe_append = observe_commands.add_parser("append")
    observe_append.add_argument("--session", required=True)
    observe_append.add_argument("--event-json", required=True)
    observe_finalize = observe_commands.add_parser("finalize")
    observe_finalize.add_argument("--session", required=True)

    skill = commands.add_parser("skill")
    skill_commands = skill.add_subparsers(dest="skill_command", required=True)
    skill_list = skill_commands.add_parser("list")
    skill_list.add_argument("--status")
    skill_show = skill_commands.add_parser("show")
    skill_show.add_argument("skill_id")
    skill_show.add_argument("--version", type=_positive)
    skill_revise = skill_commands.add_parser("revise")
    skill_revise.add_argument("skill_id")
    skill_revise.add_argument("changes")
    skill_revise.add_argument("--expected-version", required=True, type=_positive)
    skill_tested = skill_commands.add_parser("mark-tested")
    skill_tested.add_argument("skill_id")
    skill_tested.add_argument("version", type=_positive)
    skill_tested.add_argument("--evidence", required=True)
    skill_approve = skill_commands.add_parser("approve")
    skill_approve.add_argument("skill_id")
    skill_approve.add_argument("version", type=_positive)
    skill_approve.add_argument("--approval", required=True)
    skill_export = skill_commands.add_parser("export")
    skill_export.add_argument("skill_id")
    skill_export.add_argument("destination")
    skill_export.add_argument("--version", type=_positive)
    skill_import = skill_commands.add_parser("import")
    skill_import.add_argument("package")

    commands.add_parser("catalog")
    commands.add_parser("review-queue")
    commands.add_parser("verify")
    commands.add_parser("consolidate")

    backup = commands.add_parser("backup")
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_export = backup_commands.add_parser("export")
    backup_export.add_argument("destination")
    backup_restore = backup_commands.add_parser("restore")
    backup_restore.add_argument("package")
    backup_restore.add_argument("destination")

    metrics = commands.add_parser("metrics")
    metrics_commands = metrics.add_subparsers(dest="metrics_command", required=True)
    metrics_commands.add_parser("memory-quality")
    routing = metrics_commands.add_parser("routing")
    routing.add_argument("request")
    routing.add_argument("--expected-id", action="append", default=[])
    routing.add_argument("--repetitions", type=_positive, default=3)
    cold_benchmark = metrics_commands.add_parser("cold-start")
    cold_benchmark.add_argument("handoff")
    cold_benchmark.add_argument("manual_explanation")
    cold_benchmark.add_argument("--expected-id", action="append", default=[])
    zero_relearning = metrics_commands.add_parser("zero-relearning")
    zero_relearning.add_argument("context")
    zero_relearning.add_argument("observation")
    zero_relearning.add_argument("expectation")
    zero_relearning.add_argument(
        "--elapsed-ms", type=_non_negative_number, required=True
    )
    zero_relearning.add_argument(
        "--token-count", type=_non_negative_integer, required=True
    )
    memory_pollution = metrics_commands.add_parser("memory-pollution")
    memory_pollution.add_argument("input")
    metrics_commands.add_parser("lifecycle")
    conflict_resolution = metrics_commands.add_parser("conflict-resolution")
    conflict_resolution.add_argument("before")
    conflict_resolution.add_argument("after")
    context_quality = metrics_commands.add_parser("context-quality")
    context_quality.add_argument("context")
    context_quality.add_argument("observation")
    context_quality.add_argument("expectation")
    context_quality.add_argument(
        "--elapsed-ms", type=_non_negative_number, required=True
    )
    context_quality.add_argument(
        "--token-count", type=_non_negative_integer, required=True
    )
    context_reduction = metrics_commands.add_parser("context-reduction")
    context_reduction.add_argument("baseline_context")
    context_reduction.add_argument("baseline_observation")
    context_reduction.add_argument("candidate_context")
    context_reduction.add_argument("candidate_observation")
    context_reduction.add_argument("expectation")
    context_reduction.add_argument("thresholds")
    context_reduction.add_argument(
        "--baseline-elapsed-ms", type=_non_negative_number, required=True
    )
    context_reduction.add_argument(
        "--candidate-elapsed-ms", type=_non_negative_number, required=True
    )
    context_reduction.add_argument(
        "--baseline-token-count", type=_non_negative_integer, required=True
    )
    context_reduction.add_argument(
        "--candidate-token-count", type=_non_negative_integer, required=True
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    del stderr  # Operational failures are emitted as one JSON document on stdout.
    try:
        arguments = build_parser().parse_args(argv)
        if arguments.command == "backup" and arguments.backup_command == "restore":
            result = ProductService.restore_backup(arguments.package, arguments.destination)
            return _emit(output, True, "BACKUP_RESTORED", result)
        workspace = (
            Workspace.open(arguments.workspace)
            if arguments.workspace is not None
            else Workspace.discover(Path.cwd())
        )
        service = ProductService(workspace)
        try:
            result, code = _dispatch(service, arguments)
        finally:
            service.close()
        return _emit(output, True, code, result)
    except ProductCliUsageError as exc:
        return _emit(output, False, "USAGE_ERROR", message=str(exc), exit_code=EXIT_USAGE)
    except ProductError as exc:
        return _emit(
            output,
            False,
            exc.code,
            data=exc.data,
            message=exc.message,
            exit_code=_product_exit(exc.code),
        )
    except WorkspaceError as exc:
        return _emit(
            output,
            False,
            exc.code,
            message=exc.message,
            exit_code=EXIT_VALIDATION_ERROR,
        )
    except (OSError, PermissionError, json.JSONDecodeError) as exc:
        return _emit(output, False, "IO_ERROR", message=str(exc), exit_code=EXIT_IO_ERROR)
    except Exception as exc:  # pragma: no cover - stable last-resort boundary
        return _emit(
            output,
            False,
            "INTERNAL_ERROR",
            message=f"{type(exc).__name__}: {exc}",
            exit_code=EXIT_INTERNAL_ERROR,
        )


def _dispatch(service: ProductService, args: argparse.Namespace) -> tuple[Any, str]:
    if args.command == "describe":
        return service.describe(), "PRODUCT_DESCRIBED"
    if args.command == "ingest":
        return (
            service.ingest(
                _workspace_paths(service, args.paths),
                conversation_paths=_workspace_paths(service, args.conversation),
                include_code=not args.no_code,
            ),
            "INGEST_COMPLETED",
        )
    if args.command == "extract":
        return service.extract(args.batch_id), "EXTRACTION_COMPLETED"
    if args.command == "draft":
        if args.draft_command == "list":
            drafts = service.list_drafts(
                status=args.status, draft_kind=args.kind, batch_id=args.batch_id
            )
            return {"drafts": drafts, "count": len(drafts)}, "DRAFTS_LISTED"
        if args.draft_command == "show":
            return service.get_draft(args.draft_id), "DRAFT_SHOWN"
        if args.draft_command == "edit":
            return (
                service.edit_draft(
                    args.draft_id,
                    _load_json_argument(args.document),
                    expected_version=args.expected_version,
                ),
                "DRAFT_UPDATED",
            )
        if args.draft_command == "reject":
            return service.reject_draft(args.draft_id, rationale=args.rationale), "DRAFT_REJECTED"
        if args.draft_command == "commit":
            return service.commit_draft(args.draft_id), "DRAFT_COMMITTED"
        if args.draft_command == "commit-batch":
            return (
                service.commit_batch_drafts(
                    args.batch_id, deterministic_only=not args.include_model
                ),
                "DRAFT_BATCH_COMMITTED",
            )
        return {"expired": service.expire_drafts()}, "DRAFTS_EXPIRED"
    if args.command == "build":
        data: dict[str, Any] = {}
        if args.target in {"views", "all"}:
            data["views"] = service.build_memory_views()
        if args.target in {"indexes", "all"}:
            data["indexes"] = service.build_indexes()
        return data, "PRODUCT_VIEWS_BUILT"
    if args.command == "context":
        return (
            service.context(
                {
                    "task": args.task,
                    "purpose": args.purpose,
                    "query": args.query,
                    "references": args.references,
                    "depth": args.depth,
                    "budget_bytes": args.budget_bytes,
                    "budget_tokens": args.budget_tokens,
                    "hints": {},
                }
            ),
            "TASK_CONTEXT_READY",
        )
    if args.command == "search":
        return service.search(args.query, kinds=args.kinds, limit=args.limit), "SEARCH_COMPLETED"
    if args.command == "tool":
        return service.tool_call(args.name, _load_json_argument(args.arguments)), "TOOL_CALLED"
    if args.command == "cold-start":
        return (
            service.cold_start(
                _workspace_paths(service, args.paths),
                conversation_paths=_workspace_paths(service, args.conversation),
                auto_commit_deterministic=not args.no_auto_commit,
                task=args.task,
                budget_bytes=args.budget_bytes,
            ),
            "COLD_START_COMPLETED",
        )
    if args.command == "capture":
        trace_path = Path(args.trace).expanduser()
        candidate = (
            trace_path
            if trace_path.is_absolute()
            else service.workspace.root / trace_path
        )
        if candidate.is_file():
            resolved = candidate.resolve()
            try:
                resolved.relative_to(service.workspace.root.resolve())
            except ValueError as exc:
                raise ProductError(
                    "PATH_OUTSIDE_WORKSPACE",
                    f"Task trace path is outside the workspace: {resolved}",
                ) from exc
            trace: Any = resolved.read_text(encoding="utf-8")
        else:
            trace = args.trace
        return (
            service.post_task_capture(
                args.task_id, trace, auto_commit_deterministic=args.auto_commit
            ),
            "TASK_CAPTURED",
        )
    if args.command == "observe":
        from .observe import ObservationCapture

        capture = ObservationCapture(service.workspace)
        if args.observe_command == "start":
            return capture.start(args.session, args.task), "OBSERVATION_STARTED"
        if args.observe_command == "append":
            return (
                capture.append(args.session, _load_inline_json_object(args.event_json)),
                "OBSERVATION_APPENDED",
            )
        return capture.finalize(args.session, service), "OBSERVATION_FINALIZED"
    if args.command == "skill":
        if args.skill_command == "list":
            skills = service.store.list_skills(status=args.status)
            return {"skills": skills, "count": len(skills)}, "SKILLS_LISTED"
        if args.skill_command == "show":
            skill = service.store.get_skill(args.skill_id, version=args.version)
            if skill is None:
                raise ProductError("SKILL_NOT_FOUND", args.skill_id)
            return skill, "SKILL_SHOWN"
        if args.skill_command == "revise":
            return (
                service.revise_skill(
                    args.skill_id,
                    expected_version=args.expected_version,
                    changes=_load_json_argument(args.changes),
                ),
                "SKILL_REVISED",
            )
        if args.skill_command == "mark-tested":
            return (
                service.record_skill_test(
                    args.skill_id,
                    args.version,
                    evidence=_load_json_argument(args.evidence),
                ),
                "SKILL_TEST_RECORDED",
            )
        if args.skill_command == "approve":
            return (
                service.approve_skill(
                    args.skill_id,
                    args.version,
                    approval=_load_json_argument(args.approval),
                ),
                "SKILL_APPROVED",
            )
        if args.skill_command == "export":
            return (
                service.export_skill(
                    args.skill_id, args.destination, version=args.version
                ),
                "SKILL_EXPORTED",
            )
        return service.import_skill(args.package), "SKILL_IMPORTED"
    if args.command == "catalog":
        return service.catalog(), "CATALOG_READY"
    if args.command == "review-queue":
        return service.review_queue(), "REVIEW_QUEUE_READY"
    if args.command == "verify":
        report = service.verify()
        if not report["valid"]:
            raise ProductError("PRODUCT_INTEGRITY_INVALID", "Product verification failed.", data=report)
        return report, "PRODUCT_INTEGRITY_VALID"
    if args.command == "consolidate":
        return service.incremental_consolidation(), "CONSOLIDATION_COMPLETED"
    if args.command == "backup":
        return service.export_backup(args.destination), "BACKUP_EXPORTED"
    if args.command == "metrics":
        if args.metrics_command == "memory-quality":
            return service.memory_quality_metrics(), "MEMORY_QUALITY_READY"
        if args.metrics_command == "routing":
            return (
                service.context_routing_metrics(
                    _load_json_argument(args.request),
                    expected_ids=args.expected_id,
                    repetitions=args.repetitions,
                ),
                "ROUTING_METRICS_READY",
            )
        if args.metrics_command == "zero-relearning":
            return (
                service.evaluate_zero_relearning(
                    _load_workspace_json(service, args.context),
                    _load_workspace_json(service, args.observation),
                    _load_workspace_json(service, args.expectation),
                    elapsed_ms=args.elapsed_ms,
                    token_count=args.token_count,
                ),
                "ZERO_RELEARNING_EVALUATED",
            )
        if args.metrics_command == "memory-pollution":
            input_document = _load_workspace_json(service, args.input)
            memories = input_document.get("memories")
            expected_truth = input_document.get("expected_truth")
            if (
                isinstance(memories, (str, bytes))
                or not isinstance(memories, Sequence)
                or not isinstance(expected_truth, Mapping)
            ):
                raise ProductCliUsageError(
                    "memory-pollution input requires memories and expected_truth"
                )
            return (
                service.evaluate_memory_pollution(
                    memories,
                    expected_truth=expected_truth,
                    confident_threshold=float(
                        input_document.get("confident_threshold", 0.9)
                    ),
                ),
                "MEMORY_POLLUTION_EVALUATED",
            )
        if args.metrics_command == "lifecycle":
            return service.memory_lifecycle_inventory(), "MEMORY_LIFECYCLE_READY"
        if args.metrics_command == "conflict-resolution":
            return (
                service.evaluate_conflict_resolution(
                    _load_workspace_json(service, args.before),
                    _load_workspace_json(service, args.after),
                ),
                "CONFLICT_RESOLUTION_EVALUATED",
            )
        if args.metrics_command == "context-quality":
            return (
                service.evaluate_context_quality(
                    _load_workspace_json(service, args.context),
                    _load_workspace_json(service, args.observation),
                    _load_workspace_json(service, args.expectation),
                    elapsed_ms=args.elapsed_ms,
                    token_count=args.token_count,
                ),
                "CONTEXT_QUALITY_EVALUATED",
            )
        if args.metrics_command == "context-reduction":
            return (
                service.evaluate_paired_context_reduction(
                    _load_workspace_json(service, args.baseline_context),
                    _load_workspace_json(service, args.baseline_observation),
                    _load_workspace_json(service, args.candidate_context),
                    _load_workspace_json(service, args.candidate_observation),
                    _load_workspace_json(service, args.expectation),
                    _load_workspace_json(service, args.thresholds),
                    baseline_elapsed_ms=args.baseline_elapsed_ms,
                    candidate_elapsed_ms=args.candidate_elapsed_ms,
                    baseline_token_count=args.baseline_token_count,
                    candidate_token_count=args.candidate_token_count,
                ),
                "CONTEXT_REDUCTION_EVALUATED",
            )
        return (
            service.cold_start_benchmark(
                _load_json_argument(args.handoff),
                manual_explanation=Path(args.manual_explanation).read_text(encoding="utf-8"),
                expected_ids=args.expected_id,
            ),
            "COLD_START_METRICS_READY",
        )
    raise ProductCliUsageError(f"Unsupported command: {args.command}")


def _workspace_paths(service: ProductService, values: Sequence[str]) -> list[Path]:
    """Resolve CLI ingest inputs relative to the selected workspace."""

    resolved: list[Path] = []
    for value in values:
        path = Path(value).expanduser()
        resolved.append(path if path.is_absolute() else service.workspace.root / path)
    return resolved


def _load_workspace_json(service: ProductService, value: str) -> Mapping[str, Any]:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = service.workspace.root / path
    parsed = service.workspace.load_json(path)
    if not isinstance(parsed, Mapping):
        raise ProductCliUsageError("JSON argument must be an object")
    return parsed


def _load_json_argument(value: str) -> Mapping[str, Any]:
    path = Path(value)
    text = path.read_text(encoding="utf-8") if path.is_file() else value
    parsed = json.loads(text)
    if not isinstance(parsed, Mapping):
        raise ProductCliUsageError("JSON argument must be an object")
    return parsed


def _load_inline_json_object(value: str) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise ProductCliUsageError("JSON argument must be an object")
    return parsed


def _product_exit(code: str) -> int:
    if "INTEGRITY" in code or "HASH_MISMATCH" in code or "AUDIT" in code:
        return EXIT_INTEGRITY_ERROR
    return EXIT_VALIDATION_ERROR


def _emit(
    output: TextIO,
    ok: bool,
    code: str,
    data: Any | None = None,
    *,
    message: str | None = None,
    exit_code: int = EXIT_OK,
) -> int:
    document: dict[str, Any] = {"ok": ok, "code": code}
    if data is not None:
        document["data"] = data
    if message is not None:
        document["message"] = message
    output.write(canonical_json(document) + "\n")
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
