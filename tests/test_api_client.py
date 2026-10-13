"""Mocked tests for the Streamlit-to-FastAPI integration boundary."""
from __future__ import annotations

import json

import httpx
import pytest

from bess.api_client import BESSAPIClient, BESSAPIError


SIZING_RESULT = {
    "request_id": "11111111-1111-1111-1111-111111111111",
    "container_id": "5.015MWh_0.25C",
    "accepted": True,
    "margin_applied_pct": 14.0,
    "in_range": True,
    "note": "OK",
    "summary": {"n_init": 20},
    "discharging": [{"Year": 0}],
    "charging": [{"Year": 0}],
    "performance": [{"Year": 0}],
    "verification": [{"Year": 0}],
    "effective_inputs": {},
    "catalog_sha256": "a" * 64,
    "warnings": [],
}
CATALOG_SHA256 = "a" * 64
CATALOG_MANIFEST = {
    "schema_version": "catalog-manifest-v1",
    "catalog_sha256": CATALOG_SHA256,
    "container_ids": ["5.015MWh_0.25C"],
    "containers": [
        {
            "id": "5.015MWh_0.25C",
            "label": "5.015 MWh @ 0.25C",
            "energy_mwh": 5.015,
            "c_rate": 0.25,
            "max_years": 15,
        }
    ],
}


def _client(handler):
    return BESSAPIClient(
        "https://bess-api.example.test/",
        "access-token-for-test",
        transport=httpx.MockTransport(handler),
    )


def test_submit_sizing_preserves_response_and_sends_bearer_and_idempotency():
    observed = {}

    def handler(request):
        if request.url.path == "/api/v1/catalog/manifest":
            return httpx.Response(200, json=CATALOG_MANIFEST)
        observed["authorization"] = request.headers["Authorization"]
        observed["idempotency_key"] = request.headers["Idempotency-Key"]
        observed["path"] = request.url.path
        observed["body"] = json.loads(request.content)
        return httpx.Response(201, json=SIZING_RESULT)

    payload = {"container_id": "5.015MWh_0.25C", "required_energy_mwh": 100}
    with _client(handler) as client:
        manifest = client.get_catalog_manifest()
        result = client.submit_sizing(
            payload,
            catalog_sha256=manifest["catalog_sha256"],
            idempotency_key="repeat-safe-key",
        )

    assert observed == {
        "authorization": "Bearer access-token-for-test",
        "idempotency_key": "repeat-safe-key",
        "path": "/api/v1/sizing",
        "body": {**payload, "catalog_sha256": CATALOG_SHA256},
    }
    assert result == SIZING_RESULT


def test_get_catalog_manifest_validates_and_returns_catalog_identity():
    def handler(request):
        assert request.url.path == "/api/v1/catalog/manifest"
        return httpx.Response(200, json=CATALOG_MANIFEST)

    with _client(handler) as client:
        assert client.get_catalog_manifest() == CATALOG_MANIFEST


@pytest.mark.parametrize(
    "manifest",
    [
        {**CATALOG_MANIFEST, "catalog_sha256": "invalid"},
        {**CATALOG_MANIFEST, "schema_version": "unknown"},
        {**CATALOG_MANIFEST, "container_ids": []},
    ],
)
def test_get_catalog_manifest_rejects_invalid_contract(manifest):
    def handler(_request):
        return httpx.Response(200, json=manifest)

    with _client(handler) as client:
        with pytest.raises(BESSAPIError, match="invalid catalog manifest"):
            client.get_catalog_manifest()


def test_get_sizing_preserves_persisted_request_envelope():
    expected = {
        "request_id": SIZING_RESULT["request_id"],
        "request": {"container_id": SIZING_RESULT["container_id"]},
        "response": SIZING_RESULT,
    }

    def handler(request):
        assert request.url.path.endswith(SIZING_RESULT["request_id"])
        return httpx.Response(200, json=expected)

    with _client(handler) as client:
        assert client.get_sizing(SIZING_RESULT["request_id"]) == expected


