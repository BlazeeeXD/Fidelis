"""Publish one maintained, consolidated Fidelis report as a GitHub PR comment."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx

from app.schemas import ReviewRequest, ReviewResult

logger = logging.getLogger("reviewer.github_reviews")
_MARKER = "<!-- fidelis-review-report -->"


class GitHubReviewPublisher:
    """Upsert a single issue comment per pull request instead of spamming inline notes."""

    def __init__(self, token: str, *, api_url: str = "https://api.github.com", timeout: float = 20.0):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    async def publish(self, request: ReviewRequest, result: ReviewResult) -> dict:
        if not self.token:
            return {"published": False, "reason": "GITHUB_TOKEN is not configured"}
        repo = quote(request.repository, safe="/")
        comments_url = f"{self.api_url}/repos/{repo}/issues/{request.pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Fidelis-PR-Reviewer",
        }
        body = self._report_body(result)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            existing = await self._find_existing(client, comments_url, headers)
            if existing:
                comment_id = existing.get("id")
                response = await self._request_with_retry(
                    client, "PATCH", f"{self.api_url}/repos/{repo}/issues/comments/{comment_id}",
                    headers=headers, json={"body": body},
                )
                action = "updated"
            else:
                response = await self._request_with_retry(
                    client, "POST", comments_url, headers=headers, json={"body": body},
                )
                action = "created"

            # Some fine-grained tokens are configured for Pull requests: write
            # but not Issues: write. GitHub documents either permission for
            # issue comments; the PR-review endpoint is also a useful fallback
            # and reports its own accepted-permissions header when forbidden.
            if response.status_code == 403:
                fallback = await self._publish_as_pr_review(
                    client, repo=repo, pr_number=request.pr_number,
                    commit_sha=result.commit_sha, body=body, headers=headers,
                )
                if fallback is not None:
                    return fallback
                required = response.headers.get("X-Accepted-GitHub-Permissions", "not supplied")
                raise RuntimeError(
                    "GitHub rejected PR comment creation (HTTP 403). The worker token is valid for reads but lacks "
                    "a write permission for this repository. Grant Issues: Read and write OR Pull requests: Read and write, "
                    f"ensure this token is authorized for {request.repository}, then recreate review-worker. "
                    f"GitHub required-permissions header: {required}. Response: {response.text[:300]}"
                )

        if not response.is_success:
            required = response.headers.get("X-Accepted-GitHub-Permissions", "not supplied")
            raise RuntimeError(
                f"GitHub PR comment returned HTTP {response.status_code}; accepted permissions={required}: {response.text[:500]}"
            )
        data = response.json()
        return {
            "published": True,
            "action": action,
            "method": "issue_comment",
            "comment_id": data.get("id"),
            "html_url": data.get("html_url"),
            "comment_url": data.get("html_url"),
            "inline_comments": 0,
        }

    async def _publish_as_pr_review(
        self, client: httpx.AsyncClient, *, repo: str, pr_number: int,
        commit_sha: str, body: str, headers: dict,
    ) -> dict | None:
        """Fallback to a submitted PR review when issue-comment publishing is forbidden.

        The fallback is idempotent for the same commit: it checks existing PR
        reviews for the Fidelis marker before creating another review. A new
        commit may receive a new review, preserving the audit trail.
        """
        reviews_url = f"{self.api_url}/repos/{repo}/pulls/{pr_number}/reviews"
        try:
            listed = await self._request_with_retry(client, "GET", reviews_url, headers=headers, params={"per_page": 100})
            if listed.is_success:
                reviews = listed.json()
                if isinstance(reviews, list):
                    for review in reviews:
                        if not isinstance(review, dict) or _MARKER not in str(review.get("body", "")):
                            continue
                        if review.get("commit_id") == commit_sha:
                            return {
                                "published": True, "action": "already_published",
                                "method": "pull_request_review", "comment_id": review.get("id"),
                                "html_url": review.get("html_url"), "comment_url": review.get("html_url"),
                                "inline_comments": 0,
                            }
        except Exception:
            logger.warning("Could not inspect prior Fidelis PR reviews before fallback publication", exc_info=True)

        response = await self._request_with_retry(
            client, "POST", reviews_url, headers=headers,
            json={"commit_id": commit_sha, "body": body, "event": "COMMENT"},
        )
        if not response.is_success:
            required = response.headers.get("X-Accepted-GitHub-Permissions", "not supplied")
            logger.error(
                "GitHub PR-review fallback failed: HTTP %s; accepted_permissions=%s; response=%s",
                response.status_code, required, response.text[:300],
            )
            return None
        data = response.json()
        return {
            "published": True, "action": "created", "method": "pull_request_review",
            "comment_id": data.get("id"), "html_url": data.get("html_url"),
            "comment_url": data.get("html_url"), "inline_comments": 0,
        }

    async def _find_existing(self, client: httpx.AsyncClient, url: str, headers: dict) -> dict | None:
        # Ask for newest comments first, then follow pagination when an old PR
        # has more than one page. This prevents creating duplicate Fidelis
        # reports merely because a busy PR has accumulated many comments.
        next_url: str | None = url
        params = {"per_page": 100, "page": 1, "sort": "created", "direction": "desc"}
        pages = 0
        while next_url and pages < 20:
            response = await self._request_with_retry(
                client, "GET", next_url, headers=headers, params=params if pages == 0 else None,
            )
            if not response.is_success:
                raise RuntimeError(f"GitHub comment listing returned HTTP {response.status_code}: {response.text[:500]}")
            comments = response.json()
            if not isinstance(comments, list):
                raise RuntimeError("GitHub comments endpoint returned an unexpected response")
            for comment in comments:
                if isinstance(comment, dict) and _MARKER in str(comment.get("body", "")):
                    return comment
            next_link = response.links.get("next") if hasattr(response, "links") else None
            next_url = next_link.get("url") if isinstance(next_link, dict) else None
            pages += 1
        if next_url:
            logger.warning("Fidelis marker comment not found within the first %s GitHub comment pages", pages)
        return None

    async def _request_with_retry(self, client: httpx.AsyncClient, method: str, url: str, **kwargs):
        response = None
        for attempt in range(3):
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise RuntimeError(f"GitHub comment delivery failed: {type(exc).__name__}") from exc
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(30.0, max(1.0, float(retry_after))) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                await asyncio.sleep(delay)
                continue
            return response
        assert response is not None
        return response

    @staticmethod
    def _report_body(result: ReviewResult) -> str:
        counts = result.summary.by_severity
        lines = [
            _MARKER,
            "# Fidelis — Automated Code Review",
            "",
            f"**Repository:** `{result.repository}`  ",
            f"**Pull request:** #{result.pr_number}  ",
            f"**Reviewed commit:** `{result.commit_sha}`  ",
            f"**Reviewer:** {result.reviewer.name} ({result.reviewer.mode.value})",
            "",
            f"## Summary: {result.summary.total_findings} finding(s)",
            f"**{counts.get('HIGH', 0)} HIGH** · **{counts.get('MEDIUM', 0)} MEDIUM** · **{counts.get('LOW', 0)} LOW**",
            "",
        ]
        if result.execution:
            execution = result.execution
            agents = ", ".join(agent.value for agent in execution.selected_agents) or "none"
            lines.extend([
                "## Execution",
                f"- Strategy: `{execution.strategy}`",
                f"- Specialist passes: {len(execution.selected_agents)} ({agents})",
                f"- LLM requests: {execution.llm_requests}",
                f"- Reused completed checkpoints: {execution.checkpoints_reused}",
                f"- Pipeline duration: {execution.duration_ms:,} ms",
                f"- Token usage: {execution.input_tokens if execution.input_tokens is not None else 'unknown'} input / {execution.output_tokens if execution.output_tokens is not None else 'unknown'} output",
                "",
            ])
        if not result.findings:
            lines.extend(["## Findings", "No actionable issues were identified in the reviewed changes.", ""])
        else:
            lines.append("## Findings")
            for index, finding in enumerate(result.findings, start=1):
                location = f"`{finding.file}:{finding.line}`" if finding.file and finding.line else "Pull-request level"
                lines.extend([
                    f"### {index}. {finding.severity.value} — {finding.category.value}",
                    f"**Agent:** {finding.agent.value}  ",
                    f"**Location:** {location}  ",
                    f"**Confidence:** {finding.confidence:.0%}",
                    "",
                    finding.message,
                    "",
                ])
                if finding.suggestion:
                    lines.extend([f"**Suggested fix:** {finding.suggestion}", ""])
        if result.agent_runs:
            lines.extend(["## Specialist execution details", ""])
            for run in result.agent_runs:
                notes = []
                if run.reused_checkpoint:
                    notes.append("restored from checkpoint")
                if run.shared_call:
                    notes.append("shared single-pass request")
                if run.rejected_findings_count:
                    notes.append(f"{run.rejected_findings_count} rejected candidate(s)")
                suffix = f" — {'; '.join(notes)}" if notes else ""
                lines.append(f"- **{run.agent.value}:** {run.findings_count} finding(s), {run.duration_ms:,} ms, {run.response_status}{suffix}")
            lines.append("")
        lines.extend([
            "---",
            f"_Fidelis · review duration {result.duration_ms:,} ms · {len(result.agents_run)} specialist(s)._",
            "_Advisory review only. Findings require human verification; Fidelis does not approve or modify code._",
        ])
        body = "\n".join(lines)
        # GitHub accepts large issue comments; preserve a clear truncation notice if a very large PR exceeds the API limit.
        if len(body) > 60_000:
            body = body[:59_500] + "\n\n> Report truncated to fit GitHub's comment size limit. The complete structured result remains available in the Fidelis Control Room."
        return body
