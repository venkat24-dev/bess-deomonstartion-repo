"""BESS sizing analyser / chat assistant (specification section 11).

The analyser re-runs the sizing engine with changed inputs on the *same* number
of containers, compares the result with the baseline and explains the
system-level impact.  Free-text requests such as "reduce DOD to 90% and SOH by
2%" are understood by a small rule-based parser, so the assistant works with no
API key.  If an Anthropic key is configured the explanation can additionally be
written (and follow-up questions answered) by Claude.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pandas as pd

from .engine import CaseResult, Inputs, compute_case, minimum_containers

# ---------------------------------------------------------------------------
# free-text parsing
# ---------------------------------------------------------------------------
KEYWORDS = {
    "dod": r"\bdod\b|depth of discharge",
    "soh": r"\bsoh\b|state of health",
    "availability": r"availab",
    "mismatch": r"mismatch",
    "temperature": r"\btemp",
    "rte": r"\brte\b|round[- ]?trip",
    "aux": r"\baux",
    "discharge_time": r"discharge (?:time|duration|hours)|discharge hours",
    "containers": r"container|enclosure",
}
NEG = r"decreas|reduc|drop|lower|fall|down|less|declin|degrad|worse|minus|cut|remove"
POS = r"increas|rais|improv|higher|more|up\b|add|plus|grow"

NUM = re.compile(r"([+-]?)\s*(\d+(?:\.\d+)?)\s*(%|pp|x|degc|c\b|h\b|hours?)?")

PARAM_TITLES = {
    "dod": "DOD",
    "soh": "SOH",
    "availability": "BESS availability",
    "mismatch": "Mismatch losses",
    "temperature": "Operating temperature",
    "rte": "DC-DC RTE",
    "aux": "Auxiliary load",
    "discharge_time": "Discharge time",
    "containers": "Number of containers",
}


def parse_changes(text: str) -> dict:
    """Return {param: {"mode": "abs"|"delta"|"scale", "value": float}}."""
    t = text.lower()
    hits = []
    for key, pat in KEYWORDS.items():
        for m in re.finditer(pat, t):
            hits.append((m.start(), m.end(), key))
    hits.sort()
    out: dict = {}
    for i, (s, e, key) in enumerate(hits):
        if key in out:
            continue
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(t)
        window = t[e:nxt]
        num = NUM.search(window)
        if num:
            before = t[max(0, s - 18): s] + " " + window[: num.start()]
            between = window[: num.start()]
        else:  # number written before the keyword, e.g. "add 2 containers"
            prev_end = hits[i - 1][1] if i else 0
            back = t[prev_end:s]
            found = list(NUM.finditer(back))
            if not found:
                continue
            num = found[-1]
            before = back[max(0, num.start() - 18):]
            between = ""
        sign, val, unit = num.group(1), float(num.group(2)), num.group(3) or ""
        direction = -1 if re.search(NEG, before) else (1 if re.search(POS, before) else 0)
        if sign == "-":
            direction = -1
        elif sign == "+":
            direction = 1
        has_by = bool(re.search(r"\bby\b", before))
        has_to = bool(re.search(r"\bto\b|=|:|\bat\b|\bis\b", between))
        if key in ("soh", "rte"):
            out[key] = {"mode": "delta", "value": val * (direction if direction else -1)}
        elif key == "aux":
            if unit == "x" or between.rstrip().endswith("x") or unit == "times":
                out[key] = {"mode": "scale", "value": val}
            else:
                d = direction if direction else 1
                out[key] = {"mode": "scale", "value": 1 + d * val / 100.0}
        elif key == "containers":
            if direction or has_by:
                out[key] = {"mode": "delta", "value": val * (direction if direction else 1)}
            else:
                out[key] = {"mode": "abs", "value": val}
        elif key == "discharge_time":
            out[key] = {"mode": "abs", "value": val}
        else:  # dod, availability, mismatch, temperature
            if (direction or has_by) and not has_to:
                out[key] = {"mode": "delta", "value": val * (direction if direction else 1)}
            else:
                out[key] = {"mode": "abs", "value": val}
    return out


def apply_changes(inp: Inputs, n_init: int, changes: dict) -> tuple[Inputs, int, list[str]]:
    """Apply parsed changes; returns (new inputs, new container count, readable list)."""
    new = replace(inp)
    n = n_init
    notes = []
    for key, ch in changes.items():
        mode, v = ch["mode"], ch["value"]
        if key == "dod":
            new.dod_pct = min(100.0, max(1.0, v if mode == "abs" else new.dod_pct + v))
            notes.append(f"DOD {inp.dod_pct:g} % -> {new.dod_pct:g} %")
        elif key == "availability":
            new.availability_pct = min(100.0, max(1.0, v if mode == "abs" else new.availability_pct + v))
            notes.append(f"Availability {inp.availability_pct:g} % -> {new.availability_pct:g} %")
        elif key == "mismatch":
            base = new.mismatch_constant_pct
            new.mismatch_mode = "constant"
            new.mismatch_constant_pct = max(0.0, v if mode == "abs" else base + v)
            notes.append(f"Mismatch loss {base:g} % -> {new.mismatch_constant_pct:g} %")
        elif key == "temperature":
            new.operating_temp_c = v if mode == "abs" else new.operating_temp_c + v
            notes.append(f"Operating temperature {inp.operating_temp_c:g} degC -> {new.operating_temp_c:g} degC")
        elif key == "soh":
            new.soh_delta_pp = inp.soh_delta_pp + v
            notes.append(f"SOH {v:+g} percentage points in every year from year 1")
        elif key == "rte":
            new.rte_delta_pp = inp.rte_delta_pp + v
            notes.append(f"DC RTE {v:+g} percentage points in every year")
        elif key == "aux":
            new.aux_scale = inp.aux_scale * v
            notes.append(f"Auxiliary loads x{v:g}")
        elif key == "discharge_time":
            new.time_mode = "manual"
            new.discharge_time_h = v
            new.charge_time_h = inp.charge_time_h if inp.time_mode == "manual" else 1.0 / inp.c_rate
            notes.append(f"Discharge time -> {v:g} h")
        elif key == "containers":
            n = int(round(v)) if mode == "abs" else int(n_init + v)
            n = max(1, n)
            notes.append(f"Containers {n_init} -> {n}")
    return new, n, notes


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
def _pick(res: CaseResult, y: int) -> dict:
    d, c, p = res.dis.loc[y], res.chg.loc[y], res.perf.loc[y]
    return {
        "Installed DC capacity (MWh)": d["Installed DC capacity (MWh)"] + d["Augmented capacity (MWh)"],
        "DC-side guaranteed discharge capacity (MWh)": d["Guaranteed DC discharge capacity (MWh)"] + d["Augmented DC capacity (MWh)"],
        "AC / POI discharge excl. aux (MWh)": d["Guaranteed capacity at POI excl. aux (MWh)"],
        "Aux energy - discharge (MWh)": d["Aux energy (MWh)"],
        "AC / POI discharge incl. aux (MWh)": d["AC MV energy incl. augmentation & aux at POI (MWh)"],
        "AC / POI charge incl. aux (MWh)": c["AC MV energy incl. augmentation & aux at POI (MWh)"],
        "Aux energy - charge (MWh)": c["Aux energy (MWh)"],
        "DC discharge efficiency (%)": d["DC discharge efficiency (%)"],
        "SOH (%)": d["SOH (%)"],
        "AC chain efficiency (%)": d["AC chain efficiency (%)"],
        "AC RTE excl. aux (%)": p["AC RTE excl. aux (%)"],
        "AC RTE incl. aux (%)": p["AC RTE incl. aux (%)"],
        "Net power at POI (MW)": p["Net power at POI (MW)"],
    }


def compare(base: CaseResult, scen: CaseResult) -> dict:
    last = base.inputs.project_years
    tables = {}
    for label, y in (("BOL (year 0)", 0), (f"EOL (year {last})", last)):
        b, s = _pick(base, y), _pick(scen, y)
        rows = []
        for k in b:
            diff = s[k] - b[k]
            rel = (diff / b[k] * 100) if abs(b[k]) > 1e-12 else float("nan")
            rows.append({"Parameter": k, "Before": b[k], "After": s[k], "Change": diff, "Change (%)": rel})
        tables[label] = pd.DataFrame(rows)
    yr = pd.DataFrame(
        {
            "Year": base.perf.index,
            "Required (MWh)": base.verify["Required energy (MWh)"].values,
            "Before (MWh)": base.perf["Net discharge energy at POI (MWh)"].values,
            "After (MWh)": scen.perf["Net discharge energy at POI (MWh)"].values,
            "Before RTE (%)": base.perf["AC RTE incl. aux (%)"].values,
            "After RTE (%)": scen.perf["AC RTE incl. aux (%)"].values,
            "Before OK": base.verify["All OK"].values,
            "After OK": scen.verify["All OK"].values,
        }
    )
    yr["Change (MWh)"] = yr["After (MWh)"] - yr["Before (MWh)"]
    return {"tables": tables, "years": yr}


def containers_to_comply(container, inp_scen: Inputs, n_base: int) -> dict:
    """Containers needed (in the scenario) to satisfy every requirement."""
    n_min = minimum_containers(container, inp_scen)
    if n_min is None:
        return {"n_required": None, "delta": None}
    return {"n_required": n_min, "delta": n_min - n_base}


# ---------------------------------------------------------------------------
# explanation
# ---------------------------------------------------------------------------
def explain(changes: dict, base: CaseResult, scen: CaseResult, comp: dict, need: dict | None = None) -> str:
    last = base.inputs.project_years
    eol = comp["tables"][f"EOL (year {last})"].set_index("Parameter")
    bol = comp["tables"]["BOL (year 0)"].set_index("Parameter")

    def pct(tbl, k):
        v = tbl.loc[k, "Change (%)"]
        return "n/a" if pd.isna(v) else f"{v:+.2f} %"

    def val(tbl, k, col="After", f="{:.2f}"):
        return f.format(tbl.loc[k, col])

    lines = []
    for key in changes:
        if key == "dod":
            lines.append(
                "**DOD** scales the usable DC energy linearly, and because the AC chain is a fixed multiplier the "
                f"POI energy moves by the same proportion (BOL DC capacity {pct(bol, 'DC-side guaranteed discharge capacity (MWh)')}). "
                "AC RTE excluding auxiliaries is unchanged (DOD cancels between discharge and charge), but auxiliary energy is "
                f"a fixed block per cycle, so RTE including auxiliaries moves from {val(bol, 'AC RTE incl. aux (%)', 'Before')} % to "
                f"{val(bol, 'AC RTE incl. aux (%)')} % at BOL."
            )
        elif key == "soh":
            lines.append(
                "**SOH** multiplies every DC-side year after year 0, so the effect grows with age and is largest at EOL "
                f"(EOL DC capacity {pct(eol, 'DC-side guaranteed discharge capacity (MWh)')}). The AC efficiencies and the auxiliary "
                "energy do not change, so net energy at POI falls by slightly more than the DC loss in relative terms; RTE "
                "excluding auxiliaries is unaffected by SOH."
            )
        elif key == "availability":
            lines.append(
                "**BESS availability** is a straight multiplier on guaranteed capacity at every year "
                f"(EOL POI energy {pct(eol, 'AC / POI discharge incl. aux (MWh)')}); efficiencies and RTE are unchanged, "
                "auxiliary energy is unchanged."
            )
        elif key == "mismatch":
            lines.append(
                "**Mismatch losses** reduce the DC capacity by the factor (1 - loss) in each year "
                f"(EOL DC capacity {pct(eol, 'DC-side guaranteed discharge capacity (MWh)')}). RTE excluding auxiliaries stays the same."
            )
        elif key == "temperature":
            f0, f1 = base.summary["calendar_factor_pct"], scen.summary["calendar_factor_pct"]
            lines.append(
                "**Operating temperature** selects the calendar-degradation column of the database. The FAT/SAT calendar factor "
                f"changes from {f0:.2f} % to {f1:.2f} %, a one-off step that persists in all years "
                f"(DC capacity {pct(bol, 'DC-side guaranteed discharge capacity (MWh)')})."
            )
        elif key == "rte":
            lines.append(
                "**DC RTE** enters twice: DC discharge efficiency is sqrt(RTE), and the charge-side energy is divided by the "
                f"RTE. Net effect: AC RTE excluding auxiliaries {val(eol, 'AC RTE excl. aux (%)', 'Before')} % -> "
                f"{val(eol, 'AC RTE excl. aux (%)')} % at EOL, discharge energy at POI {pct(eol, 'AC / POI discharge excl. aux (MWh)')}."
            )
        elif key == "aux":
            lines.append(
                "**Auxiliary load** does not affect DC capacity or the AC chain; it only changes the auxiliary energy "
                f"(discharge {val(bol, 'Aux energy - discharge (MWh)', 'Before')} -> {val(bol, 'Aux energy - discharge (MWh)')} MWh at BOL, "
                f"charge {val(bol, 'Aux energy - charge (MWh)', 'Before')} -> {val(bol, 'Aux energy - charge (MWh)')} MWh), which lowers "
                f"net energy at POI by the same absolute amount and RTE including auxiliaries to {val(eol, 'AC RTE incl. aux (%)')} % at EOL."
            )
        elif key == "discharge_time":
            lines.append(
                "**Discharge time** sets the auxiliary energy (load x hours) and the net power at POI "
                f"(energy / time): EOL power {val(eol, 'Net power at POI (MW)', 'Before')} -> {val(eol, 'Net power at POI (MW)')} MW."
            )
        elif key == "containers":
            lines.append(
                f"**Number of containers** changes installed energy from {val(bol, 'Installed DC capacity (MWh)', 'Before')} to "
                f"{val(bol, 'Installed DC capacity (MWh)')} MWh; every capacity scales with it while efficiencies stay fixed, and "
                "auxiliary load scales with the container count."
            )
    first_fail = scen.summary["first_failing_year"]
    status = "all yearly requirements are still met" if scen.summary["sizing_ok"] else (
        f"the requirements are no longer met (first failing year: {first_fail})"
    )
    lines.append(
        f"**System result:** BOL net energy {bol.loc['AC / POI discharge incl. aux (MWh)', 'Before']:.2f} -> "
        f"{bol.loc['AC / POI discharge incl. aux (MWh)', 'After']:.2f} MWh, EOL {eol.loc['AC / POI discharge incl. aux (MWh)', 'Before']:.2f} -> "
        f"{eol.loc['AC / POI discharge incl. aux (MWh)', 'After']:.2f} MWh; EOL RTE incl. aux "
        f"{eol.loc['AC RTE incl. aux (%)', 'Before']:.2f} -> {eol.loc['AC RTE incl. aux (%)', 'After']:.2f} %; {status}."
    )
    if need and need.get("n_required") is not None:
        d = need["delta"]
        if d > 0:
            lines.append(f"**Augmentation / sizing impact:** about {d} more container(s) at start ({need['n_required']} in total) would be needed to restore compliance.")
        elif d < 0:
            lines.append(f"**Augmentation / sizing impact:** compliance could be met with {-d} fewer container(s) ({need['n_required']} in total).")
        else:
            lines.append("**Augmentation / sizing impact:** the current container count is still the minimum that complies.")
    return "\n\n".join(f"- {x}" for x in lines)


# ---------------------------------------------------------------------------
# optional Claude narration / Q&A
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a senior BESS (battery energy storage) sizing engineer. You are given JSON with a baseline sizing "
    "result and, optionally, a what-if scenario. Explain impacts on DC capacity, AC capacity, efficiency, RTE, "
    "auxiliary energy, guaranteed capacity, year-wise performance and augmentation need. Use only numbers present in "
    "the JSON, state assumptions, and be concise (under 250 words)."
)


def llm_reply(api_key: str, model: str, context: dict, question: str) -> str:
    import anthropic  # imported lazily so the app runs without the package/key

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=700,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Context:\n{json.dumps(context, default=float)[:12000]}\n\nRequest: {question}"}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def result_context(res: CaseResult) -> dict:
    s = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.summary.items()}
    return {
        "summary": s,
        "years": res.perf.round(3).reset_index().to_dict("records"),
        "verification": res.verify.round(3).reset_index().to_dict("records"),
    }
