from pathlib import Path

from bess.calculation_trace import build_trace_map
from bess.database import load_database, select_by_c_rate
from bess.engine import Inputs, compute_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE = PROJECT_ROOT / "data" / "BESS_DATABASE.xlsx"


def test_build_trace_map_includes_core_sizing_traces():
    traces = build_trace_map({
        "required_energy_mwh": 120.0,
        "required_power_mw": 35.0,
        "c_rate": 0.25,
        "availability_pct": 99.0,
        "margin_min_pct": 14.0,
    })

    assert set(traces) >= {
        "required_energy",
        "required_power",
        "c_rate",
        "availability",
        "container_selection",
    }
    assert "Required energy" in traces["required_energy"].title
    assert "ceil" in traces["container_selection"].formula.lower()
    assert "99" in traces["availability"].substitution


def test_trace_map_uses_case_result_values_for_verification_details():
    container = select_by_c_rate(load_database(DATABASE), 0.25)[0]
    result = compute_case(
        container,
        10,
        Inputs(
            required_energy_mwh=120.0,
            required_power_mw=30.0,
            availability_pct=99.0,
            project_years=3,
            cycles_required=1000,
            aux_required=True,
        ),
    )

    traces = build_trace_map(result=result)

    assert "guaranteed_energy" in traces
    assert "verification" in traces
    assert "Accepted" in traces["verification"].result or "False" in traces["verification"].result
    assert "bess.engine.compute_case" in traces["verification"].source
