import asyncio

import pytest
from app.config import Settings
from app.reviewers.llm import LLMReviewer
from app.schemas import (
    Category,
    ErrorCode,
    ReviewAgent,
    ReviewerError,
    ReviewerMode,
    ReviewRequest,
    Severity,
)

SAMPLE_DIFF = """diff --git a/app/auth.py b/app/auth.py
index 1111111..2222222 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,2 +1,3 @@
 import os
+API_KEY = \"sk-this-is-a-test-secret-value\"
+def authenticate(user):
     return user
"""


def test_multi_agent_pipeline_runs_four_specialists_and_scanners(monkeypatch):
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="multi_agent"))
    calls = []

    async def fake_completion(*, system, user, max_tokens):
        calls.append(system.split("\n")[-1])
        return {"findings": [{
            "severity": "MEDIUM", "category": "BUG", "file": "app/auth.py", "line": 3,
            "message": "Missing validation for the supplied user object.",
            "suggestion": "Validate the user before use.", "confidence": 0.8,
        }]}

    async def no_rules(repository):
        return []

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(job_id="job-llm", repository="test/repo", pr_number=4, commit_sha="abc", diff=SAMPLE_DIFF)
    result = asyncio.run(reviewer.review(request))
    assert len(calls) == 4
    assert result.agents_run == [ReviewAgent.STATIC_ANALYSIS, ReviewAgent.SECURITY, ReviewAgent.ARCHITECTURE, ReviewAgent.CODE_STYLE]
    assert any(f.agent is ReviewAgent.SECURITY and f.category is Category.SECURITY and f.severity is Severity.HIGH for f in result.findings)
    assert result.summary.total_findings >= 2
    assert len(result.agent_runs) == 4
    assert {run.agent for run in result.agent_runs} == set(result.agents_run)
    assert all(run.duration_ms >= 0 for run in result.agent_runs)
    assert all(run.findings_count == 1 for run in result.agent_runs)


def test_single_pipeline_runs_general_agent_only():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="single"))

    async def fake_completion(*, system, user, max_tokens):
        return {"findings": []}

    async def no_rules(repository):
        return []

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(job_id="job-single", repository="test/repo", pr_number=4, commit_sha="abc", diff=SAMPLE_DIFF)
    result = asyncio.run(reviewer.review(request))
    assert result.agents_run == [ReviewAgent.GENERAL]
    assert len(result.agent_runs) == 1
    assert result.findings  # deterministic security scanner still operates


def test_empty_diff_returns_empty_valid_result():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM))
    request = ReviewRequest(job_id="job-empty", repository="test/repo", pr_number=4, commit_sha="abc", diff="")
    result = asyncio.run(reviewer.review(request))
    assert result.findings == []
    assert result.summary.total_findings == 0


def test_empty_diff_has_no_agent_invocations():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM))
    request = ReviewRequest(job_id="job-empty-agents", repository="test/repo", pr_number=4, commit_sha="abc", diff="")
    result = asyncio.run(reviewer.review(request))
    assert result.agent_runs == []
    assert result.execution is not None
    assert result.execution.selected_agents == []
    assert result.execution.llm_requests == 0


def test_security_pipeline_runs_only_the_security_specialist():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="security"))
    calls = []

    async def fake_completion(*, system, user, max_tokens):
        calls.append(system)
        return {"findings": []}

    async def no_rules(repository):
        return []

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(job_id="job-security", repository="test/repo", pr_number=4, commit_sha="abc", diff=SAMPLE_DIFF)
    result = asyncio.run(reviewer.review(request))

    assert result.agents_run == [ReviewAgent.SECURITY]
    assert len(calls) == 1
    assert "APPLICATION SECURITY SPECIALIST" in calls[0]
    assert "Trace untrusted values" in calls[0]


def test_source_context_is_fetched_at_the_reviewed_commit_and_sent_to_model():
    reviewer = LLMReviewer(
        Settings(
            mode=ReviewerMode.LLM,
            pipeline_mode="security",
            github_token="test-token",
            source_context_lines=2,
        )
    )
    fetched = []
    model_inputs = []

    async def fake_fetch_file(repository, path, ref):
        fetched.append((repository, path, ref))
        return "import os\n\nAPI_KEY = 'x'\ndef authenticate(user):\n    return user\n"

    async def fake_completion(*, system, user, max_tokens):
        model_inputs.append(user)
        return {"findings": []}

    async def no_rules(repository):
        return []

    reviewer.github.fetch_file = fake_fetch_file
    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(job_id="job-context", repository="test/repo", pr_number=4, commit_sha="head-sha", diff=SAMPLE_DIFF)
    result = asyncio.run(reviewer.review(request))

    assert fetched == [("test/repo", "app/auth.py", "head-sha")]
    assert "Relevant source excerpts at commit head-sha" in model_inputs[0]
    assert "def authenticate(user):" in model_inputs[0]
    assert result.agent_runs[0].source_context_files == 1


