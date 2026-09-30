"""
Pydantic models for the subset of the GitHub `pull_request` webhook
event payload that the Webhook Service actually needs.

GitHub sends a LOT more fields than this. We deliberately only model
what the Orchestrator / Reviewer Service pipeline will consume, and
use `Extra.ignore` (Pydantic's default) so unknown fields don't break
parsing when GitHub adds new ones.
"""

from __future__ import annotations

from pydantic import BaseModel


class GitHubUser(BaseModel):
    login: str
    id: int


class GitHubRepository(BaseModel):
    id: int
    name: str
    full_name: str  # e.g. "org/repo"
    private: bool
    default_branch: str
    html_url: str
    clone_url: str


class GitHubBranchRef(BaseModel):
    ref: str
    sha: str
    repo: GitHubRepository | None = None


class GitHubPullRequest(BaseModel):
    id: int
    number: int
    title: str
    state: str  # "open" | "closed"
    draft: bool = False
    user: GitHubUser
    body: str | None = None
    head: GitHubBranchRef
    base: GitHubBranchRef
    html_url: str
    diff_url: str
    patch_url: str
    commits: int
    additions: int
    deletions: int
    changed_files: int
    merged: bool = False


class PullRequestWebhookEvent(BaseModel):
    """
    Top-level payload for the `pull_request` GitHub webhook event.
    https://docs.github.com/en/webhooks/webhook-events-and-payloads#pull_request
    """
    action: str  # "opened" | "synchronize" | "reopened" | "closed" | ...
    number: int
    pull_request: GitHubPullRequest
    repository: GitHubRepository
    sender: GitHubUser
    installation: dict | None = None  # present for GitHub App installs


# Actions that should actually trigger a review job.
# "synchronize" = new commits pushed to an existing PR.
REVIEW_TRIGGER_ACTIONS = {"opened", "synchronize", "reopened"}


class ReviewJobMessage(BaseModel):
    """
    Normalized internal message the Webhook Service hands off to the
    Orchestrator Service (directly, or via Redis/Celery once that
    piece is built in Weeks 5-6).
    """
    event_id: str  # GitHub delivery ID, used for idempotency
    action: str
    repo_full_name: str
    pr_number: int
    pr_title: str
    head_sha: str
    base_sha: str
    diff_url: str
    html_url: str
    author_login: str
