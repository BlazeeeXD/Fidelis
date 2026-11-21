# GitHub review delivery troubleshooting

## Token permissions

`GITHUB_TOKEN` being present does not mean that it can write comments. Fidelis uses the same token for reading pull-request diffs/source files and publishing the final report. For a fine-grained personal access token:

- Repository access must include the exact target repository (for example `BlazeeeXD/fidelis_lab`).
- `Metadata`: read (GitHub requires this for repository access).
- `Contents`: read and `Pull requests`: read for diff/source retrieval.
- For the consolidated PR report comment, grant **either** `Issues: Read and write` **or** `Pull requests: Read and write`.

GitHub documents both permission alternatives for `POST /repos/{owner}/{repo}/issues/{issue_number}/comments`:
https://docs.github.com/en/rest/issues/comments#create-an-issue-comment

The response header `X-Accepted-GitHub-Permissions` describes the permission sets accepted by a rejected endpoint. Fidelis logs this header when publication is forbidden.

After changing the token value in the root `.env` (the same directory as `docker-compose.yml`), recreate the worker so Compose injects the new value:

```cmd
docker compose up -d --force-recreate review-worker
```

The worker uses the token at process startup. `docker compose exec review-worker ...` can verify the token is loaded, but a non-empty token alone does not prove write permission.

## Republish an already-completed review

A GitHub delivery failure does not erase the completed/persisted review result. After fixing token permissions, republish the stored result without paying for another model run:

```cmd
docker compose exec review-worker python -m app.worker.republish --job-id YOUR_COMPLETED_JOB_ID
```

The command updates the existing Fidelis issue comment when available, otherwise creates the report. It records a `github_review_published` delivery event and does not rerun the reviewer or LLM.

## Understand job status

A job may be `COMPLETED` while its separate GitHub delivery event is `github_review_failed`. Review computation and external publication are intentionally tracked separately. Inspect the job's `github_review_failed` audit event and worker logs for the API response and accepted-permissions header.
