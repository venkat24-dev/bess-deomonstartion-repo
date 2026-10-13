"""BESS sizing engine.

Implements the calculation chain of BESS_SIZING_2_0:

  DC side   : Installed x Availability x sqrt(DC RTE) x SOH x CalendarDeg x (1-Mismatch) x DOD
  AC side   : x LV cable x PCS x PCS-IDT cable x IDT x DC DB x MV cable/switchgear
              x Main transformer x 11 kV cable x Measurement accuracy
  Charging  : DC-side value / (AC chain x DC RTE)
  Aux       : Aux load x time (+ additional aux + standby energy)
  Augmented : same chain applied to the augmented containers.

All percentages are entered as percent in ``Inputs`` and converted internally.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from .database import AC_KEYS, AC_LABELS, Container


# ----------------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------------
@dataclass
class AugEvent:
    year: int  # augmentation happens at the START of this project year (1..N)
    containers: int
    kind: str = "Add"  # "Add" new containers, or "Replace" initial containers with new ones


@dataclass
class Inputs:
    # section 1
    required_energy_mwh: float = 100.0
    required_power_mw: float = 25.0
    c_rate: float = 0.25
    time_mode: str = "c_rate"  # "c_rate" -> 1/C, "manual" -> user times
    discharge_time_h: float = 4.0
    charge_time_h: float = 4.0
    mismatch_mode: str = "constant"  # "constant" | "yearly"
    mismatch_constant_pct: float = 1.0
    mismatch_yearly_pct: list[float] | None = None  # years 0..N
    operating_temp_c: float = 25.0
    availability_pct: float = 99.0
    bol_energy_mwh: float = 100.0
    eol_energy_mwh: float = 100.0
    min_rte_eol_pct: float = 80.0
    aux_required: bool = True
    project_years: int = 10
    cycles_required: int = 3650
    dod_pct: float = 100.0
    # section 14
    extra_aux_applicable: bool = False
    extra_aux_mwh: float = 0.0  # MWh per cycle
    standby_applicable: bool = False
    standby_mode: str = "manual"  # "manual" | "database"
    standby_mwh: float = 0.0  # MWh per cycle (manual)
    extras_side: str = "discharge"  # which side the extra/standby energy is booked on
    # section 16
    fatsat_applicable: bool = True
    fatsat_months: int = 3
    caldeg_override_pct: list[float] | None = None  # per-month degradation (%), optional
    # section 17+
    aug_enabled: bool = False
    aug_events: list[AugEvent] = field(default_factory=list)
    aug_age_basis: str = "own"  # "own": SOH by age since augmentation | "year": SOH by project year
    plant_infra_power_mw: float = 0.0  # AC MV power to plant infrastructure
    # section 15 (required values by year, index = year 0..N)
    req_energy_by_year: list[float] | None = None
    req_rte_by_year_pct: list[float] | None = None
    req_power_by_year: list[float] | None = None
    # container-selection / sizing control
    margin_min_pct: float = 14.0
    margin_max_pct: float = 20.0
    margin_step_pct: float = 0.1
    allow_extended_margin: bool = True
    manual_containers: int = 0  # 0 = automatic
    mpt_source: str = "spec"  # "spec": transformer efficiency (sec. 4) | "hv": HV transformer row
    # database overrides
    ac_overrides: dict[str, float] = field(default_factory=dict)
    aux_overrides: dict[str, float] = field(default_factory=dict)  # keys: discharge, charge, standby (MW)
    # what-if adjustments used by the analyser (neutral by default)
    soh_delta_pp: float = 0.0
    rte_delta_pp: float = 0.0
    aux_scale: float = 1.0
    aux_kw_fix: bool = False  # treat suspicious database aux values (see Container.aux_suspect) as kW


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def times(inp: Inputs) -> tuple[float, float]:
    if inp.time_mode == "c_rate":
        if inp.c_rate <= 0:
            raise ValueError("C-rate must be > 0")
        t = 1.0 / inp.c_rate
        return t, t
    return inp.discharge_time_h, inp.charge_time_h


def effective_params(container: Container, inp: Inputs) -> dict:
    ac = dict(container.ac)
    if inp.mpt_source == "hv" and container.ac_alt.get("hv_transformer") is not None:
        ac["main_transformer"] = container.ac_alt["hv_transformer"]
    ac.update(inp.ac_overrides)
    chain = float(np.prod([ac[k] for k in AC_KEYS]))
    ov = inp.aux_overrides
    warnings = []

    def aux(key, db_val, label):
        if key in ov:
            return float(ov[key])
        if db_val is None:
            needed = inp.aux_required and (key != "standby" or (inp.standby_applicable and inp.standby_mode == "database"))
            if needed:
                warnings.append(f"{container.label}: {label} auxiliary load not in database - 0 MW used.")
            return 0.0
        if inp.aux_kw_fix and container.aux_suspect:
            return float(db_val) / 1000.0
        return float(db_val)

    return {
        "ac": ac,
        "chain": chain,
        "aux_dis": aux("discharge", container.aux_discharge_mw, "discharge"),
        "aux_ch": aux("charge", container.aux_charge_mw, "charge"),
        "aux_sb": aux("standby", container.aux_standby_mw, "standby"),
        "warnings": warnings,
    }


def band_index(container: Container, temp_c: float) -> int:
    for i, up in enumerate(container.temp_upper):
        if temp_c <= up:
            return i
    raise ValueError(
        f"{container.label}: operating temperature {temp_c:g} degC is above the database range "
        f"({container.temp_labels[-1]})."
    )


def calendar_factor(container: Container, inp: Inputs) -> tuple[float, list[float], str]:
    """Cumulative calendar-degradation factor for the FAT-SAT storage period.

    Monthly degradation (database, selected temperature band) is applied month by
    month: factor = prod(1 - d_m) for m = 1..months.  The factor is a one-off loss
    that stays for the whole project because the database SOH curve explicitly
    excludes calendar degradation.
    """
    months = int(inp.fatsat_months) if inp.fatsat_applicable else 0
    if months <= 0:
        return 1.0, [], "FAT/SAT not applicable - calendar degradation factor = 100 %"
    if months > 6:
        raise ValueError("FAT/SAT duration cannot exceed 6 months")
    if inp.caldeg_override_pct:
        d = [x / 100.0 for x in inp.caldeg_override_pct[:months]]
        src = "user-entered monthly values"
    else:
        idx = band_index(container, inp.operating_temp_c)
        if months > len(container.cal_rows):
            raise ValueError(f"{container.label}: database has only {len(container.cal_rows)} months of calendar degradation")
        d = [row[idx] for row in container.cal_rows[:months]]
        src = f"database band {container.temp_labels[idx]}"
    f = float(np.prod([1 - x for x in d]))
    return f, d, src


def mismatch_series(inp: Inputs) -> list[float]:
    n = inp.project_years + 1
    if inp.mismatch_mode == "yearly" and inp.mismatch_yearly_pct:
        vals = list(inp.mismatch_yearly_pct)
        vals = (vals + [vals[-1]] * n)[:n]
        return [v / 100.0 for v in vals]
    return [inp.mismatch_constant_pct / 100.0] * n


def default_requirements(inp: Inputs) -> pd.DataFrame:
    """Required values at the end of each year (section 15), pre-filled."""
    n = inp.project_years
    years = list(range(n + 1))
    energy = np.linspace(inp.bol_energy_mwh, inp.eol_energy_mwh, n + 1)
    return pd.DataFrame(
        {
            "Year": years,
            "Required energy (MWh)": np.round(energy, 4),
            "Required RTE (%)": [inp.min_rte_eol_pct] * (n + 1),
            "Required power (MW)": [inp.required_power_mw] * (n + 1),
        }
    )


def _req_arrays(inp: Inputs):
    n = inp.project_years + 1
    d = default_requirements(inp)
    e = inp.req_energy_by_year if inp.req_energy_by_year and len(inp.req_energy_by_year) == n else list(d.iloc[:, 1])
    r = inp.req_rte_by_year_pct if inp.req_rte_by_year_pct and len(inp.req_rte_by_year_pct) == n else list(d.iloc[:, 2])
    p = inp.req_power_by_year if inp.req_power_by_year and len(inp.req_power_by_year) == n else list(d.iloc[:, 3])
    return np.array(e, float), np.array(r, float) / 100.0, np.array(p, float)


def validate_events(inp: Inputs, n_init: int) -> list[AugEvent]:
    if not inp.aug_enabled:
        return []
    out = []
    replaced = 0
    for e in sorted(inp.aug_events, key=lambda x: x.year):
        if e.containers <= 0:
            continue
        if not 1 <= e.year <= inp.project_years:
            raise ValueError(f"Augmentation year {e.year} is outside 1..{inp.project_years}")
        if e.kind == "Replace":
            replaced += e.containers
            if replaced > n_init:
                raise ValueError("Replaced containers exceed the initial number of containers")
        out.append(e)
    return out


# ----------------------------------------------------------------------------
# core calculation
# ----------------------------------------------------------------------------
@dataclass
class CaseResult:
    container: Container
    n_init: int
    inputs: Inputs
    dis: pd.DataFrame
    chg: pd.DataFrame
    perf: pd.DataFrame
    verify: pd.DataFrame
    summary: dict
    warnings: list[str]


def compute_case(container: Container, n_init: int, inp: Inputs) -> CaseResult:
    N = inp.project_years
    if N > container.max_years:
        raise ValueError(
            f"{container.label}: database has SOH/RTE data up to year {container.max_years}, "
            f"project duration is {N} years."
        )
    if n_init < 1:
        raise ValueError("Number of containers must be >= 1")

    ep = effective_params(container, inp)
    warnings = list(ep["warnings"])
    chain = ep["chain"]
    A = inp.availability_pct / 100.0
    dod = inp.dod_pct / 100.0
    td, tc = times(inp)
    cd, cd_list, cd_src = calendar_factor(container, inp)
    mm = mismatch_series(inp)
    Ec = container.energy_mwh
    events = validate_events(inp, n_init)

    aux_on = inp.aux_required
    aux_dis = ep["aux_dis"] * inp.aux_scale if aux_on else 0.0
    aux_ch = ep["aux_ch"] * inp.aux_scale if aux_on else 0.0
    aux_sb = ep["aux_sb"] * inp.aux_scale if aux_on else 0.0

    def soh_at(j: int) -> float:
        v = container.soh[min(j, len(container.soh) - 1)]
        if j >= 1:
            v += inp.soh_delta_pp / 100.0
        return min(1.0, max(0.01, v))

    def rte_at(j: int) -> float:
        v = container.dc_rte[min(j, len(container.dc_rte) - 1)] + inp.rte_delta_pp / 100.0
        return min(0.999, max(0.5, v))

    def extras(n_base: int) -> tuple[float, float]:
        if not aux_on:
            return 0.0, 0.0
        e = inp.extra_aux_mwh if inp.extra_aux_applicable else 0.0
        if inp.standby_applicable:
            if inp.standby_mode == "database":
                e += n_base * aux_sb * max(0.0, 24.0 - td - tc)
            else:
                e += inp.standby_mwh
        return (e, 0.0) if inp.extras_side == "discharge" else (0.0, e)

    dis_rows, chg_rows, perf_rows = [], [], []
    infra_dis, infra_ch = inp.plant_infra_power_mw * td, inp.plant_infra_power_mw * tc
    e_req, rte_req, p_req = _req_arrays(inp)

    for y in range(N + 1):
        replaced = sum(e.containers for e in events if e.kind == "Replace" and e.year <= y)
        n_base = n_init - replaced
        inst = Ec * n_base
        eta = math.sqrt(rte_at(y))
        soh = soh_at(y)
        mmf = 1.0 - mm[y]

        # ---- base fleet, discharging
        g_dc = inst * A * eta * soh * cd * mmf * dod
        dis_excl = g_dc * chain
        x_dis, x_ch = extras(n_base)
        aux_en_dis = n_base * aux_dis * td + x_dis
        dis_incl = dis_excl - aux_en_dis
        # ---- base fleet, charging
        g_dc_ch = inst * A * eta * soh * cd * mmf * dod
        ch_excl = g_dc_ch / (chain * rte_at(y))
        aux_en_ch = n_base * aux_ch * tc + x_ch
        ch_incl = ch_excl + aux_en_ch

        # ---- augmented batches
        a_inst = a_gdc = a_dis_excl = a_dis_incl = a_ch_excl = a_ch_incl = 0.0
        a_cont = 0
        for e in events:
            if y < e.year:
                continue
            jk = max(1, (y - e.year + 1) if inp.aug_age_basis == "own" else y)
            ek = math.sqrt(rte_at(jk))
            inst_k = Ec * e.containers
            gk = inst_k * A * ek * soh_at(jk) * cd * mmf * dod
            a_inst += inst_k
            a_cont += e.containers
            a_gdc += gk
            a_dis_excl += gk * chain
            a_dis_incl += gk * chain - e.containers * aux_dis * td
            ch_k = gk / (chain * rte_at(jk))
            a_ch_excl += ch_k
            a_ch_incl += ch_k + e.containers * aux_ch * tc

        e_dis_tot = dis_incl + a_dis_incl - infra_dis
        e_ch_tot = ch_incl + a_ch_incl + infra_ch
        rte_excl = (dis_excl + a_dis_excl) / (ch_excl + a_ch_excl)
        rte_incl = (dis_incl + a_dis_incl) / (ch_incl + a_ch_incl)
        power = e_dis_tot / td if td > 0 else float("nan")

        dis_rows.append(
            {
                "Year": y,
                "Initial containers": n_base,
                "Installed DC capacity (MWh)": inst,
                "Availability (%)": A * 100,
                "DC discharge efficiency (%)": eta * 100,
                "SOH (%)": soh * 100,
                "Calendar degradation factor (%)": cd * 100,
                "Mismatch factor (%)": mmf * 100,
                "DOD (%)": dod * 100,
                "Guaranteed DC discharge capacity (MWh)": g_dc,
                "AC chain efficiency (%)": chain * 100,
                "Guaranteed capacity at POI excl. aux (MWh)": dis_excl,
                "Aux load - discharge (MW)": n_base * aux_dis,
                "Discharge time (h)": td,
                "Aux energy (MWh)": aux_en_dis,
                "Guaranteed capacity at POI incl. aux (MWh)": dis_incl,
                "Augmented containers": a_cont,
                "Augmented capacity (MWh)": a_inst,
                "Augmented DC capacity (MWh)": a_gdc,
                "Augmented energy incl. aux (MWh)": a_dis_incl,
                "Plant infrastructure energy (MWh)": infra_dis,
                "AC MV energy incl. augmentation & aux at POI (MWh)": e_dis_tot,
            }
        )
        chg_rows.append(
            {
                "Year": y,
                "Initial containers": n_base,
                "Installed DC capacity (MWh)": inst,
                "Availability (%)": A * 100,
                "DC charge efficiency (%)": eta * 100,
                "SOH (%)": soh * 100,
                "Calendar degradation factor (%)": cd * 100,
                "Mismatch factor (%)": mmf * 100,
                "DOD (%)": dod * 100,
                "DC-DC RTE (%)": rte_at(y) * 100,
                "Guaranteed DC charge capacity (MWh)": g_dc_ch,
                "AC chain efficiency (%)": chain * 100,
                "Capacity at POI excl. aux (MWh)": ch_excl,
                "Aux load - charge (MW)": n_base * aux_ch,
                "Charge time (h)": tc,
                "Aux energy (MWh)": aux_en_ch,
                "Capacity at POI incl. aux (MWh)": ch_incl,
                "Augmented containers": a_cont,
                "Augmented energy incl. aux (MWh)": a_ch_incl,
                "Plant infrastructure energy (MWh)": infra_ch,
                "AC MV energy incl. augmentation & aux at POI (MWh)": e_ch_tot,
            }
        )
        perf_rows.append(
            {
                "Year": y,
                "AC RTE excl. aux (%)": rte_excl * 100,
                "AC RTE incl. aux (%)": rte_incl * 100,
                "Net discharge energy at POI (MWh)": e_dis_tot,
                "Net power at POI (MW)": power,
                "DC usable energy (MWh)": g_dc,
            }
        )

    dis = pd.DataFrame(dis_rows).set_index("Year")
    chg = pd.DataFrame(chg_rows).set_index("Year")
    perf = pd.DataFrame(perf_rows).set_index("Year")

    # ---- verification (sections 12 and 15)
    ver = pd.DataFrame(
        {
            "Required energy (MWh)": e_req,
            "Calculated energy (MWh)": perf["Net discharge energy at POI (MWh)"].values,
            "Required RTE (%)": rte_req * 100,
            "Calculated RTE incl. aux (%)": perf["AC RTE incl. aux (%)"].values,
            "Required power (MW)": p_req,
            "Calculated power (MW)": perf["Net power at POI (MW)"].values,
        },
        index=pd.Index(range(N + 1), name="Year"),
    )
    tol = 1e-9
    ver["Energy OK"] = ver["Calculated energy (MWh)"] >= ver["Required energy (MWh)"] - tol
    ver["RTE OK"] = ver["Calculated RTE incl. aux (%)"] >= ver["Required RTE (%)"] - tol
    ver["Power OK"] = ver["Calculated power (MW)"] >= ver["Required power (MW)"] - tol
    ver["All OK"] = ver["Energy OK"] & ver["RTE OK"] & ver["Power OK"]

    e_dis_y = perf["Net discharge energy at POI (MWh)"].values
    rte_incl_y = perf["AC RTE incl. aux (%)"].values / 100.0
    bol_ok = bool(e_dis_y[0] >= inp.bol_energy_mwh - tol)
    eol_ok = bool(e_dis_y[-1] >= inp.eol_energy_mwh - tol)
    rte_eol_ok = bool(rte_incl_y[-1] * 100 >= inp.min_rte_eol_pct - tol)
    years_ok = bool(ver["All OK"].all())
    cycles_cap = container.cycles_per_day * 365.0 * N
    cycles_ok = bool(cycles_cap >= inp.cycles_required)
    sizing_ok = bol_ok and eol_ok and rte_eol_ok and years_ok
    installed0 = Ec * n_init
    summary = {
        "container": container.label,
        "n_init": n_init,
        "installed_mwh": installed0,
        "effective_margin_pct": (installed0 / inp.required_energy_mwh - 1) * 100 if inp.required_energy_mwh else float("nan"),
        "bol_energy": float(e_dis_y[0]),
        "eol_energy": float(e_dis_y[-1]),
        "rte_eol_incl": float(rte_incl_y[-1] * 100),
        "rte_eol_excl": float(perf["AC RTE excl. aux (%)"].values[-1]),
        "chain_eff_pct": chain * 100,
        "calendar_factor_pct": cd * 100,
        "calendar_source": cd_src,
        "calendar_monthly_pct": [d * 100 for d in cd_list],
        "discharge_time_h": td,
        "charge_time_h": tc,
        "bol_ok": bol_ok,
        "eol_ok": eol_ok,
        "rte_eol_ok": rte_eol_ok,
        "years_ok": years_ok,
        "cycles_ok": cycles_ok,
        "cycles_capacity": cycles_cap,
        "sizing_ok": sizing_ok,
        "accepted": sizing_ok and cycles_ok,
        "aux_load_dis_mw": n_init * aux_dis,
        "aux_load_ch_mw": n_init * aux_ch,
        "first_failing_year": (None if years_ok else int(ver.index[~ver["All OK"]][0])),
    }
    if not cycles_ok:
        warnings.append(
            f"{container.label}: supports {cycles_cap:,.0f} cycles at {container.cycles_per_day:g} cycle/day over "
            f"{N} years, below the required {inp.cycles_required:,} cycles."
        )
    return CaseResult(container, n_init, inp, dis, chg, perf, ver, summary, warnings)


# ----------------------------------------------------------------------------
# initial sizing (section 2.2)
# ----------------------------------------------------------------------------
@dataclass
class SizingOutcome:
    result: CaseResult
    margin_applied_pct: float
    in_range: bool  # True if the 14-20 % range was enough
    note: str


def containers_for_margin(inp: Inputs, container: Container, margin_pct: float) -> int:
    return max(1, math.ceil(round(inp.required_energy_mwh * (1 + margin_pct / 100.0) / container.energy_mwh, 9)))


def size_container(container: Container, inp: Inputs) -> SizingOutcome:
    """Pick the number of containers.

    Section 2.2: start with an energy margin between 14 % and 20 % and choose the
    lowest margin (hence the lowest whole number of containers) for which the
    energy, power and RTE requirements hold for the whole project.  If the range
    is not sufficient, the search is extended (when allowed) and flagged.
    """
    if inp.manual_containers and inp.manual_containers > 0:
        res = compute_case(container, int(inp.manual_containers), inp)
        return SizingOutcome(res, res.summary["effective_margin_pct"], True, "Number of containers entered manually.")

    cache: dict[int, CaseResult] = {}

    def run(n):
        if n not in cache:
            cache[n] = compute_case(container, n, inp)
        return cache[n]

    def scan(lo, hi, step):
        seen = set()
        for m in np.arange(lo, hi + 1e-9, step):
            n = containers_for_margin(inp, container, float(m))
            if n in seen:
                continue
            seen.add(n)
            res = run(n)
            if res.summary["sizing_ok"]:
                return res, float(round(m, 4))
        return None, None

    res, m = scan(inp.margin_min_pct, inp.margin_max_pct, inp.margin_step_pct)
    if res is not None:
        return SizingOutcome(res, m, True, f"Requirements met with an additional energy margin of {m:.1f} % (within {inp.margin_min_pct:g}-{inp.margin_max_pct:g} %).")
    if inp.allow_extended_margin:
        res, m = scan(inp.margin_max_pct + 0.5, 300.0, 0.5)
        if res is not None:
            return SizingOutcome(
                res, m, False,
                f"The {inp.margin_min_pct:g}-{inp.margin_max_pct:g} % margin range cannot satisfy the requirements for "
                f"{inp.project_years} years; {m:.1f} % is needed. Consider augmentation, a lower EOL energy or a shorter term.",
            )
    n = containers_for_margin(inp, container, inp.margin_max_pct)
    res = run(n)
    return SizingOutcome(
        res, inp.margin_max_pct, False,
        f"Requirements are NOT met at the maximum margin ({inp.margin_max_pct:g} %). See the verification table.",
    )


def minimum_containers(container: Container, inp: Inputs, upper: int | None = None) -> int | None:
    """Smallest number of initial containers for which all energy/power/RTE checks pass."""
    lo = max(1, int(inp.required_energy_mwh / container.energy_mwh * 0.5))
    hi = upper or int(inp.required_energy_mwh / container.energy_mwh * 6) + 5
    for n in range(lo, hi + 1):
        try:
            if compute_case(container, n, inp).summary["sizing_ok"]:
                return n
        except ValueError:
            return None
    return None


# ----------------------------------------------------------------------------
# presentation helpers
# ----------------------------------------------------------------------------
def status_labels(ok: pd.Series, style: str = "MATCHED") -> pd.Series:
    t, f = ("TRUE", "FALSE") if style == "TRUE/FALSE" else ("MATCHED", "NOT MATCHED")
    return ok.map({True: t, False: f})


def ac_chain_table(container: Container, inp: Inputs) -> pd.DataFrame:
    ep = effective_params(container, inp)
    rows = [
        {
            "Parameter": AC_LABELS[k],
            "Efficiency (%)": ep["ac"][k] * 100,
            "Source": ("override" if k in inp.ac_overrides else container.ac_source[k]),
        }
        for k in AC_KEYS
    ]
    rows.append({"Parameter": "Combined AC chain", "Efficiency (%)": ep["chain"] * 100, "Source": "product of the above"})
    return pd.DataFrame(rows)
