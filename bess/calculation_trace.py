from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CalculationTrace:
    calculation_id: str
    title: str
    description: str
    stage: str
    input_names: list[str]
    output_name: str
    formula: str
    substitution: str | None = None
    steps: list[str] = field(default_factory=list)
    result: str | None = None
    assumptions: list[str] = field(default_factory=list)
    source: str = "bess.engine"

    def as_dict(self) -> dict[str, Any]:
        return {
            "calculation_id": self.calculation_id,
            "title": self.title,
            "description": self.description,
            "stage": self.stage,
            "input_names": self.input_names,
            "output_name": self.output_name,
            "formula": self.formula,
            "substitution": self.substitution,
            "steps": self.steps,
            "result": self.result,
            "assumptions": self.assumptions,
            "source": self.source,
        }


def _fmt(value: Any) -> str:
    if value is None:
        return "Not available"
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.2f}"
        return f"{value:.3f}"
    return str(value)


def _safe_mapping(inputs: Mapping[str, Any] | None) -> dict[str, Any]:
    if inputs is None:
        return {}
    return dict(inputs)


def _trace_required_energy(inputs: Mapping[str, Any]) -> CalculationTrace:
    value = inputs.get("required_energy_mwh", 100.0)
    return CalculationTrace(
        calculation_id="required_energy",
        title="Required energy",
        description="Project energy requirement used to size the BESS and compare annual performance against the design case.",
        stage="Sizing input",
        input_names=["required_energy_mwh"],
        output_name="Required energy (MWh)",
        formula="Required energy = user-entered project energy target (MWh)",
        substitution=f"{_fmt(value)} MWh",
        steps=[
            "The project energy target is entered in the calculator inputs.",
            "This value is used as the comparison basis for the BOL/EOL energy checks and the yearly verification table.",
        ],
        result=f"Design target: {_fmt(value)} MWh",
        assumptions=[
            "This is an input requirement, not a calculated value.",
            "The engine compares the guaranteed energy at each year against this requirement.",
        ],
        source="bess.engine.Inputs.required_energy_mwh",
    )


def _trace_required_power(inputs: Mapping[str, Any]) -> CalculationTrace:
    value = inputs.get("required_power_mw", 25.0)
    return CalculationTrace(
        calculation_id="required_power",
        title="Required power",
        description="Minimum power requirement at the POI used for power verification and container sizing.",
        stage="Sizing input",
        input_names=["required_power_mw"],
        output_name="Required power (MW)",
        formula="Required power = user-entered project power target (MW)",
        substitution=f"{_fmt(value)} MW",
        steps=[
            "The required power target is entered in the calculator inputs.",
            "The yearly verification table checks whether the calculated net power at POI exceeds this target.",
        ],
        result=f"Design target: {_fmt(value)} MW",
        assumptions=[
            "This is a user requirement and is not transformed by the sizing algorithm.",
        ],
        source="bess.engine.Inputs.required_power_mw",
    )


def _trace_c_rate(inputs: Mapping[str, Any]) -> CalculationTrace:
    c_rate = inputs.get("c_rate", 0.25)
    discharge_time = inputs.get("discharge_time_h")
    charge_time = inputs.get("charge_time_h")
    return CalculationTrace(
        calculation_id="c_rate",
        title="C-rate and duration",
        description="The charge/discharge time is derived from the selected C-rate unless the user chooses manual durations.",
        stage="Sizing input",
        input_names=["c_rate", "time_mode", "discharge_time_h", "charge_time_h"],
        output_name="Charge / discharge duration",
        formula="Time = 1 / C-rate when using the automatic duration mode",
        substitution=(
            f"C-rate = {_fmt(c_rate)} 1/h; "
            f"discharge = {_fmt(discharge_time)} h; charge = {_fmt(charge_time)} h"
        ),
        steps=[
            "The engine calls bess.engine.times(inp).",
            "If time_mode == 'c_rate', the duration is set to 1 / c_rate for both charge and discharge.",
            "Otherwise the user enters charge and discharge durations explicitly.",
        ],
        result=f"Calculated duration: {_fmt(discharge_time)} h / {_fmt(charge_time)} h",
        assumptions=[
            "This is the duration used to convert power to energy and the energy margin calculation.",
        ],
        source="bess.engine.times",
    )


