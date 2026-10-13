# GitHub Webhook Events Contract (Week 2)

Owner: P2 (Webhook Service). Covers `POST /webhook/github`, served by the
Webhook Service (port 8002) and exposed through the Gateway.

## Configuring the GitHub webhook

1. In the GitHub repo: **Settings → Webhooks → Add webhook**.
2. **Payload URL**: `<public host>/webhook/github` — the Gateway's public
   host, e.g. `https://demo.example.com/webhook/github`. The Gateway
   routes `/webhook/github` to the Webhook Service.
3. **Content type**: `application/json`
   (`application/x-www-form-urlencoded` is also accepted; the body is
   `payload=<urlencoded json>`).
4. **Secret**: the same value as the service's `GITHUB_WEBHOOK_SECRET`
   environment variable. Never commit the real secret.
5. **Events**: choose "Let me select individual events" and subscribe to
   **Pull requests** only.
6. **Active** the hook and save. GitHub sends a `ping` event to confirm;
   we answer `{"status":"pong"}`.

Headers we read on every delivery:

| Header | Purpose |
|---|---|
| `X-GitHub-Event` | Event name (`pull_request`, `ping`, ...) |
| `X-Hub-Signature-256` | `sha256=<hex>` HMAC-SHA256 of the **raw body bytes** |
| `X-GitHub-Delivery` | Unique delivery ID (idempotency key) |

The signature is verified against the raw body bytes **first**, before any
parsing or content-type handling. The secret and the request body are never
logged.

## Event / action → response

| Event | Action | HTTP | Response body |
|---|---|---|---|
| `ping` | – | 200 | `{"status":"pong"}` |
| other event (`push`, `issues`, …) | – | 200 | `{"status":"ignored","event":"<event>"}` |
| `pull_request` | `opened` / `synchronize` / `reopened` | 200 | `{"status":"queued","pr":<n>,"repo":"<owner/repo>","job_id":"<uuid>","duplicate":<bool>}` |
| `pull_request` | anything else (`closed`, `labeled`, …) | 200 | `{"status":"ignored","action":"<action>"}` |
| any | delivery ID already seen | 200 | `{"status":"duplicate_ignored","delivery_id":"<id>"}` |

Only `opened`, `synchronize`, `reopened` (`REVIEW_TRIGGER_ACTIONS` in
`services/webhook/app/schemas.py`) go through full payload validation and are
forwarded to the Orchestrator. Non-trigger actions are judged on the `action`
field alone, so an irrelevant action with an unusual payload shape is ignored
(200), not rejected (422).

## HTTP status codes

| Code | Meaning |
|---|---|
| 401 | Missing or invalid `X-Hub-Signature-256` — wrong secret, or a body re-serialized after signing. Nothing was processed; no job was created. |
| 415 | Unsupported `Content-Type`. Accepted: `application/json` and `application/x-www-form-urlencoded`. |
| 400 | Signature was valid but the delivery is malformed: empty body, invalid JSON, a non-object JSON payload, or a form body without a `payload` field. |
| 422 | Trigger action (`opened`/`synchronize`/`reopened`) but the payload does not match the expected `pull_request` shape. |
| 500 | Server missing `GITHUB_WEBHOOK_SECRET`, or the Orchestrator rejected the job with a 4xx (a payload bug on our side, not an outage). |
| 502 | Orchestrator unreachable or returning 5xx after retries. The delivery ID is forgotten so a GitHub redelivery is processed. |

Any non-2xx counts as a failed delivery; GitHub redelivers on its own retry
schedule.

## What gets sent to the Orchestrator

For trigger actions only, after full validation — `POST <ORCHESTRATOR_URL>/jobs`
(see [review-job-contract.md](review-job-contract.md)):

| Webhook payload field | Job field |
|---|---|
| `repository.full_name` (`repo_full_name`) | `repository` |
| `pull_request.number` (`pr_number`) | `pr_number` |
| `pull_request.head.sha` (`head_sha`) | `commit_sha` |

## Idempotency

Seen `X-GitHub-Delivery` IDs are kept in a bounded in-memory LRU
(default 10000 entries, env `WEBHOOK_DEDUPE_MAX`). A repeat delivery ID gets
`duplicate_ignored`. IDs are forgotten when the Orchestrator call fails, so a
GitHub redelivery after a 500/502 is processed again. The LRU is per process
and cleared on restart; the Orchestrator's own idempotency on
(repository, pr_number, commit_sha) remains the second line of defence.

## Logging

Exactly one INFO line per delivery:

```
delivery delivery_id=<id> event=<event> action=<action> repo=<owner/repo> pr_number=<n> head_sha=<sha> outcome=<outcome> detail=<detail>
```

`outcome` is one of `queued`, `duplicate`, `ignored`, `rejected`. Rejected
signatures (missing secret, missing/invalid signature, empty body) are also
logged at WARNING. The secret and the request body are never logged.