def test_project_api_calls_use_the_authenticated_transport():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer access-token-for-test"
        assert request.url.path == "/api/v1/projects"
        return httpx.Response(200, json=[{"id": "project-1"}])

    with _client(handler) as client:
        assert client.list_projects() == [{"id": "project-1"}]


@pytest.mark.parametrize(
    ("base_url", "token"),
    [
        ("http://bess-api.example.test", "access-token"),
        ("https://user:password@bess-api.example.test", "access-token"),
        ("https://bess-api.example.test?token=secret", "access-token"),
        ("https://bess-api.example.test", ""),
        ("https://bess-api.example.test", "token with whitespace"),
    ],
)
def test_client_rejects_insecure_or_invalid_configuration(base_url, token):
    with pytest.raises(ValueError):
        BESSAPIClient(base_url, token)


def test_loopback_http_is_allowed_for_local_development():
    with BESSAPIClient(
        "http://127.0.0.1:8000",
        "access-token-for-test",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[])
        ),
    ) as client:
        assert client.list_containers() == []


def test_backend_error_does_not_include_response_or_token():
    def handler(_request):
        return httpx.Response(500, text="sensitive backend exception body")

    with _client(handler) as client:
        with pytest.raises(BESSAPIError) as error:
            client.list_projects()

    assert error.value.status_code == 500
    assert "sensitive" not in str(error.value)
    assert "access-token-for-test" not in str(error.value)


def test_invalid_backend_result_shape_is_rejected():
    def handler(_request):
        return httpx.Response(201, json={"not": "a sizing response"})

    with _client(handler) as client:
        with pytest.raises(BESSAPIError, match="unsupported shape"):
            client.submit_sizing(
                {"container_id": "5.015MWh_0.25C"},
                catalog_sha256=CATALOG_SHA256,
            )


def test_sizing_response_requires_year_labels_in_every_yearly_table():
    malformed = {**SIZING_RESULT, "performance": [{"Net power at POI (MW)": 2.0}]}

    def handler(_request):
        return httpx.Response(201, json=malformed)

    with _client(handler) as client:
        with pytest.raises(BESSAPIError, match="year labels"):
            client.submit_sizing(
                {"container_id": "5.015MWh_0.25C"},
                catalog_sha256=CATALOG_SHA256,
            )


def test_sizing_response_catalog_hash_must_match_verified_manifest():
    mismatched = {**SIZING_RESULT, "catalog_sha256": "b" * 64}

    def handler(_request):
        return httpx.Response(201, json=mismatched)

    with _client(handler) as client:
        with pytest.raises(BESSAPIError, match="different catalog"):
            client.submit_sizing(
                {"container_id": "5.015MWh_0.25C"},
                catalog_sha256=CATALOG_SHA256,
            )


def test_catalog_conflict_is_reported_safely():
    def handler(_request):
        return httpx.Response(
            409,
            json={
                "detail": {
                    "code": "catalog_mismatch",
                    "message": "Do not expose arbitrary response text.",
                }
            },
        )

    with _client(handler) as client:
        with pytest.raises(BESSAPIError, match="retrieve the manifest") as error:
            client.submit_sizing(
                {"container_id": "5.015MWh_0.25C"},
                catalog_sha256=CATALOG_SHA256,
            )
    assert error.value.status_code == 409
    assert error.value.error_code == "catalog_mismatch"
    assert "arbitrary response text" not in str(error.value)


def test_sizing_rejects_malformed_catalog_hash_before_request():
    with _client(lambda _request: pytest.fail("unexpected HTTP request")) as client:
        with pytest.raises(ValueError, match="lowercase SHA-256"):
            client.submit_sizing(
                {"container_id": "5.015MWh_0.25C"},
                catalog_sha256="not-a-digest",
            )


def test_transport_failure_is_reported_without_credentials():
    def handler(_request):
        raise httpx.ConnectError("private transport diagnostic")

    with _client(handler) as client:
        with pytest.raises(BESSAPIError, match="Backend is unavailable") as error:
            client.list_projects()

    assert "private transport diagnostic" not in str(error.value)
    assert "access-token-for-test" not in str(error.value)