def _trace_availability(inputs: Mapping[str, Any]) -> CalculationTrace:
    value = inputs.get("availability_pct", 99.0)
    return CalculationTrace(
        calculation_id="availability",
        title="Availability",
        description="A loss factor applied to the installed DC energy before the AC chain and storage degradation effects are considered.",
        stage="Yearly performance",
        input_names=["availability_pct"],
        output_name="Availability (%)",
        formula="Guaranteed DC energy = Installed DC energy × availability × sqrt(DC RTE) × SOH × calendar factor × mismatch × DOD",
        substitution=f"Availability = {_fmt(value)} % => {_fmt(value / 100.0)}",
        steps=[
            "The availability percentage is converted to a decimal fraction.",
            "This factor is applied to the installed DC energy in compute_case().",
        ],
        result=f"Availability factor: {_fmt(value / 100.0)}",
        assumptions=[
            "Availability is a design-based reduction factor, not a live operational availability measurement.",
        ],
        source="bess.engine.compute_case",
    )


def _trace_container_selection(inputs: Mapping[str, Any], container: Any | None = None) -> CalculationTrace:
    required_energy = inputs.get("required_energy_mwh", 100.0)
    margin_pct = inputs.get("margin_min_pct", 14.0)
    energy_per_container = getattr(container, "energy_mwh", None)
    if energy_per_container is None:
        energy_per_container = inputs.get("container_energy_mwh", 10.0)
    formula = "n = ceil(required_energy × (1 + margin_pct/100) / container.energy_mwh)"
    selected_count = max(1, int(__import__("math").ceil(required_energy * (1 + margin_pct / 100.0) / energy_per_container))) if energy_per_container else 1
    return CalculationTrace(
        calculation_id="container_selection",
        title="Container selection",
        description="The engine chooses the initial container count by applying the configured margin to the required energy and rounding upward to the next whole container.",
        stage="Container selection",
        input_names=["required_energy_mwh", "margin_min_pct", "margin_max_pct", "container.energy_mwh"],
        output_name="Initial containers",
        formula=formula,
        substitution=(
            f"{_fmt(required_energy)} MWh × (1 + {_fmt(margin_pct)} / 100) / {_fmt(energy_per_container)} MWh / container = "
            f"{selected_count} containers"
        ),
        steps=[
            "The sizing scan starts in bess.engine.size_container().",
            "For each candidate margin, the engine computes the required number of containers using the margin formula above.",
            "The smallest valid candidate satisfying energy, power and EOL RTE checks is accepted.",
        ],
        result=f"Selected container count: {selected_count}",
        assumptions=[
            "The sizing algorithm applies the configured energy margin, not a fixed over-dimensioning rule imported from elsewhere.",
            "This is a design-count estimate used to evaluate the whole-project requirements.",
        ],
        source="bess.engine.size_container / bess.engine.containers_for_margin",
    )


def _trace_guaranteed_energy(result: Any | None = None) -> CalculationTrace:
    summary = getattr(result, "summary", {}) if result is not None else {}
    installed = summary.get("installed_mwh", 0.0)
    bol = summary.get("bol_energy", 0.0)
    eol = summary.get("eol_energy", 0.0)
    return CalculationTrace(
        calculation_id="guaranteed_energy",
        title="Guaranteed DC and POI energy",
        description="The guaranteed energy is the installed DC energy adjusted by the operating model factors, calendar degradation, mismatch, DOD and auxiliary consumption.",
        stage="Yearly performance",
        input_names=["installed_mwh", "availability_pct", "soh", "calendar_factor_pct", "mismatch_factor_pct", "dod_pct"],
        output_name="Guaranteed energy",
        formula="g_dc = installed_dc × availability × sqrt(DC RTE) × SOH × calendar factor × mismatch × DOD",
        substitution=(
            f"Installed DC = {_fmt(installed)} MWh; BOL = {_fmt(bol)} MWh; EOL = {_fmt(eol)} MWh"
        ),
        steps=[
            "The engine calculates the guaranteed DC discharge energy in compute_case().",
            "The result is then adjusted for the AC chain and auxiliary consumption before being compared to the yearly project requirement.",
        ],
        result=f"BOL = {_fmt(bol)} MWh; EOL = {_fmt(eol)} MWh",
        assumptions=[
            "This is based on the actual engine dataset and degradation factors from the selected container.",
            "The final reported POI value may differ from the raw DC value because auxiliary loads and plant infrastructure are included.",
        ],
        source="bess.engine.compute_case",
    )


