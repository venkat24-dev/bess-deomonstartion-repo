"""Streamlit rendering for authenticated projects and backend sizing versions."""
from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from bess.api_client import BESSAPIClient, BESSAPIError


def profile_initials(name: str | None, email: str | None = None) -> str:
    if isinstance(name, str) and name.strip():
        parts = name.strip().split()
    else:
        local_part = (email or "").split("@", 1)[0]
        parts = [part for part in local_part.replace(".", " ").replace("_", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def organization_role_label(role: str) -> str:
    return {
        "owner": "Organization owner",
        "admin": "Organization administrator",
        "manager": "Organization manager",
        "member": "Organization member",
    }.get(role, "Unrecognized organization role")


def filter_projects(
    projects: list[dict[str, Any]], search: str, status: str
) -> list[dict[str, Any]]:
    query = search.casefold().strip()
    return [
        project
        for project in projects
        if (status == "All" or (
            status == "Archived" and project.get("archived_at")
        ) or (
            status == "Active" and not project.get("archived_at")
        ))
        and (
            not query
            or query in str(project.get("name", "")).casefold()
            or query in str(project.get("description", "")).casefold()
            or query in str(project.get("owner_display_name", "")).casefold()
        )
    ]


def version_snapshot_differences(
    first: dict[str, Any], second: dict[str, Any]
) -> list[dict[str, str]]:
    differences: list[dict[str, str]] = []
    missing = object()

    def format_value(value: Any) -> str:
        return "(missing)" if value is missing else json.dumps(
            value, sort_keys=True, default=str
        )

    def walk(path: str, left: Any, right: Any) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                left_value = left[key] if key in left else missing
                right_value = right[key] if key in right else missing
                walk(f"{path}.{key}" if path else key, left_value, right_value)
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                left_value = left[index] if index < len(left) else missing
                right_value = right[index] if index < len(right) else missing
                walk(f"{path}[{index}]", left_value, right_value)
            return

        left_text = format_value(left)
        right_text = format_value(right)
        if left_text != right_text:
            differences.append(
                {
                    "Field": path,
                    "Version A": left_text[:500],
                    "Version B": right_text[:500],
                }
            )

    walk("", first, second)
    return differences


def _input_fingerprint(request: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _clear_project_calculation_state(state: Any) -> None:
    for key in (
        "bess_current_input_configuration",
        "bess_latest_successful_run_id",
        "bess_latest_run_result",
        "bess_latest_run_scope",
        "bess_unsaved_calculation",
        "bess_saved_version_number",
        "bess_pending_sizing_key",
    ):
        state.pop(key, None)


def sizing_idempotency_key(
    state: Any, user_id: str, project_id: str, request: dict[str, Any]
) -> str:
    fingerprint = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    current = state.get("bess_pending_sizing_key")
    if (
        isinstance(current, dict)
        and current.get("user_id") == user_id
        and current.get("project_id") == project_id
        and current.get("fingerprint") == fingerprint
        and isinstance(current.get("key"), str)
    ):
        return current["key"]
    key = str(uuid4())
    state["bess_pending_sizing_key"] = {
        "user_id": user_id,
        "project_id": project_id,
        "fingerprint": fingerprint,
        "key": key,
    }
    return key


def _show_api_error(st: Any, error: BESSAPIError) -> None:
    if error.status_code == 409:
        st.warning(
            "The project changed while you were working. Refresh the project before retrying; "
            "your existing project version was not overwritten."
        )
    else:
        st.error(str(error))


def render_profile(
    st: Any,
    profile: dict[str, Any] | None,
    projects: list[dict[str, Any]] | None = None,
    recent_runs: list[dict[str, Any]] | None = None,
) -> None:
    st.title("Profile Dashboard")
    if profile is None:
        st.info("" \
        "")
        return
    user = profile["user"]
    name = user.get("display_name") or "Name not provided"
    email = user.get("email") or "Email not provided"
    organizations = profile.get("organizations", [])
    with st.container(border=True):
        avatar, identity, status = st.columns([1, 5, 2])
        with avatar:
            st.subheader(profile_initials(user.get("display_name"), user.get("email")))
        with identity:
            st.subheader(name)
            st.caption(email)
            st.caption(f"Account ID: {user['id']}")
        with status:
            st.success("SSO authenticated")
            st.info("HR verification unavailable")

    projects = projects or []
    recent_runs = recent_runs or []
    stat_projects, stat_runs, stat_orgs = st.columns(3)
    stat_projects.metric("Accessible projects", len(projects))
    stat_runs.metric("Recent sizing activity", len(recent_runs))
    stat_orgs.metric("Organization memberships", len(organizations))

    st.subheader("Account and security")
    with st.container(border=True):
        details_left, details_right = st.columns(2)
        details_left.write(
            f"**Account status:** {user.get('account_status', 'Not available').title()}"
        )
        details_left.write(f"**Created:** {user.get('created_at', 'Not available')}")
        details_left.write("**Last login:** Not available from the identity API")
        details_right.write("**Employee / contractor ID:** Not provided")
        details_right.write("**Department / designation:** Not provided")
        details_right.write("**Employment verification:** Pending; no HR verification integration is configured")
        details_right.write("**Account type:** Not provided by the identity source")

    st.subheader("Organization and effective roles")
    if organizations:
        st.dataframe(
            [
                {
                    "Organization": item["name"],
                    "Effective organization role": organization_role_label(item["role"]),
                    "Backend role": item["role"],
                }
                for item in organizations
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Organization roles are scoped permissions, not employment classifications. "
            "A member role does not establish employee status, and a manager role does not grant "
            "unrestricted project access."
        )
    else:
        st.info("No active organization memberships were returned.")

    st.subheader("My projects")
    if projects:
        st.dataframe(
            [
                {
                    "Project": item.get("name", ""),
                    "Your project role": item.get("current_user_role") or "Explicit manager grant",
                    "Current version": item.get("current_version_number") or "No saved versions",
                    "Status": "Archived" if item.get("archived_at") else "Active",
                    "Last modified": item.get("updated_at", "Not available"),
                }
                for item in projects[:10]
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("No accessible projects are available.")

    st.subheader("Recent activity")
    if recent_runs:
        names = {item["id"]: item.get("name", "") for item in projects}
        st.dataframe(
            [
                {
                    "Activity": "Sizing run",
                    "Project": names.get(run.get("project_id"), "Projectless run"),
                    "Status": run.get("status", "Unknown"),
                    "Created": run.get("created_at", "Not available"),
                }
                for run in recent_runs[:10]
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No recent activity is available.")

    st.warning(
        "Employee-number verification is not available in this deployment. Do not enter an "
        "employee number here; an authorized HR/identity administrator must provision or "
        "approve employment details through an approved source."
    )


def render_project_workspace(
    st: Any,
    client: BESSAPIClient | None,
    profile: dict[str, Any] | None,
    projects: list[dict[str, Any]] | None,
    recent_runs: list[dict[str, Any]] | None = None,
) -> None:
    st.subheader("Projects")
    if client is None or profile is None or projects is None:
        st.info(
            "Project storage requires company SSO and an API access token. "
            "The standalone calculator remains available on the Calculator page."
        )
        return

    st.caption(
        "Project sizing runs use the backend's bundled catalog and the supported API inputs. "
        "The separate Calculator retains the full local workbook, analysis and report workflow."
    )
    user_id = profile["user"]["id"]
    if st.session_state.get("bess_workspace_user_id") != user_id:
        st.session_state["bess_workspace_user_id"] = user_id
        st.session_state.pop("bess_workspace_project_id", None)
        _clear_project_calculation_state(st.session_state)
        st.session_state.pop("bess_workspace_review", None)
        st.session_state.pop("bess_workspace_comparison", None)

    st.subheader("My projects")
    search = st.text_input(
        "Search projects",
        placeholder="Search by project name, description, or owner",
        key="bess_project_search",
    )
    status_filter = st.selectbox(
        "Project status",
        ["All", "Active", "Archived"],
        key="bess_project_status_filter",
    )
    visible_projects = filter_projects(projects, search, status_filter)
    st.metric("Matching projects", len(visible_projects))
    if visible_projects:
        st.dataframe(
            [
                {
                    "Project": item.get("name", ""),
                    "Owner": item.get("owner_display_name") or "Owner name unavailable",
                    "Your role": item.get("current_user_role") or "Explicit manager grant",
                    "Current version": item.get("current_version_number") or "No saved versions",
                    "Status": "Archived" if item.get("archived_at") else "Active",
                    "Last modified": item.get("updated_at", "Not available"),
                }
                for item in visible_projects
            ],
            hide_index=True,
            width="stretch",
        )
    elif projects:
        st.info("No projects match these search and status filters.")
    else:
        st.info("You do not have access to any projects yet.")

    if projects:
        if recent_runs:
            project_names = {item["id"]: item.get("name", "") for item in projects}
            st.markdown("**Recent sizing activity**")
            st.dataframe(
                [
                    {
                        "Created": item["created_at"],
                        "Project": project_names.get(item.get("project_id"), "Projectless run"),
                        "Container": item.get("container_id") or "",
                        "Status": item["status"],
                    }
                    for item in recent_runs
                ],
                hide_index=True,
                width="stretch",
            )
        elif recent_runs == []:
            st.caption("No recent sizing activity is available.")

    with st.expander("Create a project"):
        memberships = [
            item
            for item in profile.get("organizations", [])
            if item.get("role") in {"owner", "member"}
        ]
        if not memberships:
            st.info("An active organization membership is required to create a project.")
        else:
            with st.form("bess_create_project_form"):
                organization_id = st.selectbox(
                    "Organization",
                    [item["id"] for item in memberships],
                    format_func=lambda value: next(
                        item["name"] for item in memberships if item["id"] == value
                    ),
                )
                project_name = st.text_input("Project name", max_chars=300)
                project_description = st.text_area("Description (optional)")
                create_submitted = st.form_submit_button("Create project")
            if create_submitted:
                try:
                    with st.spinner("Creating project..."):
                        created = client.create_project(
                            organization_id,
                            project_name.strip(),
                            project_description.strip() or None,
                        )
                except BESSAPIError as exc:
                    _show_api_error(st, exc)
                else:
                    _clear_project_calculation_state(st.session_state)
                    st.session_state["bess_workspace_project_id"] = created["id"]
                    st.success("Project created and saved.")
                    st.rerun()

    if not visible_projects:
        return
    project_ids = [item["id"] for item in visible_projects]
    current_selection = st.session_state.get("bess_workspace_project_id")
    selected_index = project_ids.index(current_selection) if current_selection in project_ids else 0
    with st.form("bess_open_project_form"):
        selected_id = st.selectbox(
            "Open a project",
            project_ids,
            index=selected_index,
            format_func=lambda value: next(
                item.get("name", value)
                for item in visible_projects
                if item["id"] == value
            ),
        )
        open_submitted = st.form_submit_button("Open project")
    if open_submitted:
        if selected_id != st.session_state.get("bess_workspace_project_id"):
            _clear_project_calculation_state(st.session_state)
            st.session_state.pop("bess_workspace_review", None)
            st.session_state.pop("bess_workspace_comparison", None)
        st.session_state["bess_workspace_project_id"] = selected_id
        st.rerun()

    project_id = st.session_state.get("bess_workspace_project_id")
    if project_id not in project_ids:
        return
    try:
        with st.spinner("Loading project and version history..."):
            project = client.get_project(project_id)
            versions = sorted(
                client.list_project_versions(project_id),
                key=lambda item: item["version_number"],
                reverse=True,
            )
    except BESSAPIError as exc:
        _show_api_error(st, exc)
        return

    st.markdown(f"### {project.get('name', 'Project')}")
    project_summary = next(
        (item for item in visible_projects if item["id"] == project_id), {}
    )
    is_archived = bool(project.get("archived_at"))
    st.caption(
        f"Last updated: {project.get('updated_at', 'Unknown')} · "
        f"Current version: {project.get('current_version_number') or 'No saved versions'} · "
        f"Owner: {project_summary.get('owner_display_name') or 'Owner name unavailable'} · "
        f"Status: {'Archived' if is_archived else 'Active'}"
    )
    if project.get("description"):
        st.write(project["description"])
    if project.get("current_user_role") == "owner" and not is_archived:
        with st.expander("Archive project"):
            st.warning("Archiving prevents new sizing runs and project changes.")
            with st.form(f"bess_archive_project_{project_id}"):
                archive_confirmed = st.checkbox(
                    "I understand this project will become read-only"
                )
                archive_submitted = st.form_submit_button(
                    "Archive project", disabled=not archive_confirmed
                )
            if archive_submitted:
                try:
                    with st.spinner("Archiving project..."):
                        client.archive_project(project_id, int(project["version"]))
                except BESSAPIError as exc:
                    _show_api_error(st, exc)
                else:
                    _clear_project_calculation_state(st.session_state)
                    st.success("Project archived.")
                    st.rerun()
    if is_archived:
        st.info("This project is archived. Existing history is read-only.")
    if project.get("current_user_role") == "owner" and not is_archived:
        with st.expander("Share project"):
            st.caption(
                "Share with an already provisioned member of this project's organization. "
                "The API checks the target identity and role; use their backend user ID."
            )
            with st.form(f"bess_share_project_{project_id}"):
                member_user_id = st.text_input("Member user ID")
                member_role = st.selectbox("Project role", ["editor", "viewer"])
                share_submitted = st.form_submit_button("Add project member")
            if share_submitted:
                try:
                    with st.spinner("Updating project access..."):
                        client.add_project_member(
                            project_id, member_user_id.strip(), member_role
                        )
                except BESSAPIError as exc:
                    _show_api_error(st, exc)
                else:
                    st.success("Project access updated.")
            with st.form(f"bess_manage_project_member_{project_id}"):
                managed_user_id = st.text_input("Existing member user ID")
                member_action = st.selectbox(
                    "Access change", ["Change role", "Remove access"]
                )
                updated_role = st.selectbox(
                    "New project role", ["editor", "viewer"]
                )
                manage_submitted = st.form_submit_button("Apply access change")
            if manage_submitted:
                try:
                    with st.spinner("Updating project access..."):
                        if member_action == "Change role":
                            client.update_project_member_role(
                                project_id, managed_user_id.strip(), updated_role
                            )
                        else:
                            client.remove_project_member(
                                project_id, managed_user_id.strip()
                            )
                except BESSAPIError as exc:
                    _show_api_error(st, exc)
                else:
                    st.success("Project access updated.")
    if is_archived:
        st.info("Archived projects are read-only; sizing, sharing, and restore are disabled.")
    st.markdown("#### Run project calculation")
    try:
        manifest = client.get_catalog_manifest()
    except BESSAPIError as exc:
        _show_api_error(st, exc)
        return
    containers = manifest["containers"]
    container_id = st.selectbox(
        "Container option",
        [item["id"] for item in containers],
        format_func=lambda value: next(
            f"{item['label']} — {item['energy_mwh']:g} MWh"
            for item in containers
            if item["id"] == value
        ),
    )
    left, right = st.columns(2)
    required_energy = left.number_input(
        "Required energy (MWh)", min_value=0.1, max_value=1_000_000.0,
        value=100.0, step=1.0,
        key=f"remote_energy_{project_id}",
    )
    required_power = right.number_input(
        "Required power (MW)", min_value=0.1, max_value=100_000.0,
        value=25.0, step=0.5,
        key=f"remote_power_{project_id}",
    )
    left, right = st.columns(2)
    c_rate = left.number_input(
        "C-rate", min_value=0.01, max_value=2.0, value=0.25, step=0.01,
        key=f"remote_crate_{project_id}",
    )
    project_years = right.number_input(
        "Project duration (years)", min_value=1, max_value=15, value=10, step=1,
        key=f"remote_years_{project_id}",
    )
    left, right = st.columns(2)
    operating_temp = left.number_input(
        "Operating temperature (°C)", min_value=-20.0, max_value=60.0,
        value=25.0, step=1.0, key=f"remote_temp_{project_id}",
    )
    availability = right.number_input(
        "Availability (%)", min_value=50.0, max_value=100.0, value=99.0,
        step=0.1, key=f"remote_availability_{project_id}",
    )
    left, right = st.columns(2)
    dod = left.number_input(
        "DOD (%)", min_value=10.0, max_value=100.0, value=100.0,
        step=1.0, key=f"remote_dod_{project_id}",
    )
    aux_required = right.checkbox(
        "Auxiliary consumption required", value=True,
        key=f"remote_aux_{project_id}",
    )
    left, right = st.columns(2)
    bol_energy = left.number_input(
        "BOL energy requirement (MWh)", min_value=0.1, max_value=1_000_000.0,
        value=float(required_energy), step=1.0,
        key=f"remote_bol_{project_id}",
    )
    eol_energy = right.number_input(
        "EOL energy requirement (MWh)", min_value=0.1, max_value=1_000_000.0,
        value=float(required_energy), step=1.0,
        key=f"remote_eol_{project_id}",
    )
    left, right = st.columns(2)
    min_rte = left.number_input(
        "Minimum EOL RTE (%)", min_value=10.0, max_value=100.0,
        value=80.0, step=0.5, key=f"remote_min_rte_{project_id}",
    )
    cycles = right.number_input(
        "Required cycles", min_value=1, max_value=100_000,
        value=min(365 * int(project_years), 100_000), step=1,
        key=f"remote_cycles_{project_id}",
    )
    mismatch = st.number_input(
        "Constant mismatch loss (%)", min_value=0.0, max_value=50.0,
        value=1.0, step=0.1, key=f"remote_mismatch_{project_id}",
    )
    left, right = st.columns(2)
    margin_min = left.number_input(
        "Minimum energy margin (%)", min_value=0.0, max_value=100.0,
        value=14.0, step=0.5, key=f"remote_margin_min_{project_id}",
    )
    margin_max = right.number_input(
        "Maximum energy margin (%)", min_value=0.0, max_value=100.0,
        value=20.0, step=0.5, key=f"remote_margin_max_{project_id}",
    )
    manual_containers = st.number_input(
        "Fixed container count (0 = automatic)", min_value=0, max_value=100_000,
        value=0, step=1, key=f"remote_manual_containers_{project_id}",
    )
    allow_extended = st.checkbox(
        "Allow extended margin search", value=True,
        key=f"remote_extended_margin_{project_id}",
    )
    run_submitted = st.button(
        "Run Calculation", key=f"bess_run_calc_{project_id}", disabled=is_archived
    )
    sizing_request = {
        "project_id": project_id,
        "container_id": container_id,
        "required_energy_mwh": required_energy,
        "required_power_mw": required_power,
        "c_rate": c_rate,
        "project_years": int(project_years),
        "operating_temp_c": operating_temp,
        "availability_pct": availability,
        "dod_pct": dod,
        "aux_required": aux_required,
        "bol_energy_mwh": bol_energy,
        "eol_energy_mwh": eol_energy,
        "min_rte_eol_pct": min_rte,
        "cycles_required": int(cycles),
        "mismatch_constant_pct": mismatch,
        "margin_min_pct": margin_min,
        "margin_max_pct": margin_max,
        "manual_containers": int(manual_containers),
        "allow_extended_margin": allow_extended,
    }
    current_fingerprint = _input_fingerprint(sizing_request)
    st.session_state["bess_current_input_configuration"] = {
        "user_id": user_id,
        "project_id": project_id,
        "request": sizing_request,
        "fingerprint": current_fingerprint,
    }
    latest_run_scope = st.session_state.get("bess_latest_run_scope")
    if (
        isinstance(latest_run_scope, dict)
        and latest_run_scope.get("user_id") == user_id
        and latest_run_scope.get("project_id") == project_id
        and latest_run_scope.get("input_fingerprint") != current_fingerprint
    ):
        _clear_project_calculation_state(st.session_state)
        st.session_state["bess_current_input_configuration"] = {
            "user_id": user_id,
            "project_id": project_id,
            "request": sizing_request,
            "fingerprint": current_fingerprint,
        }
        st.warning("Sizing inputs changed. Run the calculation again before saving a version.")
    if run_submitted:
        if margin_max < margin_min:
            st.error("Maximum energy margin must be greater than or equal to the minimum.")
            return
        try:
            with st.spinner("Running calculation and recording the sizing run..."):
                result = client.submit_sizing(
                    sizing_request,
                    catalog_sha256=manifest["catalog_sha256"],
                    idempotency_key=sizing_idempotency_key(
                        st.session_state, user_id, project_id, sizing_request
                    ),
                )
        except BESSAPIError as exc:
            _show_api_error(st, exc)
        else:
            st.session_state.pop("bess_pending_sizing_key", None)
            st.session_state["bess_latest_successful_run_id"] = result["request_id"]
            st.session_state["bess_latest_run_result"] = result
            st.session_state["bess_latest_run_scope"] = {
                "user_id": user_id,
                "project_id": project_id,
                "input_fingerprint": current_fingerprint,
            }
            st.session_state["bess_unsaved_calculation"] = True
            st.session_state.pop("bess_saved_version_number", None)
            st.success("Calculated — Not Saved as Project Version.")
    saved_run = st.session_state.get("bess_latest_run_result")
    run_scope = st.session_state.get("bess_latest_run_scope")
    if (
        isinstance(saved_run, dict)
        and isinstance(run_scope, dict)
        and run_scope.get("user_id") == user_id
        and run_scope.get("project_id") == project_id
        and run_scope.get("input_fingerprint") == current_fingerprint
        and st.session_state.get("bess_latest_successful_run_id") == saved_run.get("request_id")
    ):
        result = saved_run
        st.markdown("#### Calculation results")
        if st.session_state.get("bess_unsaved_calculation"):
            st.warning("Calculated — Not Saved as Project Version.")
        else:
            st.success(
                f"Saved project version {st.session_state.get('bess_saved_version_number')}."
            )
        st.metric("Sizing outcome", "Accepted" if result["accepted"] else "Not accepted")
        st.caption(result["note"])
        st.json(result["summary"])
        for warning in result["warnings"]:
            st.warning(warning)
        with st.expander("Year-by-year verification", expanded=True):
            st.dataframe(result["verification"], hide_index=True, width="stretch")
        with st.expander("Discharging results"):
            st.dataframe(result["discharging"], hide_index=True, width="stretch")
        with st.expander("Charging results"):
            st.dataframe(result["charging"], hide_index=True, width="stretch")
        with st.expander("Power and performance"):
            st.dataframe(result["performance"], hide_index=True, width="stretch")
        if st.session_state.get("bess_unsaved_calculation"):
            with st.form(f"bess_save_version_{project_id}"):
                change_summary = st.text_input(
                    "Version description / change summary", max_chars=1000
                )
                save_submitted = st.form_submit_button(
                    "Save Version", disabled=is_archived
                )
        else:
            save_submitted = False
            change_summary = ""
        if save_submitted:
            run_id = st.session_state["bess_latest_successful_run_id"]
            try:
                with st.spinner("Saving immutable project version..."):
                    saved_version = client.save_project_version(
                        project_id,
                        run_id,
                        int(project.get("current_version_number") or 0),
                        change_summary.strip() or None,
                    )
            except BESSAPIError as exc:
                _show_api_error(st, exc)
            else:
                version_number = saved_version["version_number"]
                st.session_state["bess_unsaved_calculation"] = False
                st.session_state["bess_saved_version_number"] = version_number
                st.success(f"Project version {version_number} saved.")
                project["current_version_number"] = version_number
                try:
                    versions = sorted(
                        client.list_project_versions(project_id),
                        key=lambda item: item["version_number"],
                        reverse=True,
                    )
                except BESSAPIError as exc:
                    st.warning(
                        "The version was saved, but version history could not be refreshed: "
                        + str(exc)
                    )

    st.markdown("#### Version history")
    if not versions:
        st.info("No project versions have been saved.")
        return
    st.dataframe(
        [
            {
                "Version": item["version_number"],
                "Author ID": item.get("created_by_user_id", ""),
                "Created": item.get("created_at", ""),
                "Summary": item.get("change_summary") or "",
                "Restored from": item.get("source_version_number") or "",
                "Current": item["version_number"] == project.get("current_version_number"),
            }
            for item in versions
        ],
        hide_index=True,
        width="stretch",
    )
    version_numbers = [item["version_number"] for item in versions]
    if len(version_numbers) >= 2:
        with st.form(f"bess_compare_versions_{project_id}"):
            compare_left, compare_right = st.columns(2)
            first_version_number = compare_left.selectbox(
                "Compare version A",
                version_numbers,
                index=0,
                format_func=lambda value: f"Version {value}",
            )
            second_version_number = compare_right.selectbox(
                "Compare version B",
                version_numbers,
                index=len(version_numbers) - 1,
                format_func=lambda value: f"Version {value}",
            )
            compare_submitted = st.form_submit_button("Compare versions")
        if compare_submitted:
            try:
                first_version = client.get_project_version(
                    project_id, first_version_number
                )
                second_version = client.get_project_version(
                    project_id, second_version_number
                )
            except BESSAPIError as exc:
                _show_api_error(st, exc)
            else:
                if not all(
                    isinstance(version.get(snapshot_name), dict)
                    for version in (first_version, second_version)
                    for snapshot_name in ("input_snapshot", "result_snapshot")
                ):
                    st.error("One or both saved versions have an unsupported snapshot format.")
                else:
                    st.session_state["bess_workspace_comparison"] = {
                        "user_id": user_id,
                        "project_id": project_id,
                        "version_a": first_version_number,
                        "version_b": second_version_number,
                        "input_differences": version_snapshot_differences(
                            first_version["input_snapshot"],
                            second_version["input_snapshot"],
                        ),
                        "result_differences": version_snapshot_differences(
                            first_version["result_snapshot"],
                            second_version["result_snapshot"],
                        ),
                    }
        comparison = st.session_state.get("bess_workspace_comparison")
        if (
            isinstance(comparison, dict)
            and comparison.get("user_id") == user_id
            and comparison.get("project_id") == project_id
        ):
            st.markdown(
                f"**Version {comparison['version_a']} → Version {comparison['version_b']}**"
            )
            for label, key in (
                ("Input snapshot differences", "input_differences"),
                ("Result snapshot differences", "result_differences"),
            ):
                st.markdown(f"_{label}_")
                differences = comparison[key]
                if differences:
                    st.dataframe(
                        differences[:200], hide_index=True, width="stretch"
                    )
                    if len(differences) > 200:
                        st.caption(
                            f"Showing the first 200 of {len(differences)} changed fields."
                        )
                else:
                    st.caption("No differences.")

    selected_version = st.selectbox(
        "Review a saved version",
        version_numbers,
        format_func=lambda value: f"Version {value}",
    )
    if st.button("Load version for review", key=f"bess_review_{project_id}"):
        try:
            version = client.get_project_version(project_id, selected_version)
        except BESSAPIError as exc:
            _show_api_error(st, exc)
        else:
            for snapshot_name in (
                "input_snapshot",
                "result_snapshot",
                "selected_container_snapshot",
            ):
                if not isinstance(version.get(snapshot_name), dict):
                    st.error("This saved version has an unsupported snapshot format.")
                    return
            st.session_state["bess_workspace_review"] = {
                "user_id": user_id,
                "project_id": project_id,
                "version": version,
            }
    review = st.session_state.get("bess_workspace_review")
    if (
        isinstance(review, dict)
        and review.get("user_id") == user_id
        and review.get("project_id") == project_id
    ):
        version = review["version"]
        st.caption(
            f"Reviewing version {version['version_number']} created by "
            f"{version.get('created_by_user_id', 'unknown')} at {version.get('created_at', 'unknown')}."
        )
        st.json(version["input_snapshot"])
        st.json(version["result_snapshot"])
        with st.form(f"bess_restore_version_{project_id}_{selected_version}"):
            restore_summary = st.text_input(
                "Restore summary (optional)", max_chars=1000
            )
            restore_submitted = st.form_submit_button(
                f"Restore version {selected_version} as a new version",
                disabled=is_archived,
            )
        if restore_submitted:
            try:
                with st.spinner("Restoring as a new immutable version..."):
                    restored = client.restore_project_version(
                        project_id,
                        selected_version,
                        int(project.get("current_version_number") or 0),
                        restore_summary.strip()
                        or f"Restored from version {selected_version}",
                    )
            except BESSAPIError as exc:
                _show_api_error(st, exc)
            else:
                st.success(
                    f"Restored as new project version {restored['version_number']}."
                )
                st.rerun()
