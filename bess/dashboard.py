"""Engineering dashboard views built from existing BESS calculation results."""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from .engine import CaseResult

_COLORS = {
    "blue": "#2673B8",
    "teal": "#159A9C",
    "green": "#20834A",
    "amber": "#C58A12",
    "red": "#C43D3D",
    "gray": "#6B7280",
}


def _finite(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _yearly_series(
    frame: pd.DataFrame,
    columns: Iterable[tuple[str, str]],
) -> list[tuple[str, str, list[float | None]]]:
    if not isinstance(frame, pd.DataFrame):
        return []
    years = [_finite(value) for value in frame.index]
    if not any(year is not None for year in years):
        return []
    series = []
    for label, column in columns:
        if column not in frame.columns:
            continue
        values = [_finite(value) for value in frame[column].tolist()]
        if any(value is not None for value in values):
            series.append((label, column, values))
    return [
        (label, column, values)
        for label, column, values in series
    ]


def _safe_frame(frame: pd.DataFrame) -> pd.DataFrame:
    safe = frame.copy()
    for column in safe.columns:
        if pd.api.types.is_numeric_dtype(safe[column]):
            safe[column] = pd.to_numeric(safe[column], errors="coerce").replace(
                [np.inf, -np.inf], np.nan
            )
    return safe


def _format_value(value: Any, suffix: str, decimals: int = 2) -> str:
    numeric = _finite(value)
    if numeric is None:
        return "Not available"
    return f"{numeric:,.{decimals}f}{suffix}"


def _line_chart(
    frame: pd.DataFrame,
    columns: Iterable[tuple[str, str]],
    title: str,
    y_title: str,
) -> go.Figure:
    figure = go.Figure()
    series = _yearly_series(frame, columns)
    years = [_finite(value) for value in frame.index] if isinstance(frame, pd.DataFrame) else []
    for index, (label, _column, values) in enumerate(series):
        figure.add_trace(
            go.Scatter(
                x=years,
                y=values,
                mode="lines+markers",
                name=label,
                connectgaps=False,
                line={"color": list(_COLORS.values())[index % len(_COLORS)]},
                hovertemplate=f"Year %{{x}}<br>{label}: %{{y:.3f}}<extra></extra>",
            )
        )
    figure.update_layout(
        title=title,
        xaxis_title="Project year",
        yaxis_title=y_title,
        hovermode="x unified",
        legend={"orientation": "h", "y": -0.25},
        margin={"l": 20, "r": 20, "t": 55, "b": 70},
    )
    return figure


def build_performance_figures(result: CaseResult) -> dict[str, go.Figure]:
    """Build charts from actual result columns, skipping unavailable optional fields."""
    dis = result.dis
    chg = result.chg
    perf = result.perf
    verify = result.verify
    discharge_figure = _line_chart(
        dis,
        [
            ("Installed DC capacity", "Installed DC capacity (MWh)"),
            ("Guaranteed DC discharge", "Guaranteed DC discharge capacity (MWh)"),
            ("Guaranteed POI energy, excl. aux", "Guaranteed capacity at POI excl. aux (MWh)"),
        ],
        "Discharge capacity by project year",
        "Energy (MWh)",
    )
    net_poi_trace = _line_chart(
        perf,
        [("Net POI discharge energy", "Net discharge energy at POI (MWh)")],
        "",
        "Energy (MWh)",
    )
    discharge_figure.add_traces(net_poi_trace.data)

    charging_figure = _line_chart(
        chg,
        [
            ("Guaranteed DC charge", "Guaranteed DC charge capacity (MWh)"),
            ("AC charge, excl. aux", "Capacity at POI excl. aux (MWh)"),
            ("AC charge, incl. aux", "Capacity at POI incl. aux (MWh)"),
            ("Net AC charge incl. infrastructure", "AC MV energy incl. augmentation & aux at POI (MWh)"),
        ],
        "Charging energy by project year",
        "Energy (MWh)",
    )
    charge_efficiency = _line_chart(
        chg,
        [("DC-DC RTE", "DC-DC RTE (%)")],
        "",
        "Efficiency (%)",
    )
    for trace in charge_efficiency.data:
        trace.yaxis = "y2"
        charging_figure.add_trace(trace)
    if charge_efficiency.data:
        charging_figure.update_layout(
            yaxis2={
                "title": "DC-DC RTE (%)",
                "overlaying": "y",
                "side": "right",
            }
        )

    rte_figure = _line_chart(
        perf,
        [
            ("RTE incl. auxiliary consumption", "AC RTE incl. aux (%)"),
            ("RTE excl. auxiliary consumption", "AC RTE excl. aux (%)"),
        ],
        "Round-trip efficiency by project year",
        "Efficiency (%)",
    )
    required_rte = _line_chart(
        verify,
        [("Minimum required RTE", "Required RTE (%)")],
        "",
        "Efficiency (%)",
    )
    rte_figure.add_traces(required_rte.data)

    power_figure = _line_chart(
        perf,
        [("Available net power at POI", "Net power at POI (MW)")],
        "Available power by project year",
        "Power (MW)",
    )
    required_power = _line_chart(
        verify,
        [("Required project power", "Required power (MW)")],
        "",
        "Power (MW)",
    )
    power_figure.add_traces(required_power.data)
    capacity_figure = _line_chart(
        result.dis,
        [
            ("Initial installed DC capacity", "Installed DC capacity (MWh)"),
            ("Added / replacement DC capacity", "Augmented capacity (MWh)"),
            ("Guaranteed DC discharge capacity", "Guaranteed DC discharge capacity (MWh)"),
        ],
        "Installed and guaranteed capacity",
        "Energy (MWh)",
    )
    for trace in capacity_figure.data:
        if trace.name == "Added / replacement DC capacity":
            trace.line.shape = "hv"
    return {
        "discharge": discharge_figure,
        "charging": charging_figure,
        "rte": rte_figure,
        "power": power_figure,
        "lifecycle": _line_chart(
            result.dis,
            [
                ("Engine-modelled SOH", "SOH (%)"),
                ("Calendar degradation factor", "Calendar degradation factor (%)"),
            ],
            "Modelled degradation factors",
            "Factor (%)",
        ),
        "capacity": capacity_figure,
    }


def _metric(st: Any, label: str, value: Any, suffix: str = "") -> None:
    numeric = _finite(value)
    rendered = "Not available" if numeric is None else f"{numeric:,.2f}{suffix}"
    st.metric(label, rendered, border=True)


def _verification_status(st: Any, label: str, passed: Any) -> None:
    if isinstance(passed, (bool, np.bool_)):
        if bool(passed):
            st.success(f"{label}: PASS")
        else:
            st.error(f"{label}: FAIL")
    else:
        st.info(f"{label}: NOT AVAILABLE")


def _finite_status(passed: Any, *values: Any) -> bool | None:
    if not isinstance(passed, (bool, np.bool_)):
        return None
    if any(_finite(value) is None for value in values):
        return None
    return bool(passed)


def _render_kpis(st: Any, result: CaseResult) -> None:
    summary = result.summary
    metric_rows = [
        (
            ("Required energy", result.inputs.required_energy_mwh, " MWh"),
            ("Required power", result.inputs.required_power_mw, " MW"),
            ("Installed DC energy", summary.get("installed_mwh"), " MWh"),
            ("Initial containers", summary.get("n_init"), ""),
        ),
        (
            ("BOL guaranteed energy", summary.get("bol_energy"), " MWh"),
            ("EOL guaranteed energy", summary.get("eol_energy"), " MWh"),
            ("EOL RTE incl. aux", summary.get("rte_eol_incl"), " %"),
            ("AC-chain efficiency", summary.get("chain_eff_pct"), " %"),
        ),
    ]
    for row in metric_rows:
        columns = st.columns(4)
        for column, (label, value, suffix) in zip(columns, row):
            with column:
                _metric(st, label, value, suffix)


def _render_configuration(st: Any, result: CaseResult) -> None:
    summary = result.summary
    st.subheader("Battery configuration")
    st.caption("Calculated design configuration; this is not a physical rack layout.")
    stages = [
        ("Selected battery container", summary.get("container", result.container.label)),
        ("Container energy rating", _format_value(result.container.energy_mwh, " MWh", 3)),
        ("Initial container quantity", f"{result.n_init:,}"),
        ("Initial installed DC capacity", _format_value(summary.get("installed_mwh"), " MWh", 3)),
        (
            "BOL / EOL guaranteed POI energy",
            f"{_format_value(summary.get('bol_energy'), ' MWh', 3)} / "
            f"{_format_value(summary.get('eol_energy'), ' MWh', 3)}",
        ),
        ("Applied energy margin", _format_value(summary.get("effective_margin_pct"), " %")),
    ]
    for index, (label, value) in enumerate(stages):
        with st.container(border=True):
            st.markdown(f"**{label}**")
            st.write(value)
        if index < len(stages) - 1:
            st.markdown("↓")


def _render_power_flow(st: Any, result: CaseResult) -> None:
    st.subheader("Energy conversion and power design flow")
    st.caption("Year-wise calculated design values only; no live SCADA or instantaneous telemetry is available.")
    year_zero = 0
    dis = result.dis
    chg = result.chg
    perf = result.perf
    battery_value = (
        dis.loc[year_zero, "Guaranteed DC discharge capacity (MWh)"]
        if "Guaranteed DC discharge capacity (MWh)" in dis.columns and year_zero in dis.index
        else None
    )
    poi_value = (
        perf.loc[year_zero, "Net discharge energy at POI (MWh)"]
        if "Net discharge energy at POI (MWh)" in perf.columns and year_zero in perf.index
        else None
    )
    stages = [
        ("Battery containers", f"{result.n_init} initial units"),
        ("DC-side guaranteed energy", _format_value(battery_value, " MWh")),
        ("AC conversion chain", _format_value(result.summary.get("chain_eff_pct"), " %")),
        ("Net energy at POI", _format_value(poi_value, " MWh")),
    ]
    for index, (label, value) in enumerate(stages):
        with st.container(border=True):
            st.markdown(f"**{label}**")
            st.write(value)
        if index < len(stages) - 1:
            st.markdown("↓")

    flow_values = [
        ("Available net discharge power at POI", perf, "Net power at POI (MW)", year_zero, " MW"),
        ("Net charge energy at POI", chg, "AC MV energy incl. augmentation & aux at POI (MWh)", year_zero, " MWh"),
        ("Discharge auxiliary energy", dis, "Aux energy (MWh)", year_zero, " MWh"),
        ("Plant infrastructure energy", dis, "Plant infrastructure energy (MWh)", year_zero, " MWh"),
    ]
    columns = st.columns(2)
    for index, (label, frame, field, year, unit) in enumerate(flow_values):
        with columns[index % len(columns)]:
            if field in frame.columns and year in frame.index:
                _metric(st, label, frame.loc[year, field], unit)
            else:
                st.metric(label, "Not available", border=True)


def _render_charts(st: Any, result: CaseResult) -> None:
    figures = build_performance_figures(result)
    st.subheader("Year-wise performance")
    for key, label in (
        ("discharge", "Discharge performance"),
        ("charging", "Charging performance"),
    ):
        figure = figures[key]
        if figure.data:
            st.plotly_chart(figure, width="stretch", theme="streamlit", key=f"bess_dashboard_{key}")
        else:
            st.info(f"{label}: required calculation columns are unavailable.")
    left, right = st.columns(2)
    for column, key in ((left, "rte"), (right, "power")):
        with column:
            figure = figures[key]
            if figure.data:
                st.plotly_chart(figure, width="stretch", theme="streamlit", key=f"bess_dashboard_{key}")
            else:
                st.info(f"{key.title()} chart: required calculation columns are unavailable.")


def _render_lifecycle(st: Any, result: CaseResult) -> None:
    st.subheader("Battery lifecycle")
    st.caption("SOH shown here is the sizing model's catalog-derived SOH, not BMS-measured health.")
    figures = build_performance_figures(result)
    left, right = st.columns(2)
    for column, key in ((left, "lifecycle"), (right, "capacity")):
        with column:
            figure = figures[key]
            if figure.data:
                st.plotly_chart(figure, width="stretch", theme="streamlit", key=f"bess_dashboard_{key}")
            else:
                st.info("Lifecycle inputs are not available in the calculation result.")
    events = result.inputs.aug_events if result.inputs.aug_enabled else []
    if events:
        st.markdown("**Configured augmentation timeline**")
        st.dataframe(
            [
                {
                    "Project year": event.year,
                    "Containers": event.containers,
                    "Action": event.kind,
                }
                for event in events
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No augmentation events are configured for this case.")


def _render_verification(st: Any, result: CaseResult) -> None:
    st.subheader("Requirement verification")
    summary = result.summary
    verify = result.verify

    def overall_status(ok_column: str, calculated_column: str, required_column: str):
        if not {ok_column, calculated_column, required_column}.issubset(verify.columns):
            return None
        values = verify[[calculated_column, required_column]].to_numpy().ravel()
        flags = verify[ok_column]
        if (
            not all(_finite(value) is not None for value in values)
            or flags.isna().any()
            or not flags.map(lambda value: isinstance(value, (bool, np.bool_))).all()
        ):
            return None
        return bool(flags.all())

    bol = _finite_status(
        summary.get("bol_ok"),
        summary.get("bol_energy"),
        result.inputs.bol_energy_mwh,
    )
    eol = _finite_status(
        summary.get("eol_ok"),
        summary.get("eol_energy"),
        result.inputs.eol_energy_mwh,
    )
    checks = [
        ("BOL energy", bol),
        ("EOL energy", eol),
        (
            "Energy over project years",
            overall_status("Energy OK", "Calculated energy (MWh)", "Required energy (MWh)"),
        ),
        (
            "Power over project years",
            overall_status("Power OK", "Calculated power (MW)", "Required power (MW)"),
        ),
        (
            "RTE over project years",
            overall_status("RTE OK", "Calculated RTE incl. aux (%)", "Required RTE (%)"),
        ),
        (
            "Cycle capability",
            _finite_status(
                summary.get("cycles_ok"),
                summary.get("cycles_capacity"),
                result.inputs.cycles_required,
            ) if "cycles_capacity" in summary else None,
        ),
    ]
    columns = st.columns(3)
    for index, (label, passed) in enumerate(checks):
        with columns[index % len(columns)]:
            _verification_status(st, label, passed)
    st.dataframe(_safe_frame(result.verify.reset_index()), hide_index=True, width="stretch")
    accepted = _finite_status(
        summary.get("accepted"),
        summary.get("bol_energy"),
        summary.get("eol_energy"),
        summary.get("rte_eol_incl"),
        summary.get("chain_eff_pct"),
    )
    if accepted is not None:
        yearly_checks = [
            overall_status("Energy OK", "Calculated energy (MWh)", "Required energy (MWh)"),
            overall_status("RTE OK", "Calculated RTE incl. aux (%)", "Required RTE (%)"),
            overall_status("Power OK", "Calculated power (MW)", "Required power (MW)"),
        ]
        accepted = (
            bool(accepted and all(yearly_checks))
            if all(check is not None for check in yearly_checks)
            else None
        )
    _verification_status(st, "Overall project sizing", accepted)
    if accepted is None:
        st.caption("Overall acceptance is unavailable in this result.")


def _render_advanced(st: Any, result: CaseResult, comparisons: Iterable[CaseResult]) -> None:
    with st.expander("Additional engineering views"):
        energy_frame = pd.DataFrame(
            {
                "Case": ["BOL", "EOL"],
                "Required POI energy (MWh)": [
                    result.inputs.bol_energy_mwh,
                    result.inputs.eol_energy_mwh,
                ],
                "Calculated net POI energy (MWh)": [
                    result.summary.get("bol_energy"),
                    result.summary.get("eol_energy"),
                ],
            }
        )
        figure = go.Figure()
        for index, column in enumerate(energy_frame.columns[1:]):
            figure.add_bar(
                x=energy_frame["Case"],
                y=[_finite(value) for value in energy_frame[column]],
                name=column,
                marker_color=(_COLORS["blue"], _COLORS["teal"])[index],
            )
        figure.update_layout(
            title="BOL versus EOL energy",
            yaxis_title="Energy (MWh)",
            barmode="group",
            margin={"l": 20, "r": 20, "t": 55, "b": 20},
        )
        st.plotly_chart(figure, width="stretch", theme="streamlit", key="bess_dashboard_bol_eol")

        comparison_cases = list(comparisons)
        if len(comparison_cases) > 1:
            comparison = pd.DataFrame(
                [
                    {
                        "Container": case.summary.get("container", case.container.label),
                        "Initial containers": case.summary.get("n_init"),
                        "Installed DC (MWh)": case.summary.get("installed_mwh"),
                        "BOL POI energy (MWh)": case.summary.get("bol_energy"),
                        "EOL POI energy (MWh)": case.summary.get("eol_energy"),
                        "EOL RTE (%)": case.summary.get("rte_eol_incl"),
                    }
                    for case in comparison_cases
                ]
            )
            comparison_figure = go.Figure()
            comparison_figure.add_bar(
                x=comparison["Container"],
                y=[_finite(value) for value in comparison["BOL POI energy (MWh)"]],
                name="BOL POI energy",
                marker_color=_COLORS["blue"],
            )
            comparison_figure.add_bar(
                x=comparison["Container"],
                y=[_finite(value) for value in comparison["EOL POI energy (MWh)"]],
                name="EOL POI energy",
                marker_color=_COLORS["teal"],
            )
            comparison_figure.update_layout(
                title="Container option comparison",
                xaxis_title="Selected container",
                yaxis_title="Net POI energy (MWh)",
                barmode="group",
                margin={"l": 20, "r": 20, "t": 55, "b": 70},
            )
            st.plotly_chart(
                comparison_figure,
                width="stretch",
                theme="streamlit",
                key="bess_dashboard_container_comparison",
            )
            st.dataframe(_safe_frame(comparison), hide_index=True, width="stretch")

        for label, frame, field in (
            ("Discharge auxiliary energy", result.dis, "Aux energy (MWh)"),
            ("Charge auxiliary energy", result.chg, "Aux energy (MWh)"),
        ):
            if field in frame.columns:
                aux_figure = _line_chart(
                    frame,
                    [(label, field)],
                    f"{label} by project year",
                    "Energy (MWh)",
                )
                if aux_figure.data:
                    st.plotly_chart(
                        aux_figure,
                        width="stretch",
                        theme="streamlit",
                        key=f"bess_dashboard_{field}_{label}",
                    )


def render_engineering_dashboard(
    st: Any,
    result: CaseResult | None,
    *,
    error: str | None = None,
    comparison_cases: Iterable[CaseResult] = (),
) -> None:
    """Render a dashboard for one calculated case, or an honest empty/error state."""
    st.subheader("Engineering dashboard")
    if error:
        st.error(error)
        return
    if result is None:
        st.info("No successful calculation is available. Configure the calculator inputs and run sizing first.")
        return

    summary = result.summary if isinstance(result.summary, dict) else {}
    with st.container(border=True):
        status = summary.get("accepted")
        if status is True:
            st.success("Sizing status: ACCEPTED")
        elif status is False:
            st.error("Sizing status: NOT ACCEPTED")
        else:
            st.warning("Sizing status: NOT AVAILABLE")
        _render_kpis(st, result)

    left, right = st.columns(2)
    with left:
        _render_configuration(st, result)
    with right:
        _render_power_flow(st, result)
    _render_charts(st, result)
    _render_lifecycle(st, result)
    _render_verification(st, result)
    _render_advanced(st, result, comparison_cases)
