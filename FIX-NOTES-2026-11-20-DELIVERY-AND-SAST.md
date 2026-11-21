# Fidelis follow-up fix — 20 November 2026

## What the latest production-like run established

- The reviewer returned HTTP 200 and the result was persisted as `COMPLETED`.
- All four specialist passes ran sequentially.
- GitHub reads of the PR comment list succeeded, but creating the issue comment returned HTTP 403 with `Resource not accessible by personal access token`.
- This is a GitHub token permission/authorization issue, not an LLM, Docker, ngrok, webhook, or model-networking failure. A populated `GITHUB_TOKEN` environment variable does not imply write permission.
- The 4 specialist runs returned empty findings. The existing deterministic scanner had no rules for the SQL interpolation, `os.system`, direct path opening, or deleted admin guard in the supplied test case.

## Changes in this package

1. Added deterministic high-signal checks for:
   - SQL statements dynamically assembled using f-strings, `.format`, percent interpolation, or string concatenation.
   - Dynamic commands passed to `os.system` or `os.popen`.
   - Path-like parameters passed directly to `open()` / `Path(...).read_text()` without a visible path-containment check.
   - A deleted administrator/permission guard combined with a role/privilege-changing operation in the same changed file.
2. Kept these checks as a deterministic supplement to the LLM. They do not replace full data-flow/static analysis and may need tuning for other repositories.
3. Added a GitHub PR-review publication fallback when issue-comment creation is forbidden.
4. Added GitHub's `X-Accepted-GitHub-Permissions` response header to publication diagnostics.
5. Delivery audit messages now retain the actual bounded error explanation.
6. Added `python -m app.worker.republish --job-id ...` to publish an already-persisted result without rerunning the model.

## Verification

- Reviewer: 288 passed, 1 skipped.
- Orchestrator: 35 passed.
- Webhook: 27 passed.
- Learner: 2 passed.
- Gateway: 11 passed.
- Total: 363 passed, 1 skipped.
- Python compilation passed.
- Docker Compose CLI validation was not run in this build environment because the Docker CLI is unavailable.
- A live GitHub write was not attempted because the user's PAT is not available in this environment.

## Required user-side permission

For a fine-grained PAT restricted to the target repository, grant either:

- `Issues: Read and write`, or
- `Pull requests: Read and write`.

It must include the exact private repository. After replacing the token in the root `.env`, recreate the worker with:

```cmd
docker compose up -d --force-recreate review-worker
```

The previous failed delivery remains separate from the completed review. Use the republish command only when the saved result is worth publishing. To test the new deterministic checks, trigger a new commit after installing this package.