def _trace_rte(result: Any | None = None) -> CalculationTrace:
    summary = getattr(result, "summary", {}) if result is not None else {}
    rte_excl = summary.get("rte_eol_excl", 0.0)
    rte_incl = summary.get("rte_eol_incl", 0.0)
    chain = summary.get("chain_eff_pct", 0.0)
    return CalculationTrace(
        calculation_id="rte",
        title="Round-trip efficiency",
        description="The AC chain efficiency and auxiliary loads reduce the round-trip efficiency seen at the POI.",
        stage="Verification",
        input_names=["dc_rte", "chain_eff_pct", "aux_load_*"],
        output_name="EOL RTE (%)",
        formula="RTE incl. aux = (discharge energy incl. aux) / (charge energy incl. aux) ; AC chain = product(ac efficiencies)",
        substitution=(
            f"AC chain efficiency = {_fmt(chain)}%; RTE excl. aux = {_fmt(rte_excl)}%; "
            f"RTE incl. aux = {_fmt(rte_incl)}%"
        ),
        steps=[
            "The engine calculates the AC efficiency chain from each AC component in effective_params().",
            "The yearly discharge and charge performance is then combined with the auxiliary loads, and the final value is reported in perf and verify.",
        ],
        result=f"EOL RTE incl. aux = {_fmt(rte_incl)}%",
        assumptions=[
            "RTE values follow the engine's actual model, not a generic BMS estimate.",
            "Auxiliary consumption is explicitly included when the user enables it.",
        ],
        source="bess.engine.effective_params / bess.engine.compute_case",
    )


def _trace_verification(result: Any | None = None) -> CalculationTrace:
    summary = getattr(result, "summary", {}) if result is not None else {}
    accepted = summary.get("accepted", False)
    years_ok = summary.get("years_ok", False)
    bol_ok = summary.get("bol_ok", False)
    eol_ok = summary.get("eol_ok", False)
    rte_ok = summary.get("rte_eol_ok", False)
    cycles_ok = summary.get("cycles_ok", False)
    return CalculationTrace(
        calculation_id="verification",
        title="Requirement verification",
        description="The sizing result is accepted only when the yearly energy, power and RTE checks pass, and the cycle capability meets the design requirement.",
        stage="Verification",
        input_names=["required_energy_mwh", "required_power_mw", "min_rte_eol_pct", "cycles_required"],
        output_name="Project acceptance",
        formula="Accepted = BOL OK AND EOL OK AND RTE EOL OK AND year-by-year checks OK AND cycle capability OK",
        substitution=(
            f"BOL = {str(bool(bol_ok))}; EOL = {str(bool(eol_ok))}; RTE = {str(bool(rte_ok))}; "
            f"Yearly = {str(bool(years_ok))}; Cycles = {str(bool(cycles_ok))}; Accepted = {str(bool(accepted))}"
        ),
        steps=[
            "The verify dataframe is built in compute_case().",
            "Each year must satisfy the required energy, RTE and power thresholds.",
            "The final accepted flag is only true when all checks pass and the battery cycle capability meets the requirement.",
        ],
        result=f"Accepted = {bool(accepted)}",
        assumptions=[
            "This is a design verification step, not a live system health status.",
            "The engine does not infer additional acceptance criteria beyond the implemented checks.",
        ],
        source="bess.engine.compute_case",
    )


