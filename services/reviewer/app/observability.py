"""Optional Langfuse v4 instrumentation with a no-op fallback.

Tracing is disabled unless LANGFUSE_ENABLED=true and credentials are present.
Only metadata (repo, PR, agent, model, duration and token counts) is recorded;
source code and diffs are never sent to the observability backend.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("reviewer.observability")


@contextmanager
def generation_observation(*, name: str, model: str, metadata: dict[str, Any] | None = None):
    enabled = os.getenv("LANGFUSE_ENABLED", "false").lower() in {"1", "true", "yes"}
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    if not enabled or not public_key or not secret_key:
        yield None
        return

    try:
        from langfuse import get_client
        client = get_client()
        observation_context = client.start_as_current_observation(
            as_type="generation", name=name, model=model, input=metadata or {}
        )
    except Exception:
        logger.warning("Langfuse is configured but could not be initialised", exc_info=True)
        yield None
        return

    try:
        with observation_context as span:
            yield span
    except Exception:
        # Let the review pipeline handle its own operational errors.
        raise
    finally:
        try:
            client.flush()
        except Exception:
            logger.debug("Langfuse flush failed", exc_info=True)
