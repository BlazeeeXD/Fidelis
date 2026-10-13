"""Shared fixtures for the reviewer tests."""

from datetime import UTC, datetime, timedelta

import pytest
from app.schemas import ReviewRequest


class FakeClock:
    """Deterministic clock: each call advances ``step_ms`` from a fixed start."""

    def __init__(self, step_ms: int = 250) -> None:
        self._now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        self._step = timedelta(milliseconds=step_ms)

    def __call__(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def request_obj() -> ReviewRequest:
    return ReviewRequest(
        job_id="job-1", repository="test/repo", pr_number=7, commit_sha="abc123"
    )


SAMPLE_DIFF = """\
diff --git a/app/users.py b/app/users.py
index 1111111..2222222 100644
--- a/app/users.py
+++ b/app/users.py
@@ -10,3 +10,5 @@ def load():
     existing = 1
+    new_a = 2
+    new_b = 3
     tail = 4
diff --git a/app/db.py b/app/db.py
new file mode 100644
--- /dev/null
+++ b/app/db.py
@@ -0,0 +1,3 @@
+import sqlite3
+
+conn = sqlite3.connect("x.db")
"""
