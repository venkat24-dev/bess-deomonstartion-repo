"""OIDC authentication tests use generated signing keys and mocked local user lookup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from bess import authentication


ISSUER = "https://identity.example.test/"
AUDIENCE = "bess-sizing-api"
SETTINGS = authentication.OIDCSettings(
    issuer=ISSUER,
    audience=AUDIENCE,
    jwks_url="https://identity.example.test/.well-known/jwks.json",
)


@pytest.fixture
def signing_key():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private_key,
        private_key.public_key(),
    )


def _token(private_key, **overrides):
    now = datetime.now(timezone.utc)
    claims = {
        "iss": ISSUER,
        "sub": "verified-subject",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _mock_key_client(monkeypatch, public_key):
    monkeypatch.setattr(
        authentication,
        "_jwks_client",
        lambda _url: SimpleNamespace(
            get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public_key)
        ),
    )


def _credentials(token="not-a-token"):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_authentication_requires_bearer_credentials():
    with pytest.raises(HTTPException) as error:
        authentication.get_current_user(None)
    assert error.value.status_code == 401
    assert error.value.headers["WWW-Authenticate"] == "Bearer"


def test_oidc_configuration_fails_closed_without_provider_values(monkeypatch):
    monkeypatch.setattr(authentication, "load_dotenv", lambda *_args, **_kwargs: None)
    for name in ("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(authentication.AuthenticationConfigurationError):
        authentication.OIDCSettings.from_environment()


def test_oidc_configuration_requires_https(monkeypatch):
    monkeypatch.setattr(authentication, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("OIDC_ISSUER", "http://identity.example.test/")
    monkeypatch.setenv("OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("OIDC_JWKS_URL", "https://identity.example.test/jwks")
    with pytest.raises(authentication.AuthenticationConfigurationError, match="HTTPS"):
        authentication.OIDCSettings.from_environment()


def test_valid_signed_token_maps_issuer_subject_and_checks_active_status(
    monkeypatch, signing_key
):
    private_key, public_key = signing_key
    _mock_key_client(monkeypatch, public_key)
    monkeypatch.setattr(
        authentication.OIDCSettings,
        "from_environment",
        classmethod(lambda _cls: SETTINGS),
    )
    user_id = uuid4()
    resolved = SimpleNamespace(
        id=user_id,
        oidc_issuer=ISSUER,
        oidc_subject="verified-subject",
        is_active=True,
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalar(self, statement):
            sql = str(statement)
            assert "oidc_issuer" in sql and "oidc_subject" in sql
            return resolved

    monkeypatch.setattr(authentication, "get_session_factory", lambda: lambda: FakeSession())
    user = authentication.get_current_user(_credentials(_token(private_key)))
    assert user.id == user_id
    assert user.oidc_issuer == ISSUER
    assert user.oidc_subject == "verified-subject"


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://wrong.example.test/"},
        {"aud": "another-api"},
        {"exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
    ],
)
def test_invalid_oidc_claims_are_rejected(monkeypatch, signing_key, claims):
    private_key, public_key = signing_key
    _mock_key_client(monkeypatch, public_key)
    with pytest.raises(HTTPException) as error:
        authentication._verified_identity(_token(private_key, **claims), SETTINGS)
    assert error.value.status_code == 401


def test_bad_signature_is_rejected(monkeypatch, signing_key):
    private_key, _public_key = signing_key
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _mock_key_client(monkeypatch, other_key.public_key())
    with pytest.raises(HTTPException) as error:
        authentication._verified_identity(_token(private_key), SETTINGS)
    assert error.value.status_code == 401


@pytest.mark.parametrize(
    ("user", "expected_status"),
    [
        (None, 403),
        (
            SimpleNamespace(
                id=uuid4(),
                oidc_issuer=ISSUER,
                oidc_subject="verified-subject",
                is_active=False,
            ),
            403,
        ),
    ],
)
def test_unprovisioned_or_inactive_users_are_denied(
    monkeypatch, signing_key, user, expected_status
):
    private_key, public_key = signing_key
    _mock_key_client(monkeypatch, public_key)
    monkeypatch.setattr(
        authentication.OIDCSettings,
        "from_environment",
        classmethod(lambda _cls: SETTINGS),
    )

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def scalar(self, _statement):
            return user

    monkeypatch.setattr(authentication, "get_session_factory", lambda: lambda: FakeSession())
    with pytest.raises(HTTPException) as error:
        authentication.get_current_user(_credentials(_token(private_key)))
    assert error.value.status_code == expected_status