def test_candidate_findings_on_unchanged_lines_fail_instead_of_becoming_empty_reviews():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="security"))

    async def fake_completion(*, system, user, max_tokens):
        return {"findings": [{
            "severity": "HIGH", "category": "SECURITY", "file": "app/auth.py", "line": 4,
            "message": "A made-up finding on an unchanged context line.",
            "suggestion": "Fix it.", "confidence": 0.95,
        }]}

    async def no_rules(repository):
        return []

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(job_id="job-bad-location", repository="test/repo", pr_number=4, commit_sha="abc", diff=SAMPLE_DIFF)

    with pytest.raises(ReviewerError) as error:
        asyncio.run(reviewer.review(request))
    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert "none passed schema and changed-line validation" in error.value.message
    assert error.value.details["candidate_count"] == 1
    assert error.value.details["rejected_count"] == 1
    assert error.value.details["rejection_reasons"] == {"location_not_in_added_diff": 1}


def test_partially_rejected_findings_are_counted_in_agent_telemetry():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="security"))

    async def fake_completion(*, system, user, max_tokens):
        return {"findings": [
            {
                "severity": "HIGH", "category": "SECURITY", "file": "app/auth.py", "line": 2,
                "message": "Hard-coded API credential is introduced.",
                "suggestion": "Load it from a secret store.", "confidence": 0.99,
            },
            {
                "severity": "HIGH", "category": "SECURITY", "file": "app/auth.py", "line": 999,
                "message": "This location does not exist in the patch.",
                "suggestion": "Fix it.", "confidence": 0.99,
            },
        ]}

    async def no_rules(repository):
        return []

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(job_id="job-partial", repository="test/repo", pr_number=4, commit_sha="abc", diff=SAMPLE_DIFF)
    result = asyncio.run(reviewer.review(request))

    assert result.agent_runs[0].findings_count == 1
    assert result.agent_runs[0].rejected_findings_count == 1
    assert result.agent_runs[0].response_status == "partial"


def test_sequential_specialist_prompt_requires_exact_findings_array_contract():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="security"))
    captured = {}

    async def fake_completion(*, system, user, max_tokens):
        captured["system"] = system
        return {"findings": []}

    async def no_rules(repository):
        return []

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(
        job_id="job-schema-prompt", repository="test/repo", pr_number=4,
        commit_sha="abc", diff=SAMPLE_DIFF,
    )
    result = asyncio.run(reviewer.review(request))

    assert result.agents_run == [ReviewAgent.SECURITY]
    assert '"findings": [' in captured["system"]
    assert 'return exactly {"findings": []}' in captured["system"].lower()
    assert 'top-level key MUST be named exactly "findings"' in captured["system"]


def test_missing_findings_array_reports_actual_top_level_keys():
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="security"))

    async def fake_completion(*, system, user, max_tokens):
        return {"issues": [], "summary": "No security issues found."}

    async def no_rules(repository):
        return []

    reviewer.llm.json_completion = fake_completion
    reviewer._fetch_repository_rules = no_rules
    request = ReviewRequest(
        job_id="job-wrong-wrapper", repository="test/repo", pr_number=4,
        commit_sha="abc", diff=SAMPLE_DIFF,
    )

    with pytest.raises(ReviewerError) as error:
        asyncio.run(reviewer.review(request))

    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert error.value.details["top_level_keys"] == ["issues", "summary"]
    assert "required top-level 'findings' array" in error.value.message


def test_deterministic_security_signals_detect_sql_shell_path_and_removed_admin_guard():
    diff = '''diff --git a/src/account_service.py b/src/account_service.py
index 1111111..2222222 100644
--- a/src/account_service.py
+++ b/src/account_service.py
@@ -1,12 +1,14 @@
 import os
-def change_role(actor, user_id, role):
-    if not actor.is_admin:
-        raise PermissionError("admin only")
+def find_user(username):
+    query = f"SELECT * FROM users WHERE username = '{username}'"
+    return db.execute(query)
+def read_report(report_path):
+    with open(report_path, "r") as handle:
+        return handle.read()
+def ping_host(host):
+    return os.system(f"ping -c 1 {host}")
+def change_role(actor, user_id, role):
+    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
'''
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="security"))
    from app.reviewers.diff_utils import parse_added_lines

    findings = reviewer._static_signal_findings(parse_added_lines(diff), diff)
    assert any("sql" in item.message.lower() and "injection" in item.message.lower() for item in findings)
    assert any("os.system" in item.message.lower() for item in findings)
    assert any("traversal" in item.message.lower() for item in findings)
    assert any("authorization" in item.message.lower() or "administrator" in item.message.lower() for item in findings)
    assert all(item.file == "src/account_service.py" and item.line for item in findings)


def test_deterministic_security_signals_do_not_flag_static_sql_or_static_shell_commands():
    diff = '''diff --git a/app/safe.py b/app/safe.py
index 1111111..2222222 100644
--- a/app/safe.py
+++ b/app/safe.py
@@ -1,2 +1,3 @@
+query = "SELECT * FROM users WHERE active = 1"
+os.system("uptime")
'''
    reviewer = LLMReviewer(Settings(mode=ReviewerMode.LLM, pipeline_mode="security"))
    from app.reviewers.diff_utils import parse_added_lines

    findings = reviewer._static_signal_findings(parse_added_lines(diff), diff)
    assert not [item for item in findings if item.category is Category.SECURITY]
