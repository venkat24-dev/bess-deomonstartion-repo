from __future__ import annotations

from bess.project_workspace import (
    _clear_project_calculation_state,
    filter_projects,
    organization_role_label,
    profile_initials,
    version_snapshot_differences,
)


def test_profile_initials_use_name_then_email_fallback():
    assert profile_initials("Ada Lovelace") == "AL"
    assert profile_initials(None, "ada.lovelace@example.test") == "AL"
    assert profile_initials(None, None) == "?"


def test_organization_role_labels_are_not_employment_classifications():
    assert organization_role_label("owner") == "Organization owner"
    assert organization_role_label("manager") == "Organization manager"
    assert organization_role_label("unknown") == "Unrecognized organization role"


def test_project_filters_search_authorized_rows_and_archived_state():
    projects = [
        {"name": "North", "description": "Solar site", "owner_display_name": "A. Owner"},
        {
            "name": "South",
            "description": "Storage site",
            "owner_display_name": "B. Owner",
            "archived_at": "2026-01-01T00:00:00+00:00",
        },
    ]

    assert [item["name"] for item in filter_projects(projects, "solar", "Active")] == [
        "North"
    ]
    assert [item["name"] for item in filter_projects(projects, "", "Archived")] == [
        "South"
    ]
    assert [item["name"] for item in filter_projects(projects, "b. owner", "All")] == [
        "South"
    ]


def test_version_snapshot_comparison_reports_nested_changes_and_added_fields():
    differences = version_snapshot_differences(
        {"engine": {"energy": 100}, "result": {"accepted": True}},
        {"engine": {"energy": 120}, "result": {"accepted": True}, "note": "updated"},
    )

    assert differences == [
        {"Field": "engine.energy", "Version A": "100", "Version B": "120"},
        {"Field": "note", "Version A": "(missing)", "Version B": '"updated"'},
    ]


def test_project_switch_clears_run_and_unsaved_version_state():
    state = {
        "bess_current_input_configuration": {"project_id": "project-A"},
        "bess_latest_successful_run_id": "run-A",
        "bess_latest_run_result": {"request_id": "run-A"},
        "bess_latest_run_scope": {"project_id": "project-A"},
        "bess_unsaved_calculation": True,
        "bess_saved_version_number": 2,
        "bess_pending_sizing_key": "idempotency-key",
        "other_state": "preserved",
    }

    _clear_project_calculation_state(state)

    assert state == {"other_state": "preserved"}
