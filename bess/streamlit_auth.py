"""Safe access-token boundary for Streamlit's native OIDC session."""
from __future__ import annotations

from typing import Any


class APIAccessTokenUnavailable(RuntimeError):
    """The Streamlit identity session has no configured API access token."""


def is_logged_in(user: Any) -> bool:
    try:
        return bool(user.is_logged_in)
    except AttributeError:
        return False


def api_access_token(user: Any) -> str | None:
    if not is_logged_in(user):
        return None
    try:
        token = user.tokens["access"]
    except (AttributeError, KeyError) as exc:
        raise APIAccessTokenUnavailable(
            "The identity provider did not expose an API access token. "
            "Configure Streamlit auth.expose_tokens = ['access'] and an API scope."
        ) from exc
    if (
        not isinstance(token, str)
        or not token
        or token.strip() != token
        or any(character.isspace() for character in token)
    ):
        raise APIAccessTokenUnavailable(
            "The identity provider returned an invalid API access token."
        )
    return token


def clear_authenticated_session(state: Any) -> None:
    """Drop all identity-scoped and calculator widget state on logout/account change."""
    state.clear()


def bind_authenticated_user(state: Any, user_id: str) -> bool:
    """Bind the session to one backend identity; clear state when identity changes."""
    previous_user_id = state.get("bess_workspace_user_id")
    if previous_user_id is not None and previous_user_id != user_id:
        clear_authenticated_session(state)
        return False
    state["bess_workspace_user_id"] = user_id
    return True
