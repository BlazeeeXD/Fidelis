#!/usr/bin/env python3
"""Bounded async load test for the stateless Reviewer HTTP endpoint.

Example:
    python scripts/load_test.py --requests 100 --concurrency 8

Defaults to mock mode expectations; use caution with a paid LLM endpoint because
every request can incur model cost. No GitHub or Orchestrator jobs are created.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from statistics import mean
from uuid import uuid4

import httpx

DIFF = """diff --git a/app/math.py b/app/math.py\nindex 1111111..2222222 100644\n--- a/app/math.py\n+++ b/app/math.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n+    return a + b\n"""


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))], 2)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8003/review")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    if not 1 <= args.requests <= 5000 or not 1 <= args.concurrency <= 100:
        parser.error("--requests must be 1..5000 and --concurrency must be 1..100")
    semaphore = asyncio.Semaphore(args.concurrency)
    timings: list[float] = []
    failures: list[str] = []
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        async def one(index: int):
            payload = {
                "schema_version": "1.3", "job_id": f"load-{index}-{uuid4().hex[:8]}",
                "repository": "fidelis-load-test/fixtures", "pr_number": index + 1,
                "commit_sha": uuid4().hex, "diff": DIFF,
            }
            async with semaphore:
                started = time.perf_counter()
                try:
                    response = await client.post(args.url, json=payload)
                    elapsed = (time.perf_counter() - started) * 1000
                    if response.is_success:
                        timings.append(elapsed)
                    else:
                        failures.append(f"{index}: HTTP {response.status_code} {response.text[:180]}")
                except httpx.HTTPError as exc:
                    failures.append(f"{index}: {type(exc).__name__}: {exc}")
        started = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(args.requests)))
        total_seconds = time.perf_counter() - started
    summary = {
        "requested": args.requests, "successful": len(timings), "failed": len(failures),
        "concurrency": args.concurrency, "wall_seconds": round(total_seconds, 2),
        "throughput_requests_per_second": round(len(timings) / total_seconds, 2) if total_seconds else 0,
        "mean_latency_ms": round(mean(timings), 2) if timings else None,
        "p50_latency_ms": percentile(timings, .50), "p95_latency_ms": percentile(timings, .95),
        "p99_latency_ms": percentile(timings, .99), "sample_errors": failures[:10],
    }
    print(json.dumps(summary, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
