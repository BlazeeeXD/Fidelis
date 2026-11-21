import asyncio

import httpx

from app.github_reviews import _MARKER, GitHubReviewPublisher
from app.reviewers.mock import MockReviewer, MockScenario
from app.schemas import ReviewRequest


class FakeAsyncClient:
    def __init__(self, *, existing=None):
        self.existing = existing
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == "GET":
            data = self.existing or []
        else:
            data = {"id": 77, "html_url": "https://github.com/example/repo/pull/1#issuecomment-77"}
        return httpx.Response(200, json=data, request=httpx.Request(method, url))


def test_publisher_updates_existing_fidelis_comment_without_inline_review(monkeypatch):
    fake = FakeAsyncClient(existing=[{"id": 9, "body": f"{_MARKER}\nold report"}])
    monkeypatch.setattr("app.github_reviews.httpx.AsyncClient", lambda **kwargs: fake)
    request = ReviewRequest(job_id="job-gh", repository="example/repo", pr_number=1, commit_sha="abc123")
    result = asyncio.run(MockReviewer(MockScenario.MULTI_AGENT).review(request))

    delivery = asyncio.run(GitHubReviewPublisher("token").publish(request, result))
    assert delivery["published"] is True
    assert delivery["action"] == "updated"
    assert delivery["comment_url"].endswith("issuecomment-77")
    assert [item[0] for item in fake.requests] == ["GET", "PATCH"]
    assert fake.requests[1][2]["json"]["body"].startswith(_MARKER)
    assert "## Findings" in fake.requests[1][2]["json"]["body"]


def test_publisher_creates_one_comment_when_no_fidelis_comment_exists(monkeypatch):
    fake = FakeAsyncClient(existing=[])
    monkeypatch.setattr("app.github_reviews.httpx.AsyncClient", lambda **kwargs: fake)
    request = ReviewRequest(job_id="job-gh-new", repository="example/repo", pr_number=2, commit_sha="def456")
    result = asyncio.run(MockReviewer(MockScenario.CLEAN).review(request))

    delivery = asyncio.run(GitHubReviewPublisher("token").publish(request, result))
    assert delivery["action"] == "created"
    assert [item[0] for item in fake.requests] == ["GET", "POST"]


class FallbackAsyncClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if method == "GET" and url.endswith("/issues/3/comments"):
            response = httpx.Response(200, json=[], request=httpx.Request(method, url))
        elif method == "POST" and url.endswith("/issues/3/comments"):
            response = httpx.Response(
                403, json={"message": "Resource not accessible by personal access token"},
                headers={"X-Accepted-GitHub-Permissions": "issues=write; pull_requests=write"},
                request=httpx.Request(method, url),
            )
        elif method == "GET" and url.endswith("/pulls/3/reviews"):
            response = httpx.Response(200, json=[], request=httpx.Request(method, url))
        elif method == "POST" and url.endswith("/pulls/3/reviews"):
            response = httpx.Response(200, json={"id": 88, "html_url": "https://github.com/example/repo/pull/3#pullrequestreview-88"}, request=httpx.Request(method, url))
        else:
            response = httpx.Response(404, json={}, request=httpx.Request(method, url))
        return response


def test_publisher_falls_back_to_pull_request_review_when_issue_comment_is_forbidden(monkeypatch):
    fake = FallbackAsyncClient()
    monkeypatch.setattr("app.github_reviews.httpx.AsyncClient", lambda **kwargs: fake)
    request = ReviewRequest(job_id="job-gh-fallback", repository="example/repo", pr_number=3, commit_sha="abc123")
    result = asyncio.run(MockReviewer(MockScenario.CLEAN).review(request))

    delivery = asyncio.run(GitHubReviewPublisher("token").publish(request, result))
    assert delivery["published"] is True
    assert delivery["method"] == "pull_request_review"
    assert delivery["comment_url"].endswith("pullrequestreview-88")
    assert [item[0] for item in fake.requests] == ["GET", "POST", "GET", "POST"]
