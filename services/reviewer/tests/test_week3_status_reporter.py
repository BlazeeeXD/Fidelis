import json

import httpx
import pytest

from app.worker import HttpJobStatusReporter, StatusReporterRejected, StatusReporterUnavailable


def test_http_status_reporter_sends_authenticated_start_event():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "RUNNING"})

    inner = httpx.Client(base_url="http://orchestrator:8000", transport=httpx.MockTransport(handler))
    reporter = HttpJobStatusReporter(token="secret", client=inner)
    reporter.job_started("job-week3-1", attempt=2)
    req = seen[0]
    assert req.url.path == "/jobs/job-week3-1/status"
    assert req.headers["X-Worker-Token"] == "secret"
    assert json.loads(req.read()) == {"event": "started", "attempt": 2}
    inner.close()


def test_http_status_reporter_classifies_unavailable_and_rejected():
    unavailable_client = httpx.Client(
        base_url="http://orchestrator",
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    unavailable = HttpJobStatusReporter(token="secret", client=unavailable_client)
    with pytest.raises(StatusReporterUnavailable):
        unavailable.job_started("job-week3-1", attempt=1)
    unavailable.close()

    rejected_client = httpx.Client(
        base_url="http://orchestrator",
        transport=httpx.MockTransport(lambda r: httpx.Response(409)),
    )
    rejected = HttpJobStatusReporter(token="secret", client=rejected_client)
    with pytest.raises(StatusReporterRejected):
        rejected.job_started("job-week3-1", attempt=1)
    rejected.close()
