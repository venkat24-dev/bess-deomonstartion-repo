from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from bess.dashboard import (
    _finite_status,
    _verification_status,
    build_performance_figures,
    render_engineering_dashboard,
)
from bess.database import load_database, select_by_c_rate
from bess.engine import AugEvent, Inputs, compute_case


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE = PROJECT_ROOT / "data" / "BESS_DATABASE.xlsx"


@pytest.fixture(scope="module")
def case_result():
    container = select_by_c_rate(load_database(DATABASE), 0.25)[0]
    return compute_case(
        container,
        24,
        Inputs(
            project_years=3,
            cycles_required=1000,
            aux_required=True,
            aux_overrides={"discharge": 0.01, "charge": 0.01},
            plant_infra_power_mw=0.1,
        ),
    )


def test_performance_charts_use_engine_values_and_project_years(case_result):
    figures = build_performance_figures(case_result)
    discharge = figures["discharge"]
    net_poi = next(
        trace for trace in discharge.data if trace.name == "Net POI discharge energy"
    )
    assert list(net_poi.x) == list(case_result.perf.index)
    assert list(net_poi.y) == pytest.approx(
        case_result.perf["Net discharge energy at POI (MWh)"].tolist()
    )

    rte = figures["rte"]
    required_rte = next(trace for trace in rte.data if trace.name == "Minimum required RTE")
    assert list(required_rte.y) == pytest.approx(
        case_result.verify["Required RTE (%)"].tolist()
    )
    power = figures["power"]
    required_power = next(trace for trace in power.data if trace.name == "Required project power")
    assert list(required_power.y) == pytest.approx(
        case_result.verify["Required power (MW)"].tolist()
    )


def test_optional_columns_can_be_absent(case_result):
    partial = replace(
        case_result,
        dis=case_result.dis.drop(
            columns=["Installed DC capacity (MWh)", "SOH (%)"]
        ),
        chg=case_result.chg.drop(columns=["Guaranteed DC charge capacity (MWh)"]),
        perf=case_result.perf.drop(columns=["AC RTE excl. aux (%)"]),
    )

    figures = build_performance_figures(partial)

    assert "Installed DC capacity" not in [trace.name for trace in figures["discharge"].data]
    assert "Engine-modelled SOH" not in [trace.name for trace in figures["lifecycle"].data]
    assert "Guaranteed DC charge" not in [trace.name for trace in figures["charging"].data]
    assert "RTE excl. auxiliary consumption" not in [
        trace.name for trace in figures["rte"].data
    ]


def test_non_finite_plot_points_are_gaps_and_source_result_is_not_mutated(case_result):
    discharge = case_result.dis.copy()
    column = "Guaranteed DC discharge capacity (MWh)"
    original_value = discharge.loc[1, column]
    discharge.loc[1, column] = np.inf
    altered = replace(case_result, dis=discharge)

    figure = build_performance_figures(altered)["discharge"]
    trace = next(trace for trace in figure.data if trace.name == "Guaranteed DC discharge")

    assert trace.y[1] is None
    assert altered.dis.loc[1, column] == np.inf
    assert case_result.dis.loc[1, column] == original_value


def test_lifecycle_and_augmentation_chart_use_configured_engine_data(case_result):
    augmented = compute_case(
        case_result.container,
        case_result.n_init,
        replace(
            case_result.inputs,
            aug_enabled=True,
            aug_events=[AugEvent(year=2, containers=3)],
        ),
    )
    figures = build_performance_figures(augmented)
    installed_additions = next(
        trace
        for trace in figures["capacity"].data
        if trace.name == "Added / replacement DC capacity"
    )

    assert list(installed_additions.x) == list(augmented.dis.index)
    assert installed_additions.y[0] == 0
    assert installed_additions.y[1] == 0
    assert installed_additions.y[2] == pytest.approx(
        augmented.dis.loc[2, "Augmented capacity (MWh)"]
    )
    soh = next(trace for trace in figures["lifecycle"].data if trace.name == "Engine-modelled SOH")
    assert list(soh.y) == pytest.approx(augmented.dis["SOH (%)"].tolist())
    assert [(event.year, event.containers) for event in augmented.inputs.aug_events] == [
        (2, 3)
    ]


@pytest.mark.parametrize(
    ("passed", "method", "expected"),
    [(True, "success", "PASS"), (False, "error", "FAIL"), (None, "info", "NOT AVAILABLE")],
)
def test_verification_status_displays_pass_fail_or_unavailable(passed, method, expected):
    messages = []
    stub = SimpleNamespace(
        **{
            name: (lambda message, name=name: messages.append((name, message)))
            for name in ("success", "error", "info")
        }
    )

    _verification_status(stub, "Energy", passed)

    assert messages == [(method, f"Energy: {expected}")]


def test_non_finite_verification_values_are_not_reported_as_pass():
    assert _finite_status(True, np.inf, 100.0) is None
    assert _finite_status(True, np.nan, 100.0) is None
    assert _finite_status(True, 100.0, 100.0) is True


def test_dashboard_has_an_empty_state_without_a_case_result():
    messages = []
    stub = SimpleNamespace(
        subheader=lambda _label: None,
        info=messages.append,
    )

    render_engineering_dashboard(stub, None)

    assert messages == [
        "No successful calculation is available. Configure the calculator inputs and run sizing first."
    ]


def test_calculation_error_state_does_not_render_results():
    messages = []
    stub = SimpleNamespace(
        subheader=lambda _label: None,
        error=messages.append,
    )

    render_engineering_dashboard(stub, None, error="Sizing calculation failed.")

    assert messages == ["Sizing calculation failed."]


def test_engineering_dashboard_streamlit_apptest():
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py"), default_timeout=120).run()
    next(
        widget for widget in app.radio if widget.key == "bess_workspace_navigation"
    ).set_value("Calculator").run()

    assert not app.exception
    assert "Engineering dashboard" in [widget.value for widget in app.subheader]
    assert any(widget.label == "Engineering Dashboard" for widget in app.tabs)
    assert app.get("plotly_chart")

    app.session_state["bess_calculator_tabs"] = "Calculator - Inputs"
    app.run()
    assert not app.exception
    assert "Engineering dashboard" not in [
        widget.value for widget in app.subheader
    ]
    assert not app.get("plotly_chart")
