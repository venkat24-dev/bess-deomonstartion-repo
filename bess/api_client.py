"""Authenticated HTTP boundary for Streamlit-to-FastAPI requests."""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx


class BESSAPIError(RuntimeError):
    """Safe-to-display backend connectivity or response error."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validated_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("BESS API URL must be an absolute HTTP(S) URL without credentials or query data.")
    if parsed.scheme != "https" and not _is_loopback(parsed.hostname):
        raise ValueError("BESS API must use HTTPS outside a loopback development host.")
    return base_url.rstrip("/")


class BESSAPIClient:
    """Small synchronous API client; callers supply a trusted user's current access token."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not access_token or access_token.strip() != access_token or any(
            character.isspace() for character in access_token
        ):
            raise ValueError("A non-empty bearer access token is required.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self._client = httpx.Client(
            base_url=_validated_base_url(base_url),
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> BESSAPIClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def list_containers(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/v1/containers")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise BESSAPIError("Backend returned an invalid container list.")
        return result

    def get_catalog_manifest(self) -> dict[str, Any]:
        result = self._object("GET", "/api/v1/catalog/manifest")
        digest = result.get("catalog_sha256")
        containers = result.get("containers")
        container_ids = result.get("container_ids")
        if (
            result.get("schema_version") != "catalog-manifest-v1"
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(containers, list)
            or any(
                not isinstance(container, dict)
                or not isinstance(container.get("id"), str)
                or not isinstance(container.get("label"), str)
                or not isinstance(container.get("energy_mwh"), (int, float))
                or not isinstance(container.get("c_rate"), (int, float))
                or not isinstance(container.get("max_years"), int)
                for container in containers
            )
            or not isinstance(container_ids, list)
            or container_ids != [container["id"] for container in containers]
        ):
            raise BESSAPIError("Backend returned an invalid catalog manifest.")
        return result

    def create_project(
        self,
        organization_id: str,
        name: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        return self._object(
            "POST",
            "/api/v1/projects",
            json={
                "organization_id": organization_id,
                "name": name,
                "description": description,
            },
        )

    def get_profile(self) -> dict[str, Any]:
        result = self._object("GET", "/api/v1/me")
        user = result.get("user")
        organizations = result.get("organizations")
        if (
            not isinstance(user, dict)
            or not isinstance(user.get("id"), str)
            or user.get("account_status") not in {"active", "inactive"}
            or not isinstance(user.get("created_at"), str)
            or not isinstance(organizations, list)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("name"), str)
                or item.get("role")
                not in {"owner", "admin", "manager", "member"}
                for item in organizations
            )
        ):
            raise BESSAPIError("Backend returned an invalid profile.")
        return result

    def list_projects(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/v1/projects")
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise BESSAPIError("Backend returned an invalid project list.")
        return result

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._object("GET", f"/api/v1/projects/{quote(project_id, safe='')}")

    def archive_project(
        self, project_id: str, expected_project_version: int
    ) -> dict[str, Any]:
        if expected_project_version < 1:
            raise ValueError("expected_project_version must be positive.")
        return self._object(
            "POST",
            f"/api/v1/projects/{quote(project_id, safe='')}/archive",
            json={"expected_project_version": expected_project_version},
        )

    def list_project_versions(self, project_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET", f"/api/v1/projects/{quote(project_id, safe='')}/versions"
        )
        if not isinstance(result, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("version_number"), int)
            or not isinstance(item.get("created_at"), str)
            for item in result
        ):
            raise BESSAPIError("Backend returned an invalid project version list.")
        return result

    def get_project_version(self, project_id: str, version_number: int) -> dict[str, Any]:
        if version_number < 1:
            raise ValueError("version_number must be positive.")
        return self._object(
            "GET",
            f"/api/v1/projects/{quote(project_id, safe='')}/versions/{version_number}",
        )

    def save_project_version(
        self,
        project_id: str,
        simulation_request_id: str,
        expected_current_version: int,
        change_summary: str | None = None,
    ) -> dict[str, Any]:
        return self._object(
            "POST",
            f"/api/v1/projects/{quote(project_id, safe='')}/versions",
            json={
                "simulation_request_id": simulation_request_id,
                "expected_current_version": expected_current_version,
                "change_summary": change_summary,
            },
        )

    def restore_project_version(
        self,
        project_id: str,
        version_number: int,
        expected_current_version: int,
        change_summary: str | None = None,
    ) -> dict[str, Any]:
        if version_number < 1:
            raise ValueError("version_number must be positive.")
        return self._object(
            "POST",
            f"/api/v1/projects/{quote(project_id, safe='')}/versions/{version_number}/restore",
            json={
                "expected_current_version": expected_current_version,
                "change_summary": change_summary,
            },
        )

    def update_project(self, project_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self._object(
            "PATCH",
            f"/api/v1/projects/{quote(project_id, safe='')}",
            json=changes,
        )

    def add_project_member(
        self,
        project_id: str,
        user_id: str,
        role: str,
    ) -> dict[str, Any]:
        return self._object(
            "POST",
            f"/api/v1/projects/{quote(project_id, safe='')}/members",
            json={"user_id": user_id, "role": role},
        )

    def update_project_member_role(
        self, project_id: str, user_id: str, role: str
    ) -> dict[str, Any]:
        return self._object(
            "PATCH",
            (
                f"/api/v1/projects/{quote(project_id, safe='')}/members/"
                f"{quote(user_id, safe='')}/role"
            ),
            json={"role": role},
        )

    def remove_project_member(self, project_id: str, user_id: str) -> None:
        result = self._request(
            "DELETE",
            (
                f"/api/v1/projects/{quote(project_id, safe='')}/members/"
                f"{quote(user_id, safe='')}"
            ),
        )
        if result is not None:
            raise BESSAPIError("Backend returned an unexpected response for member removal.")

    def submit_sizing(
        self,
        request: dict[str, Any],
        catalog_sha256: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if re.fullmatch(r"[0-9a-f]{64}", catalog_sha256) is None:
            raise ValueError("catalog_sha256 must be a lowercase SHA-256 digest.")
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        result = self._object(
            "POST",
            "/api/v1/sizing",
            json={**request, "catalog_sha256": catalog_sha256},
            headers=headers,
        )
        required = {
            "request_id",
            "container_id",
            "accepted",
            "margin_applied_pct",
            "in_range",
            "note",
            "summary",
            "discharging",
            "charging",
            "performance",
            "verification",
            "effective_inputs",
            "catalog_sha256",
            "warnings",
        }
        if not required.issubset(result):
            raise BESSAPIError("Backend returned a sizing result with an unsupported shape.")
        if result["catalog_sha256"] != catalog_sha256:
            raise BESSAPIError("Backend sizing result used a different catalog than the verified manifest.")
        if not isinstance(result["effective_inputs"], dict) or not isinstance(
            result["warnings"], list
        ):
            raise BESSAPIError("Backend returned invalid sizing context.")
        yearly_fields = ("discharging", "charging", "performance", "verification")
        if any(
            not isinstance(result[field], list)
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("Year"), int)
                or isinstance(row.get("Year"), bool)
                for row in result[field]
            )
            for field in yearly_fields
        ):
            raise BESSAPIError("Backend returned yearly sizing records without valid year labels.")
        return result

    def get_sizing(self, request_id: str) -> dict[str, Any]:
        return self._object(
            "GET",
            f"/api/v1/sizing/{quote(request_id, safe='')}",
        )

    def list_recent_sizing_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50.")
        result = self._request("GET", f"/api/v1/sizing/recent?limit={limit}")
        if not isinstance(result, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("request_id"), str)
            or not isinstance(item.get("created_at"), str)
            or not isinstance(item.get("status"), str)
            for item in result
        ):
            raise BESSAPIError("Backend returned an invalid recent sizing activity list.")
        return result

    def _object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        result = self._request(method, path, **kwargs)
        if not isinstance(result, dict):
            raise BESSAPIError("Backend returned an invalid response object.")
        return result

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("X-Correlation-ID", str(uuid4()))
        try:
            response = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BESSAPIError("Backend request timed out.") from exc
        except httpx.RequestError as exc:
            raise BESSAPIError("Backend is unavailable.") from exc
        if not response.is_success:
            error_code = None
            try:
                error_detail = response.json().get("detail")
                if isinstance(error_detail, dict):
                    candidate = error_detail.get("code")
                    if isinstance(candidate, str):
                        error_code = candidate
            except (AttributeError, ValueError):
                pass
            status_messages = {
                401: "Your sign-in has expired. Sign in again to continue.",
                403: "You do not have permission to perform this action.",
                404: "The requested project or record is unavailable.",
                409: "The project changed or conflicts with this request. Refresh and retry.",
            }
            if error_code == "catalog_mismatch":
                message = (
                    "The server catalog changed; refresh the catalog and retry the sizing run."
                )
            elif response.status_code in status_messages:
                message = status_messages[response.status_code]
            elif response.status_code >= 500:
                message = "The backend is temporarily unavailable. Try again later."
            else:
                message = f"Backend request could not be completed (HTTP {response.status_code})."
            raise BESSAPIError(
                message,
                status_code=response.status_code,
                error_code=error_code,
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise BESSAPIError("Backend returned invalid JSON.") from exc
