from pathlib import Path

from streamlit.testing.v1 import AppTest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _select_page(app, page):
    navigation = next(
        widget for widget in app.radio if widget.key == "bess_workspace_navigation"
    )
    navigation.set_value(page).run()
    assert not app.exception


def _assert_project_workspace_not_rendered(app):
    assert "Projects" not in [widget.value for widget in app.subheader]
    workspace_controls = {
        "Search projects",
        "Project status",
        "Select a project",
        "Create project",
        "Open project",
        "Run Calculation",
        "Required energy as per project (MWh)",
    }
    rendered_widget_labels = {
        widget.label
        for element_type in ("text_input", "selectbox", "button")
        for widget in getattr(app, element_type)
    }
    assert workspace_controls.isdisjoint(rendered_widget_labels)


def test_workspace_navigation_renders_only_selected_page():
    app = AppTest.from_file(
        str(PROJECT_ROOT / "app.py"), default_timeout=120
    ).run()
    assert not app.exception
    assert "Projects" in [widget.value for widget in app.subheader]
    assert not app.tabs
    assert not any(
        "BESS Sizing Software" in widget.value for widget in app.title
    )

    _select_page(app, "Profile")
    assert "Profile Dashboard" in [widget.value for widget in app.title]
    _assert_project_workspace_not_rendered(app)
    assert "BESS Sizing Software" not in [widget.value for widget in app.title]
    assert not app.tabs
    assert any(
        "Sign in with company SSO" in widget.value for widget in app.info
    )

    _select_page(app, "Home / Projects")
    assert "Projects" in [widget.value for widget in app.subheader]
    assert any(
        "Project storage requires company SSO" in widget.value
        for widget in app.info
    )
    assert not any("Profile Dashboard" == widget.value for widget in app.title)
    assert not app.tabs

    _select_page(app, "Profile")
    assert "Profile Dashboard" in [widget.value for widget in app.title]
    _assert_project_workspace_not_rendered(app)
    assert not app.tabs

    _select_page(app, "Calculator")
    assert any("BESS Sizing Software" in widget.value for widget in app.title)
    assert any(widget.label == "Calculator - Inputs" for widget in app.tabs)
    assert any(
        widget.label == "Required energy as per project (MWh)"
        for widget in app.number_input
    )

    _select_page(app, "Profile")
    assert "Profile Dashboard" in [widget.value for widget in app.title]
    assert not app.tabs
