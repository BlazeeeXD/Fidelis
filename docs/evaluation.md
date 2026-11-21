# Fidelis evaluation protocol

The roadmap calls for a comparison between a single LLM reviewer and four specialists, with findings, category coverage, false positives, latency and consistency measured. `evaluation/benchmark_cases.json` and `scripts/evaluate_pipelines.py` provide a repeatable starting point.

## Run

Configure `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` and related variables in the shell, then run:

```bash
python scripts/evaluate_pipelines.py --repeat 3 --output evaluation/results.json
```

The script compares `sequential` and `single_pass` by default against seven local diff fixtures; `parallel` can be added with `--modes sequential,single_pass,parallel` when the inference endpoint supports concurrency. It records category precision/recall/F1, false-positive categories, missed expected categories, mean/p50/p95 latency, pipeline-level token usage, model request counts, per-agent runs and the slowest mean independent-agent runtime. The repeat parameter permits review-consistency comparisons.

## Interpretation cautions

- The expected categories are simplified human labels, not adjudicated ground truth. A category-only metric does not assess severity, location correctness, exploitability, or whether the proposed fix is correct.
- One fixture can contain several issues, and model outputs can express the same issue in multiple categories. Inspect individual findings and manually adjudicate them.
- Token counts depend on provider accounting; local endpoints may omit usage.
- Latency depends on the model, provider, network, concurrency and machine. Compare runs under the same conditions.
- `sequential` and `parallel` each make one request per selected specialist. `single_pass` makes one request containing all specialist perspectives; one generation is not equivalent to four independently sampled agents. Compare the actual quality gain against latency and token cost rather than assuming either approach is better.

For the V9 concurrency experiment, run the stateless HTTP load test in mock mode first:

```bash
python scripts/load_test.py --requests 100 --concurrency 8
```

It posts synthetic diffs directly to the Reviewer API; it does not create jobs or call GitHub. When LLM mode is enabled, every request may incur model cost. Record hardware/container limits, worker concurrency, model identifier and environment alongside the results.
