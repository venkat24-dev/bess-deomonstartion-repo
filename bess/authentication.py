"""Fail-closed OIDC bearer-token validation and local user resolution."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient, PyJWTError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from bess.db_connection import DatabaseConfigurationError, get_session_factory
from bess.models import User

logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


class AuthenticationConfigurationError(RuntimeError):
    """OIDC validation cannot be safely configured."""


@dataclass(frozen=True)
class OIDCSettings:
    issuer: str
    audience: str
    jwks_url: str

    @classmethod
    def from_environment(cls) -> OIDCSettings:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        issuer = os.environ.get("OIDC_ISSUER", "").strip()
        audience = os.environ.get("OIDC_AUDIENCE", "").strip()
        jwks_url = os.environ.get("OIDC_JWKS_URL", "").strip()
        if not issuer or not audience or not jwks_url:
            raise AuthenticationConfigurationError(
                "OIDC_ISSUER, OIDC_AUDIENCE, and OIDC_JWKS_URL must all be configured."
            )
        issuer_url = urlsplit(issuer)
        jwks_endpoint = urlsplit(jwks_url)
        if (
            issuer_url.scheme != "https"
            or not issuer_url.hostname
            or issuer_url.username
            or issuer_url.password
            or jwks_endpoint.scheme != "https"
            or not jwks_endpoint.hostname
            or jwks_endpoint.username
            or jwks_endpoint.password
        ):
            raise AuthenticationConfigurationError("OIDC issuer and JWKS URL must use HTTPS.")
        return cls(issuer=issuer, audience=audience, jwks_url=jwks_url)


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID
    oidc_issuer: str
    oidc_subject: str


@lru_cache(maxsize=4)
def _jwks_client(jwks_url: str) -> PyJWKClient:
    return PyJWKClient(jwks_url, timeout=5)


def _verified_identity(token: str, settings: OIDCSettings) -> tuple[str, str]:
    try:
        signing_key = _jwks_client(settings.jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.audience,
            issuer=settings.issuer,
            leeway=30,
            options={"require": ["iss", "sub", "aud", "exp", "iat"]},
        )
    except (PyJWTError, OSError, TimeoutError, ValueError) as exc:
        raise HTTPException(
            status_code=401,
            detail="Bearer token is invalid or expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject or len(subject) > 500:
        raise HTTPException(
            status_code=401,
            detail="Bearer token has an invalid subject.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims["iss"], subject


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Bearer authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        settings = OIDCSettings.from_environment()
    except AuthenticationConfigurationError as exc:
        raise HTTPException(status_code=503, detail="OIDC authentication is not configured.") from exc

    issuer, subject = _verified_identity(credentials.credentials, settings)
    try:
        with get_session_factory()() as session:
            user = session.scalar(
                select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
            )
            if user is None:
                raise HTTPException(status_code=403, detail="Authenticated user is not provisioned.")
            if not user.is_active:
                raise HTTPException(status_code=403, detail="User account is inactive.")
            return AuthenticatedUser(id=user.id, oidc_issuer=issuer, oidc_subject=subject)
    except HTTPException:
        raise
    except (DatabaseConfigurationError, SQLAlchemyError) as exc:
        logger.error("Could not resolve authenticated user (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Authentication service is unavailable.") from exc
