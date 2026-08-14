"""Run every unittest file in an isolated process and combine branch coverage.

The repository contains durability and multi-process acceptance tests that are
safer and substantially faster when test modules run in separate interpreters.
Process-heavy modules run in an exclusive lane after the normal parallel lane
so they do not compete with one another for SQLite locks or CPU scheduling.
This runner preserves a complete per-file log while still enforcing one
combined coverage threshold.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


_EXCLUSIVE_TEST_FILES = frozenset(
    {
        "test_multi_client_acceptance.py",
        "test_process_durability.py",
        "test_concurrency.py",
    }
)


@dataclass(frozen=True)
class TestResult:
    path: Path
    returncode: int
    test_count: int
    seconds: float
    log_path: Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tests", type=Path, default=Path("tests"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--fail-under", type=int, default=80)
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(".ci/test-results/per-file"),
    )
    parser.add_argument(
        "--summary-log",
        type=Path,
        default=Path(".ci/test-results/unittest.log"),
    )
    parser.add_argument(
        "--pattern",
        default="test*.py",
        help="glob used recursively below the tests directory",
    )
    return parser.parse_args()


def _safe_log_name(path: Path) -> str:
    return path.as_posix().replace("/", "__").removesuffix(".py") + ".log"


def _run_test_file(
    *,
    root: Path,
    tests_root: Path,
    test_path: Path,
    log_dir: Path,
    timeout: int,
) -> TestResult:
    relative = test_path.relative_to(root)
    log_path = log_dir / _safe_log_name(relative)
    started = time.monotonic()
    environment = dict(os.environ)
    source_root = root / "src"
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root)
        if not existing_pythonpath
        else os.pathsep.join((str(source_root), existing_pythonpath))
    )
    command = [
        sys.executable,
        "-m",
        "coverage",
        "run",
        "--parallel-mode",
        "-m",
        "unittest",
        "discover",
        "-s",
        str(tests_root.relative_to(root)),
        "-p",
        test_path.name,
        "-v",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = completed.stdout
        returncode = completed.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        output = f"{stdout}\nTIMEOUT after {timeout} seconds\n"
        returncode = 124
    log_path.write_text(output, encoding="utf-8")
    match = re.search(r"Ran (\d+) tests?", output)
    return TestResult(
        path=relative,
        returncode=returncode,
        test_count=int(match.group(1)) if match else 0,
        seconds=time.monotonic() - started,
        log_path=log_path,
    )


def _run_command(root: Path, command: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout


def _record_result(
    result: TestResult,
    *,
    completed_count: int,
    total_count: int,
    results: list[TestResult],
    summary_lines: list[str],
) -> None:
    results.append(result)
    line = (
        f"[{completed_count:02d}/{total_count}] "
        f"{'PASS' if result.returncode == 0 else 'FAIL'} "
        f"{result.path.as_posix()} "
        f"({result.test_count} tests, {result.seconds:.3f}s)"
    )
    print(line, flush=True)
    summary_lines.append(line)


def main() -> int:
    args = _parse_args()
    root = args.root.resolve()
    tests_root = (root / args.tests).resolve()
    log_dir = (root / args.log_dir).resolve()
    summary_log = (root / args.summary_log).resolve()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")
    if args.timeout < 1:
        raise SystemExit("--timeout must be at least 1")
    if not tests_root.is_dir() or root not in tests_root.parents:
        raise SystemExit(f"invalid tests directory: {tests_root}")

    test_files = sorted(tests_root.rglob(args.pattern))
    if not test_files:
        raise SystemExit(f"no tests matched {args.pattern!r} below {tests_root}")
    duplicate_names = sorted(
        name
        for name in {path.name for path in test_files}
        if sum(candidate.name == name for candidate in test_files) > 1
    )
    if duplicate_names:
        raise SystemExit(
            "test filenames must be unique for unittest discovery: "
            + ", ".join(duplicate_names)
        )

    parallel_files = [
        path for path in test_files if path.name not in _EXCLUSIVE_TEST_FILES
    ]
    exclusive_files = [
        path for path in test_files if path.name in _EXCLUSIVE_TEST_FILES
    ]

    log_dir.mkdir(parents=True, exist_ok=True)
    summary_log.parent.mkdir(parents=True, exist_ok=True)
    for coverage_path in root.glob(".coverage*"):
        coverage_path.unlink()

    started = time.monotonic()
    results: list[TestResult] = []
    summary_lines: list[str] = []
    completed_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                _run_test_file,
                root=root,
                tests_root=tests_root,
                test_path=test_path,
                log_dir=log_dir,
                timeout=args.timeout,
            ): test_path
            for test_path in parallel_files
        }
        for future in concurrent.futures.as_completed(futures):
            completed_count += 1
            _record_result(
                future.result(),
                completed_count=completed_count,
                total_count=len(test_files),
                results=results,
                summary_lines=summary_lines,
            )

    # These modules intentionally exercise multiple OS processes and SQLite
    # writers.  Running them alone prevents unrelated parallel test workers from
    # turning scheduler contention into spurious database-lock failures.
    for test_path in exclusive_files:
        completed_count += 1
        _record_result(
            _run_test_file(
                root=root,
                tests_root=tests_root,
                test_path=test_path,
                log_dir=log_dir,
                timeout=args.timeout,
            ),
            completed_count=completed_count,
            total_count=len(test_files),
            results=results,
            summary_lines=summary_lines,
        )

    failures = [result for result in results if result.returncode != 0]
    if not failures:
        combine_status, combine_output = _run_command(
            root, [sys.executable, "-m", "coverage", "combine"]
        )
        print(combine_output, end="", flush=True)
        summary_lines.append(combine_output.rstrip())
        if combine_status != 0:
            failures.append(
                TestResult(Path("coverage-combine"), combine_status, 0, 0.0, summary_log)
            )
        else:
            report_status, report_output = _run_command(
                root,
                [
                    sys.executable,
                    "-m",
                    "coverage",
                    "report",
                    f"--fail-under={args.fail_under}",
                ],
            )
            print(report_output, end="", flush=True)
            summary_lines.append(report_output.rstrip())
            if report_status != 0:
                failures.append(
                    TestResult(
                        Path("coverage-report"),
                        report_status,
                        0,
                        0.0,
                        summary_log,
                    )
                )

    total_line = (
        f"TOTAL files={len(results)} "
        f"tests={sum(result.test_count for result in results)} "
        f"failures={len(failures)} "
        f"seconds={time.monotonic() - started:.3f}"
    )
    print(total_line, flush=True)
    summary_lines.append(total_line)
    for failure in failures:
        failure_line = f"FAILED {failure.path.as_posix()} -> {failure.log_path}"
        print(failure_line, flush=True)
        summary_lines.append(failure_line)
    summary_log.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
