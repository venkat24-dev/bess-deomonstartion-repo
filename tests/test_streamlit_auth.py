from __future__ import annotations

from types import SimpleNamespace

import pytest

from bess.project_workspace import sizing_idempotency_key
from bess.streamlit_auth import (
    APIAccessTokenUnavailable,
    api_access_token,
    bind_authenticated_user,
    clear_authenticated_session,
    is_logged_in,
)


def test_access_token_requires_an_authenticated_session():
    assert not is_logged_in(SimpleNamespace())
    assert api_access_token(SimpleNamespace(is_logged_in=False)) is None
    with pytest.raises(APIAccessTokenUnavailable, match="did not expose"):
        api_access_token(SimpleNamespace(is_logged_in=True, tokens={}))


def test_access_token_reads_only_access_token_and_rejects_invalid_values():
    assert api_access_token(
        SimpleNamespace(
            is_logged_in=True,
            tokens={"access": "api-access-token", "id": "identity-token"},
        )
    ) == "api-access-token"
    with pytest.raises(APIAccessTokenUnavailable, match="invalid API access token"):
        api_access_token(
            SimpleNamespace(is_logged_in=True, tokens={"access": "bad token"})
        )


def test_logout_or_account_change_clears_all_session_state():
    state = {
        "bess_workspace_project_id": "project-A",
        "bess_latest_run_result": {"private": "result"},
        "bess_unsaved_calculation": True,
        "calculator_input": 42,
    }
    clear_authenticated_session(state)
    assert state == {}


def test_authenticated_account_switch_clears_old_user_data():
    state = {
        "bess_workspace_user_id": "user-A",
        "bess_workspace_project_id": "project-A",
        "bess_latest_run_result": {"private": "result"},
        "bess_unsaved_calculation": True,
    }
    assert not bind_authenticated_user(state, "user-B")
    assert state == {}
    assert bind_authenticated_user(state, "user-B")
    assert state == {"bess_workspace_user_id": "user-B"}


def test_idempotency_key_is_reused_for_retry_and_scoped_to_payload_and_identity():
    state = {}
    request = {"container_id": "container-A", "required_energy_mwh": 100.0}
    first = sizing_idempotency_key(state, "user-A", "project-A", request)
    retry = sizing_idempotency_key(state, "user-A", "project-A", request)
    changed_request = sizing_idempotency_key(
        state,
        "user-A",
        "project-A",
        {"container_id": "container-A", "required_energy_mwh": 110.0},
    )
    changed_user = sizing_idempotency_key(state, "user-B", "project-A", request)
    assert first == retry
    assert changed_request != first
    assert changed_user != first
