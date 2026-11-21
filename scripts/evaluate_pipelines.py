#!/usr/bin/env python3
"""Compare sequential, single-pass and parallel Fidelis review schedulers against local benchmark diffs.

Run from the repository root after setting the LLM_* environment variables:
    python scripts/evaluate_pipelines.py --repeat 2

This is an experiment harness, not a claim of objective code-review accuracy.
Expected categories are human-labelled fixture expectations; inspect each JSON
result and false positive before drawing conclusions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from statistics import mean
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
REVIEWER_DIR = ROOT / "services" / "reviewer"
sys.path.insert(0, str(REVIEWER_DIR))

from app.config import Settings  # noqa: E402
from app.reviewers.llm import LLMReviewer  # noqa: E402
from app.schemas import ReviewAgent, ReviewRequest, ReviewerMode  # noqa: E402


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


async def _run_case(settings: Settings, case: dict, run_number: int) -> dict:
    reviewer = LLMReviewer(settings)
    request = ReviewRequest(
        job_id=f"eval-{case['id']}-{run_number}-{uuid4().hex[:8]}",
        repository=case["repository"], pr_number=case["pr_number"],
        commit_sha=uuid4().hex, diff=case["diff"],
    )
    started = time.perf_counter()
    result = await reviewer.review(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    expected = set(case["expected_categories"])
    predicted = {finding.category.value for finding in result.findings}
    true_positive = len(expected & predicted)
    false_positive = len(predicted - expected)
    false_negative = len(expected - predicted)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else (1.0 if not expected else 0.0)
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "case_id": case["id"], "run": run_number,
        "latency_ms": round(elapsed_ms, 2), "expected_categories": sorted(expected),
        "predicted_categories": sorted(predicted), "true_positive_categories": sorted(expected & predicted),
        "false_positive_categories": sorted(predicted - expected),
        "missed_categories": sorted(expected - predicted),
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "finding_count": result.summary.total_findings,
        "agents_run": [agent.value for agent in result.agents_run],
        "agent_runs": [run.model_dump(mode="json") for run in result.agent_runs],
        "execution": result.execution.model_dump(mode="json") if result.execution else None,
        "findings": [finding.model_dump(mode="json") for finding in result.findings],
    }


async def _main(args) -> dict:
    cases = json.loads((ROOT / "evaluation" / "benchmark_cases.json").read_text(encoding="utf-8"))
    base = Settings.from_env()
    if not base.llm_base_url:
        raise SystemExit("LLM_BASE_URL is empty")
    if not base.llm_api_key and "localhost" not in base.llm_base_url and "127.0.0.1" not in base.llm_base_url and "host.docker.internal" not in base.llm_base_url:
        print("Warning: no LLM_API_KEY set; this only works if the chosen endpoint does not require one.", file=sys.stderr)

    output: dict = {"warning": "Benchmark labels are simplified human expectations, not ground-truth vulnerability adjudication.", "models": {}}
    for mode in args.modes.split(","):
        mode = mode.strip()
        if mode not in {"sequential", "single_pass", "parallel"}:
            raise SystemExit(f"Unsupported strategy: {mode}. Choose sequential, single_pass or parallel.")
        # These fixture repositories/SHAs are synthetic. Disable GitHub source
        # retrieval and checkpoints so the benchmark measures only the labelled
        # fixture inputs, not external repository state or previous runs.
        settings = replace(
            base,
            mode=ReviewerMode.LLM,
            review_agents=(ReviewAgent.STATIC_ANALYSIS, ReviewAgent.SECURITY, ReviewAgent.ARCHITECTURE, ReviewAgent.CODE_STYLE),
            execution_strategy=mode,
            checkpoint_enabled=False,
            github_token="",
            worker_status_token="",
        )
        runs = []
        for case in cases:
            for run_number in range(1, args.repeat + 1):
                print(f"[{mode}] {case['id']} · run {run_number}/{args.repeat}")
                runs.append(await _run_case(settings, case, run_number))
        latencies = [row["latency_ms"] for row in runs]
        output["models"][mode] = {
            "model": base.llm_model, "repeat": args.repeat, "case_count": len(cases),
            "runs": runs,
            "summary": {
                "mean_latency_ms": round(mean(latencies), 2) if latencies else None,
                "p50_latency_ms": _percentile(latencies, 0.50),
                "p95_latency_ms": _percentile(latencies, 0.95),
                "mean_precision": round(mean(row["precision"] for row in runs), 4) if runs else None,
                "mean_recall": round(mean(row["recall"] for row in runs), 4) if runs else None,
                "mean_f1": round(mean(row["f1"] for row in runs), 4) if runs else None,
                "false_positive_cases": sum(bool(row["false_positive_categories"]) for row in runs),
                "missed_expected_categories": sum(len(row["missed_categories"]) for row in runs),
                "total_findings": sum(row["finding_count"] for row in runs),
                "total_input_tokens": sum((row.get("execution") or {}).get("input_tokens") or 0 for row in runs),
                "total_output_tokens": sum((row.get("execution") or {}).get("output_tokens") or 0 for row in runs),
                "total_llm_requests": sum((row.get("execution") or {}).get("llm_requests") or 0 for row in runs),
                "slowest_agent_by_mean_runtime_ms": _slowest_agent(runs),
            },
        }
    return output


def _slowest_agent(runs: list[dict]) -> dict | None:
    timings: dict[str, list[int]] = {}
    for row in runs:
        for agent in row["agent_runs"]:
            if agent.get("shared_call"):
                continue
            timings.setdefault(agent["agent"], []).append(agent["duration_ms"])
    if not timings:
        return None
    return {"agent": max(timings, key=lambda key: mean(timings[key])), "mean_ms": round(max(mean(v) for v in timings.values()), 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=1, help="Runs per fixture and pipeline (default: 1)")
    parser.add_argument(
        "--modes",
        default="sequential,single_pass",
        help="Comma-separated: sequential,single_pass,parallel (parallel is intended for capable inference servers)",
    )
    parser.add_argument("--output", default="evaluation/results.json")
    args = parser.parse_args()
    if args.repeat < 1 or args.repeat > 10:
        parser.error("--repeat must be between 1 and 10")
    result = asyncio.run(_main(args))
    path = ROOT / args.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"\nResults written to {path.relative_to(ROOT)}")
    for mode, details in result["models"].items():
        print(mode, json.dumps(details["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