def build_trace_map(
    inputs: Mapping[str, Any] | None = None,
    result: Any | None = None,
    *,
    container: Any | None = None,
) -> dict[str, CalculationTrace]:
    input_map = _safe_mapping(inputs)
    if result is not None and getattr(result, "inputs", None) is not None:
        result_inputs = getattr(result, "inputs")
        input_map.update(
            {
                "required_energy_mwh": getattr(result_inputs, "required_energy_mwh", input_map.get("required_energy_mwh", 100.0)),
                "required_power_mw": getattr(result_inputs, "required_power_mw", input_map.get("required_power_mw", 25.0)),
                "c_rate": getattr(result_inputs, "c_rate", input_map.get("c_rate", 0.25)),
                "availability_pct": getattr(result_inputs, "availability_pct", input_map.get("availability_pct", 99.0)),
                "margin_min_pct": getattr(result_inputs, "margin_min_pct", input_map.get("margin_min_pct", 14.0)),
                "discharge_time_h": getattr(result_inputs, "discharge_time_h", input_map.get("discharge_time_h", 4.0)),
                "charge_time_h": getattr(result_inputs, "charge_time_h", input_map.get("charge_time_h", 4.0)),
            }
        )
    container_obj = container or getattr(result, "container", None)
    traces = {
        "required_energy": _trace_required_energy(input_map),
        "required_power": _trace_required_power(input_map),
        "c_rate": _trace_c_rate(input_map),
        "availability": _trace_availability(input_map),
        "container_selection": _trace_container_selection(input_map, container_obj),
        "guaranteed_energy": _trace_guaranteed_energy(result),
        "rte": _trace_rte(result),
        "verification": _trace_verification(result),
    }
    return traces


def render_calculation_info(st: Any, calculation_id: str, trace: CalculationTrace | Mapping[str, Any] | None = None, title: str | None = None) -> None:
    if trace is None:
        trace = build_trace_map().get(calculation_id)
    if not trace:
        return
    if isinstance(trace, Mapping):
        trace = CalculationTrace(
            calculation_id=str(trace.get("calculation_id", calculation_id)),
            title=str(trace.get("title", title or calculation_id)),
            description=str(trace.get("description", "")),
            stage=str(trace.get("stage", "Calculation")),
            input_names=list(trace.get("input_names", [])),
            output_name=str(trace.get("output_name", "Result")),
            formula=str(trace.get("formula", "")),
            substitution=trace.get("substitution"),
            steps=list(trace.get("steps", [])),
            result=trace.get("result"),
            assumptions=list(trace.get("assumptions", [])),
            source=str(trace.get("source", "bess.engine")),
        )

    button_label = "🧮" if title is None else f"🧮 {title}"
    popover_title = title or trace.title
    try:
        with st.popover(button_label, help="View calculation details"):
            _render_trace(st, trace, popover_title)
    except Exception:
        if st.button(button_label, key=f"calc_trace_{calculation_id}", help="View calculation details"):
            _render_trace(st, trace, popover_title)


def _render_trace(st: Any, trace: CalculationTrace, title: str | None = None) -> None:
    if title:
        st.caption(f"**{title}**")
    if trace.description:
        st.write(trace.description)
    if trace.stage:
        st.caption(f"Stage: {trace.stage}")
    if trace.formula:
        st.markdown("**Formula used**")
        st.code(trace.formula)
    if trace.substitution:
        st.markdown("**Actual value substitution**")
        st.code(trace.substitution)
    if trace.steps:
        st.markdown("**Step-by-step calculation**")
        for index, step in enumerate(trace.steps, start=1):
            st.write(f"{index}. {step}")
    if trace.result:
        st.markdown("**Final result**")
        st.write(trace.result)
    if trace.assumptions:
        st.markdown("**Engineering assumptions**")
        for assumption in trace.assumptions:
            st.write(f"- {assumption}")
    if trace.source:
        st.caption(f"Source: {trace.source}")
