"""
JWT authentication/authorization for the Gateway Service.

This protects the internal API surface (e.g. dashboards, manual review
triggers, admin endpoints) that clients other than GitHub itself call.
NOTE: GitHub's own webhook calls do NOT carry a JWT — they're verified
separately downstream by the Webhook Service's HMAC signature check
(see webhook/security.py). Don't require a JWT on the webhook path.
"""

import os
from datetime import datetime, timedelta, timezone

import jwt  # PyJWT
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-env")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = int(os.environ.get("JWT_EXPIRY_MINUTES", "60"))

bearer_scheme = HTTPBearer(auto_error=False)


def create_access_token(subject: str, extra_claims: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRY_MINUTES),
        **(extra_claims or {}),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),  # noqa: B008 — FastAPI dependency injection pattern
) -> dict:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return decode_access_token(credentials.credentials)
