"""Regression tests for independent specialist scheduling and retry checkpoints."""
from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.reviewers.llm import LLMReviewer
from app.schemas import (
    ErrorCode,
    ReviewAgent,
    ReviewerError,
    ReviewerMode,
    ReviewRequest,
)


AGENTS = (
    ReviewAgent.STATIC_ANALYSIS,
    ReviewAgent.SECURITY,
    ReviewAgent.ARCHITECTURE,
    ReviewAgent.CODE_STYLE,
)
DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
-old = True
+new = True
+print(new)
"""


def make_reviewer(strategy: str) -> LLMReviewer:
    return LLMReviewer(Settings(
        mode=ReviewerMode.LLM,
        review_agents=AGENTS,
        execution_strategy=strategy,
        checkpoint_enabled=True,
        worker_status_token="test-worker-token",
        llm_model="test-model",
    ))


def make_request(job_id: str = "job-scheduler") -> ReviewRequest:
    return ReviewRequest(
        job_id=job_id,
        repository="test/repo",
        pr_number=42,
        commit_sha="a" * 40,
        diff=DIFF,
    )


def agent_from_prompt(system_prompt: str) -> ReviewAgent:
    markers = {
        ReviewAgent.STATIC_ANALYSIS: "STATIC ANALYSIS SPECIALIST",
        ReviewAgent.SECURITY: "APPLICATION SECURITY SPECIALIST",
        ReviewAgent.ARCHITECTURE: "SOFTWARE ARCHITECTURE SPECIALIST",
        ReviewAgent.CODE_STYLE: "CODE STYLE AND MAINTAINABILITY SPECIALIST",
    }
    return next(agent for agent, marker in markers.items() if marker in system_prompt)


@pytest.mark.parametrize("strategy", ["sequential", "parallel"])
def test_specialist_execution_uses_selected_roster(strategy: str):
    reviewer = make_reviewer(strategy)
    calls: list[ReviewAgent] = []
    saved: list[ReviewAgent] = []

    async def fake_completion(*, system, user, max_tokens):
        agent = agent_from_prompt(system)
        calls.append(agent)
        return {"findings": [], "_usage": {"input_tokens": 100, "output_tokens": 10}}

    async def no_rules(repository):
        return []

    async def no_checkpoints(job_id, fingerprint):
        return {}

    async def save_checkpoint(job_id, fingerprint, agent, findings, run):
        saved.append(agent)

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    reviewer._load_checkpoints = no_checkpoints
    reviewer._save_checkpoint = save_checkpoint

    result = asyncio.run(reviewer.review(make_request()))

    assert set(calls) == set(AGENTS)
    assert len(calls) == 4
    assert set(result.agents_run) == set(AGENTS)
    assert result.execution is not None
    assert result.execution.strategy == strategy
    assert result.execution.llm_requests == 4
    assert result.execution.input_tokens == 400
    if strategy == "sequential":
        assert calls == list(AGENTS)
        assert saved == list(AGENTS)
    else:
        # asyncio.gather preserves result order even though call completion can vary.
        assert set(saved) == set(AGENTS)


def test_sequential_retry_reuses_completed_specialist_checkpoints():
    reviewer = make_reviewer("sequential")
    saved_payloads: dict[str, dict] = {}
    first_attempt_calls: list[ReviewAgent] = []
    fail_once = True

    async def no_rules(repository):
        return []

    async def load_checkpoints(job_id, fingerprint):
        return dict(saved_payloads)

    async def save_checkpoint(job_id, fingerprint, agent, findings, run):
        saved_payloads[agent.value] = {
            "findings": [finding.model_dump(mode="json") for finding in findings],
            "agent_run": run.model_dump(mode="json"),
        }

    async def first_attempt(*, system, user, max_tokens):
        nonlocal fail_once
        agent = agent_from_prompt(system)
        first_attempt_calls.append(agent)
        if agent is ReviewAgent.ARCHITECTURE and fail_once:
            fail_once = False
            raise ReviewerError(ErrorCode.REVIEW_TIMEOUT, "simulated temporary model timeout")
        return {"findings": [], "_usage": {"input_tokens": 100, "output_tokens": 10}}

    reviewer._fetch_repository_rules = no_rules
    reviewer._load_checkpoints = load_checkpoints
    reviewer._save_checkpoint = save_checkpoint
    reviewer.llm.json_completion = first_attempt

    with pytest.raises(ReviewerError) as error:
        asyncio.run(reviewer.review(make_request("job-retry")))
    assert error.value.code is ErrorCode.REVIEW_TIMEOUT
    assert first_attempt_calls == [ReviewAgent.STATIC_ANALYSIS, ReviewAgent.SECURITY, ReviewAgent.ARCHITECTURE]
    assert set(saved_payloads) == {ReviewAgent.STATIC_ANALYSIS.value, ReviewAgent.SECURITY.value}

    retry_calls: list[ReviewAgent] = []

    async def retry_completion(*, system, user, max_tokens):
        agent = agent_from_prompt(system)
        retry_calls.append(agent)
        return {"findings": [], "_usage": {"input_tokens": 120, "output_tokens": 12}}

    reviewer.llm.json_completion = retry_completion
    result = asyncio.run(reviewer.review(make_request("job-retry")))

    assert retry_calls == [ReviewAgent.ARCHITECTURE, ReviewAgent.CODE_STYLE]
    assert result.execution is not None
    assert result.execution.strategy == "sequential"
    assert result.execution.checkpoints_reused == 2
    assert result.execution.llm_requests == 4
    assert [run.reused_checkpoint for run in result.agent_runs] == [True, True, False, False]
    # Pipeline totals include completed work represented by the final report,
    # including outputs restored from the previous attempt.
    assert result.execution.input_tokens == 440
    assert result.execution.output_tokens == 44


def test_single_pass_uses_one_model_request_and_returns_separate_agent_results():
    reviewer = make_reviewer("single_pass")
    calls = 0

    async def no_rules(repository):
        return []

    async def one_combined_completion(*, system, user, max_tokens):
        nonlocal calls
        calls += 1
        return {
            "agent_results": [
                {"agent": agent.value, "findings": []}
                for agent in AGENTS
            ],
            "_usage": {"input_tokens": 700, "output_tokens": 80},
        }

    reviewer._fetch_repository_rules = no_rules
    reviewer.llm.json_completion = one_combined_completion
    result = asyncio.run(reviewer.review(make_request("job-single-pass")))

    assert calls == 1
    assert result.execution is not None
    assert result.execution.strategy == "single_pass"
    assert result.execution.llm_requests == 1
    assert result.execution.input_tokens == 700
    assert result.execution.output_tokens == 80
    assert [run.agent for run in result.agent_runs] == list(AGENTS)
    assert all(run.shared_call and run.duration_ms == 0 for run in result.agent_runs)


def test_single_pass_fails_when_model_omits_a_required_specialist():
    reviewer = make_reviewer("single_pass")

    async def no_rules(repository):
        return []

    async def incomplete_completion(*, system, user, max_tokens):
        return {
            "agent_results": [
                {"agent": agent.value, "findings": []}
                for agent in AGENTS[:-1]
            ]
        }

    reviewer._fetch_repository_rules = no_rules
    reviewer.llm.json_completion = incomplete_completion

    with pytest.raises(ReviewerError) as error:
        asyncio.run(reviewer.review(make_request("job-single-pass-missing")))
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert "CODE_STYLE" in error.value.message
    assert error.value.details["missing_agents"] == ["CODE_STYLE"]
