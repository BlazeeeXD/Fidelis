# Fidelis security notes

## Authentication and trust boundaries

- GitHub webhook requests are authenticated using HMAC SHA-256 over the raw request body and `GITHUB_WEBHOOK_SECRET`. The delivery ID is tracked separately for replay/deduplication.
- Browser-to-Gateway APIs use JWTs. Development token creation and demo event injection are disabled when `ENV=production`.
- Internal worker callbacks and Learner write operations require `X-Worker-Token`. Set the same high-entropy value in all relevant containers.
- The internal Reviewer and Learner APIs should not be exposed to untrusted networks. The compose port mappings are for local development.

## Secrets

Never commit `.env` or credentials in review fixtures. Use fine-grained GitHub permissions. Keep API keys outside source control and rotate any token that appears in a pull request. Change the default JWT, webhook, worker, database and Grafana passwords before use beyond a local sandbox.

## LLM review limitations

PR diffs, source excerpts and repository rules are untrusted input. Prompts instruct the model to treat diff content as data, not instructions; however, prompt injection is not solved by wording alone. Validate all model output, do not execute generated code, and require a developer to confirm findings. The deterministic scanner covers a few high-signal patterns and is not a complete SAST product.

## GitHub write access

`PUBLISH_GITHUB_REVIEWS` is enabled in the sample local configuration. It creates or updates one marked PR issue comment; set it to `false` to disable the external write. Grant the GitHub token Issues: Read and write (plus Pull requests: Read and Contents: Read for source retrieval). Delivery status is recorded separately in the audit trail.

## Telemetry

Langfuse is opt-in. The code records agent/model metadata, run timing and token usage and avoids passing source diffs or prompts as trace input. Still evaluate retention, access controls, hosting region and organizational policy before enabling third-party telemetry.

## Production gap

The project has no Alembic migration history, secret manager integration, rate-limiting policy, multi-tenant authorization model, time-based stale-job lease reaper, or deployment-specific network policy. Those are prerequisites for a serious production service, not optional polish.
