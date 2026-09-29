"""
GitHub webhook signature verification.

GitHub signs every webhook delivery with an HMAC-SHA256 digest of the
raw request body, using the secret you configure on the GitHub
App / repo webhook. It sends this as the `X-Hub-Signature-256` header
in the form `sha256=<hex digest>`.

We MUST verify this before trusting the payload, otherwise anyone who
finds the endpoint URL could forge PR events.
"""

import hmac
import hashlib


class InvalidSignatureError(Exception):
    pass


def verify_github_signature(payload_body: bytes, secret: str, signature_header: str | None) -> None:
    """
    Raises InvalidSignatureError if the signature doesn't match.

    payload_body: the RAW request body bytes (not re-serialized JSON —
                  re-serializing can change whitespace/key order and
                  break the comparison).
    secret:       GITHUB_WEBHOOK_SECRET, shared with GitHub's config.
    signature_header: the raw 'X-Hub-Signature-256' header value.
    """
    if not signature_header:
        raise InvalidSignatureError("Missing X-Hub-Signature-256 header")

    if not signature_header.startswith("sha256="):
        raise InvalidSignatureError("Unexpected signature format")

    expected_digest = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    expected_header = f"sha256={expected_digest}"

    # constant-time comparison to avoid timing attacks
    if not hmac.compare_digest(expected_header, signature_header):
        raise InvalidSignatureError("Signature mismatch")
