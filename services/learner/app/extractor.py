"""Mine lightweight, evidence-backed conventions from merged pull requests.

This is deliberately repository-specific and explainable, not a self-modifying
model. Rules are derived from repeated path, language, test and code patterns.
"""
from __future__ import annotations

import collections
import re
from urllib.parse import quote

import httpx


class GitHubLearningError(RuntimeError):
    pass


class GitHubLearningClient:
    def __init__(self, token: str, *, api_url: str = "https://api.github.com", timeout: float = 25.0):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Fidelis-Repository-Learner",
        }

    def get_json(self, url: str, params: dict | None = None):
        try:
            response = httpx.get(url, headers=self._headers(), params=params, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise GitHubLearningError(f"GitHub request failed: {type(exc).__name__}") from exc
        if response.status_code in {401, 403}:
            raise GitHubLearningError("GitHub token is invalid or lacks repository read permissions")
        if response.status_code == 429 or response.status_code >= 500:
            raise GitHubLearningError(f"GitHub temporarily returned HTTP {response.status_code}")
        if not response.is_success:
            raise GitHubLearningError(f"GitHub returned HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def mine(self, repository: str, *, max_prs: int = 30) -> dict:
        base = f"{self.api_url}/repos/{quote(repository, safe='/')}"
        prs = self.get_json(f"{base}/pulls", {
            "state": "closed", "sort": "updated", "direction": "desc", "per_page": min(max_prs, 100),
        })
        merged = [pr for pr in prs if pr.get("merged_at")][:max_prs]
        changed_paths: list[str] = []
        patches: list[tuple[int, str]] = []
        source_prs: list[int] = []
        for pr in merged:
            number = int(pr["number"])
            source_prs.append(number)
            files = self.get_json(f"{base}/pulls/{number}/files", {"per_page": 100})
            for file_item in files:
                path = str(file_item.get("filename", ""))
                if path:
                    changed_paths.append(path)
                patch = file_item.get("patch") or ""
                if patch:
                    patches.append((number, patch))
        rules = extract_rules(changed_paths, patches, source_prs)
        return {
            "repository": repository,
            "merged_prs_scanned": len(source_prs),
            "changed_files_scanned": len(changed_paths),
            "rules": rules,
            "source_prs": source_prs,
        }


def extract_rules(paths: list[str], patches: list[tuple[int, str]], source_prs: list[int]) -> list[dict]:
    if not paths:
        return []
    total = len(paths)
    extensions = collections.Counter(path.rsplit(".", 1)[-1].lower() for path in paths if "." in path)
    directories = collections.Counter(path.split("/", 1)[0] for path in paths if "/" in path)
    test_count = sum(bool(re.search(r"(^|/)(tests?|__tests__?)(/|$)|(^|/)test_[^/]+|\.(test|spec)\.[^.]+$", path, re.IGNORECASE)) for path in paths)
    rules: list[dict] = []
    if extensions:
        ext, count = extensions.most_common(1)[0]
        if count / total >= 0.35:
            rules.append(_rule("dominant-language", f"Most changed files use the .{ext} extension ({count}/{total} sampled files); keep new implementation files consistent with this repository's dominant language.", count, source_prs))
    if directories:
        directory, count = directories.most_common(1)[0]
        if count >= 3:
            rules.append(_rule("primary-source-directory", f"The '{directory}/' directory is the most frequent top-level location for changed files ({count} observations); place related code with the existing module structure.", count, source_prs))
    if test_count:
        ratio = test_count / total
        rules.append(_rule("test-file-convention", f"Merged changes include {test_count}/{total} test-path file(s); keep automated tests in the repository's established test directories/patterns.", test_count, source_prs, min(0.95, 0.55 + ratio * 0.4)))

    added_lines: list[tuple[int, str]] = []
    for pr_number, patch in patches:
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_lines.append((pr_number, line[1:]))
    if added_lines:
        python_defs = [line for _, line in added_lines if re.match(r"\s*def\s+[a-z][a-z0-9_]*\s*\(", line)]
        camel_defs = [line for _, line in added_lines if re.match(r"\s*function\s+[a-z][A-Za-z0-9]*\s*\(", line) or re.match(r"\s*(const|let)\s+[a-z][A-Za-z0-9]*\s*=\s*\(", line)]
        typed_defs = [line for _, line in added_lines if re.search(r"def\s+\w+\([^)]*:\s*\w+", line)]
        if len(python_defs) >= 3:
            rules.append(_rule("python-naming", "Python functions in merged changes predominantly use snake_case; follow snake_case for new functions.", len(python_defs), source_prs, 0.9))
        if len(camel_defs) >= 3:
            rules.append(_rule("javascript-naming", "JavaScript/TypeScript functions in merged changes predominantly use camelCase; follow camelCase for new functions.", len(camel_defs), source_prs, 0.85))
        if len(typed_defs) >= 3:
            rules.append(_rule("python-type-hints", "Several merged Python changes use parameter type annotations; continue adding explicit type hints to new functions.", len(typed_defs), source_prs, 0.85))
    return rules


def _rule(key: str, description: str, count: int, source_prs: list[int], confidence: float | None = None) -> dict:
    evidence_confidence = confidence if confidence is not None else min(0.95, 0.55 + count * 0.04)
    return {
        "rule_key": key,
        "description": description,
        "confidence": round(evidence_confidence, 2),
        "evidence_count": max(1, count),
        "source_prs": source_prs[:100],
    }
