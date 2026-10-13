"""``python -m app.worker.demo`` — print a mock review execution."""

import argparse
import sys

from app.reviewers import MockScenario
from app.worker.mock_execution import run_mock_execution


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a mock review through the worker lifecycle.")
    parser.add_argument(
        "--scenario",
        choices=[s.value for s in MockScenario],
        default=MockScenario.MULTI_AGENT.value,
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--flaky-failures", type=int, default=2)
    args = parser.parse_args(argv)

    report = run_mock_execution(
        MockScenario(args.scenario),
        max_attempts=args.max_attempts,
        flaky_failures=args.flaky_failures,
    )

    print(f"job {report.request.job_id}  scenario={args.scenario}  max_attempts={args.max_attempts}")
    for outcome in report.outcomes:
        line = f"  attempt {outcome.attempt}/{outcome.max_attempts}: {outcome.disposition.value}"
        if outcome.result is not None:
            summary = outcome.result.summary
            line += f" ({summary.total_findings} finding(s), highest={summary.highest_severity})"
        if outcome.error is not None:
            line += f" [{outcome.error.code.value}: {outcome.error.message}]"
        if outcome.retry_after_seconds is not None:
            line += f" -> retry in {outcome.retry_after_seconds:g}s"
        print(line)
    print("  status path: " + " -> ".join(report.transitions))
    print(f"  final status: {report.final_status.value}")
    return 0 if report.final_status.value == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
