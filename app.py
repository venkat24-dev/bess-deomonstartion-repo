"""BESS Sizing Software - Streamlit app.

Run locally:   streamlit run app.py
Deploy:        push the repository to GitHub and point Streamlit Community Cloud at app.py
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit.errors import StreamlitAuthError, StreamlitSecretNotFoundError

from bess import analyser
from bess.catalog_import import (
    MAX_CATALOG_UPLOAD_SIZE_MB,
    CatalogUploadError,
    CatalogValidationError,
    parse_catalog_upload,
)
from bess.calculation_trace import build_trace_map, render_calculation_info
from bess.database import AC_KEYS, AC_LABELS, load_database, select_by_c_rate
from bess.dashboard import render_engineering_dashboard
from bess.engine import (
    AugEvent,
    Inputs,
    band_index,
    compute_case,
    default_requirements,
    effective_params,
    ac_chain_table,
    size_container,
    status_labels,
)
from bess.report import BAD, GOOD, build_excel, verification_frame
from bess.api_client import BESSAPIClient, BESSAPIError
from bess.project_workspace import render_profile, render_project_workspace
from bess.streamlit_auth import (
    APIAccessTokenUnavailable,
    api_access_token,
    bind_authenticated_user,
    clear_authenticated_session,
    is_logged_in,
)

st.set_page_config(page_title="BESS Sizing", page_icon="🔋", layout="wide")

DB_PATH = Path(__file__).parent / "data" / "BESS_DATABASE.xlsx"
MAX_YEARS = 15


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _default_db():
    return load_database(DB_PATH)


@st.cache_data(show_spinner=False)
def _uploaded_db(data: bytes, filename: str):
    return parse_catalog_upload(data, filename)


def _status_style(v):
    if v in GOOD:
        return "background-color:#c6efce;color:#006100;font-weight:600"
    if v in BAD:
        return "background-color:#ffc7ce;color:#9c0006;font-weight:600"
    return ""


def show_df(df: pd.DataFrame, status_cols=(), precision=3, height=None):
    sty = df.style.format(precision=precision)
    cols = [c for c in status_cols if c in df.columns]
    if cols:
        sty = sty.map(_status_style, subset=cols)
    kwargs = {"height": height} if height else {}
    st.dataframe(sty, width="stretch", **kwargs)


def api_key() -> str | None:
    try:
        k = st.secrets.get("ANTHROPIC_API_KEY")
    except Exception:
        k = None
    return k or os.environ.get("ANTHROPIC_API_KEY")


def yesno(label, key, default=False, help=None):
    return st.radio(label, ["No", "Yes"], index=1 if default else 0, horizontal=True, key=key, help=help) == "Yes"


def _company_auth_sidebar():
    st.sidebar.divider()
    st.sidebar.header("Company account")
    try:
        auth_settings = st.secrets.get("auth")
    except StreamlitSecretNotFoundError:
        auth_settings = None
    provider = os.environ.get("BESS_OIDC_PROVIDER", "microsoft").strip()
    provider_settings = auth_settings.get(provider) if isinstance(auth_settings, Mapping) else None
    exposed_tokens = auth_settings.get("expose_tokens", []) if isinstance(auth_settings, Mapping) else []
    if isinstance(exposed_tokens, str):
        exposed_tokens = [exposed_tokens]
    api_base_url = os.environ.get("BESS_API_BASE_URL", "").strip()
    missing = []
    if not isinstance(auth_settings, Mapping):
        missing.extend(
            ["auth.redirect_uri", "auth.cookie_secret", f"auth.{provider}"]
        )
    else:
        for setting in ("redirect_uri", "cookie_secret"):
            if not auth_settings.get(setting):
                missing.append(f"auth.{setting}")
        if not isinstance(provider_settings, Mapping):
            missing.append(f"auth.{provider}")
        else:
            for setting in ("client_id", "client_secret", "server_metadata_url"):
                if not provider_settings.get(setting):
                    missing.append(f"auth.{provider}.{setting}")
            client_kwargs = provider_settings.get("client_kwargs", {})
            scopes = (
                client_kwargs.get("scope", "").split()
                if isinstance(client_kwargs, Mapping)
                else []
            )
            if not any(
                scope not in {"openid", "profile", "email", "offline_access"}
                for scope in scopes
            ):
                missing.append(f"auth.{provider}.client_kwargs.scope (API delegated scope)")
    if "access" not in exposed_tokens:
        missing.append("auth.expose_tokens = [\"access\"]")
    if not api_base_url:
        missing.append("BESS_API_BASE_URL")
    if missing:
        st.sidebar.caption("○ Backend sign-in is not configured")
        st.sidebar.caption("Local calculator available; project storage is offline.")
        with st.sidebar.expander("Required sign-in configuration"):
            st.write("Provide these server-side settings:")
            st.code("\n".join(missing))
        if is_logged_in(st.user) and st.sidebar.button(
            "Sign out", key="bess_logout_unconfigured"
        ):
            clear_authenticated_session(st.session_state)
            st.logout()
            st.stop()
        return None, None

    if not is_logged_in(st.user):
        st.sidebar.caption("○ Not signed in")
        if st.sidebar.button("Sign in with company SSO", key="bess_login"):
            try:
                st.login(provider)
            except StreamlitAuthError:
                st.sidebar.warning("Company sign-in is unavailable; check OIDC setup.")
                with st.sidebar.expander("Sign-in troubleshooting"):
                    st.write(
                        "Verify server-side OIDC settings, provider registration, "
                        "callback URL, and the Authlib dependency."
                    )
        return api_base_url, None
    st.sidebar.caption("● Signed in")
    try:
        token = api_access_token(st.user)
    except APIAccessTokenUnavailable as exc:
        st.sidebar.warning("Company account is signed in, but no API access token is available.")
        with st.sidebar.expander("Token configuration"):
            st.write(str(exc))
        token = None
    if token:
        st.sidebar.caption("● Company API connected")
    if st.sidebar.button("Sign out", key="bess_logout"):
        clear_authenticated_session(st.session_state)
        st.logout()
        st.stop()
    return api_base_url, token


def _render_workspace_page(page, client, profile, projects, recent_runs=None):
    if page == "Home / Projects":
        render_project_workspace(st, client, profile, projects, recent_runs)
    elif page == "Profile":
        render_profile(st, profile, projects, recent_runs)
    else:
        raise ValueError(f"Unknown workspace page: {page}")


# ---------------------------------------------------------------------------
# sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Workspace")
    workspace_page = st.radio(
        "Page",
        ["Home / Projects", "Calculator", "Profile"],
        key="bess_workspace_navigation",
        label_visibility="collapsed",
    )
    st.divider()
    st.header("Settings")
    st.subheader("Product Database")
    up = st.file_uploader(
        "Product database (.xlsx)",
        help="Leave empty to use the bundled BESS_DATABASE.xlsx. CSV files are not supported.",
        max_upload_size=MAX_CATALOG_UPLOAD_SIZE_MB,
    )
    st.caption(
        f"Excel workbook only · Maximum file size {MAX_CATALOG_UPLOAD_SIZE_MB} MB"
    )
    catalog_state_key = "last_valid_product_catalog"
    if catalog_state_key not in st.session_state:
        st.session_state[catalog_state_key] = _default_db()
    if up is None:
        db = _default_db()
        st.session_state[catalog_state_key] = db
        db_source = "Bundled"
    else:
        try:
            db = _uploaded_db(up.getvalue(), up.name)
            st.session_state[catalog_state_key] = db
            db_source = "Uploaded"
        except CatalogValidationError as exc:
            st.error("Could not use the uploaded product database: " + "; ".join(exc.report.errors))
            db = st.session_state[catalog_state_key]
            db_source = "Last valid"
            st.warning("The existing product catalog is unchanged and remains active.")
        except CatalogUploadError as exc:
            st.error(str(exc))
            db = st.session_state[catalog_state_key]
            db_source = "Last valid"
            st.warning("The existing product catalog is unchanged and remains active.")
    st.caption("Active catalog")
    st.caption(f"● {db_source} database: {len(db)} container options")
    st.divider()
    st.subheader("Calculation Preferences")
    status_style = st.radio(
        "Match result labels",
        ["MATCHED / NOT MATCHED", "TRUE / FALSE"],
        help="Changes the displayed labels only; calculation results are unchanged.",
    ).split(" / ")[0]
    status_style = "TRUE/FALSE" if status_style == "TRUE" else "MATCHED"
    aux_kw_fix = False
    if any(c.aux_suspect for c in db):
        aux_kw_fix = st.checkbox(
            "Read flagged aux loads as kW",
            value=False,
            help="For flagged catalog values only, divide suspected kW values by 1000. "
            "Unflagged container values are not changed.",
        )
    else:
        st.caption("No flagged auxiliary-load values in the active catalog.")
    st.divider()
    api_base_url, access_token = _company_auth_sidebar()
    st.divider()
    st.caption("BESS Sizing Software")
    st.caption("Specification: BESS_SIZING_2_0")

workspace_profile, workspace_projects, workspace_recent_runs = None, None, None
if workspace_page != "Calculator" and api_base_url and access_token:
    try:
        with BESSAPIClient(api_base_url, access_token) as api_client:
            with st.spinner("Loading your authorized workspace..."):
                workspace_profile = api_client.get_profile()
            authenticated_user_id = workspace_profile["user"]["id"]
            if not bind_authenticated_user(st.session_state, authenticated_user_id):
                st.logout()
                st.stop()
            with st.spinner("Loading projects and recent sizing activity..."):
                workspace_projects = api_client.list_projects()
                workspace_recent_runs = api_client.list_recent_sizing_runs()
            _render_workspace_page(workspace_page, api_client, workspace_profile, workspace_projects, workspace_recent_runs)
    except (BESSAPIError, ValueError) as exc:
        if isinstance(exc, BESSAPIError) and exc.status_code == 401:
            clear_authenticated_session(st.session_state)
            st.logout()
            st.stop()
        safe_message = (
            str(exc)
            if isinstance(exc, BESSAPIError)
            else "BESS_API_BASE_URL is not a valid secure API URL."
        )
        st.error(safe_message)
elif workspace_page != "Calculator":
    _render_workspace_page(workspace_page, None, None, None)

if workspace_page != "Calculator":
    st.stop()

st.title("🔋 BESS Sizing Software")
st.caption("Container selection, year-wise discharge / charge performance, BOL-EOL verification, augmentation and what-if analysis.")

tab_dashboard, tab_in, tab_res, tab_an, tab_db = st.tabs(
    [
        "Engineering Dashboard",
        "Calculator - Inputs",
        "Calculator - Results",
        "Calculator - Analyser",
        "Calculator - Database",
    ],
    key="bess_calculator_tabs",
    on_change="rerun",
)

# ---------------------------------------------------------------------------
# INPUTS
# ---------------------------------------------------------------------------
with tab_in:
    st.subheader("1. Project inputs")
    c1, c2, c3 = st.columns(3)
    with c1:
        req_e = st.number_input("Required energy as per project (MWh)", 1.0, 1e6, 100.0, 1.0)
        render_calculation_info(
            st,
            "required_energy",
            trace=build_trace_map({"required_energy_mwh": req_e}).get("required_energy"),
            title="Energy",
        )
    with c2:
        req_p = st.number_input("Required power as per project (MW)", 0.1, 1e5, 25.0, 0.5)
        render_calculation_info(
            st,
            "required_power",
            trace=build_trace_map({"required_power_mw": req_p}).get("required_power"),
            title="Power",
        )
    with c3:
        crate = st.number_input("C-rate", 0.05, 2.0, 0.25, 0.01, help="Used to pick the container from the database")
        render_calculation_info(
            st,
            "c_rate",
            trace=build_trace_map({"c_rate": crate, "discharge_time_h": 1 / crate, "charge_time_h": 1 / crate}).get("c_rate"),
            title="C-rate",
        )

    tmode = st.radio(
        "Charge / discharge duration",
        ["Calculate from C-rate (1 / C-rate)", "Enter duration directly"],
        horizontal=True,
    )
    manual_time = tmode.startswith("Enter")
    td_in = tc_in = 1.0 / crate
    if manual_time:
        t1, t2 = st.columns(2)
        td_in = t1.number_input("Discharge time (h)", 0.25, 24.0, round(1 / crate, 2), 0.25)
        tc_in = t2.number_input("Charge time (h)", 0.25, 24.0, round(1 / crate, 2), 0.25)
    else:
        st.caption(f"Duration = 1 / {crate:g} = {1 / crate:.2f} h for both charge and discharge")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        years = int(st.number_input("Total project duration (years)", 1, MAX_YEARS, 10, 1))
        render_calculation_info(
            st,
            "verification",
            trace=build_trace_map({"project_years": years}).get("verification"),
            title="Duration",
        )
    with c2:
        temp = st.number_input("Operating temperature (°C)", -20.0, 60.0, 25.0, 1.0)
    with c3:
        avail = st.number_input("BESS availability (%)", 50.0, 100.0, 99.0, 0.1)
        render_calculation_info(
            st,
            "availability",
            trace=build_trace_map({"availability_pct": avail}).get("availability"),
            title="Availability",
        )
    with c4:
        dod = st.number_input("DOD (%)", 10.0, 100.0, 100.0, 1.0)

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        bol_e = st.number_input("BOL energy (MWh)", 1.0, 1e6, float(req_e), 1.0, key=f"bol_{req_e}")
    with c2:
        eol_e = st.number_input("EOL energy (MWh)", 1.0, 1e6, float(req_e), 1.0, key=f"eol_{req_e}")
    with c3:
        min_rte = st.number_input("Minimum RTE at EOL (%)", 10.0, 100.0, 80.0, 0.5)
    with c4:
        cycles_req = int(st.number_input("Number of cycles (project)", 1, 100000, 365 * years, 1, key=f"cyc_{years}"))

    c1, c2 = st.columns(2)
    with c1:
        aux_required = yesno("Auxiliary consumption required?", "aux_req", True)
    with c2:
        infra_p = st.number_input(
            "AC MV power to plant infrastructure (MW)", 0.0, 1e4, 0.0, 0.1,
            help="Deducted from discharge energy / added to charge energy at POI (spec sections 19 and 21).",
        )

    mm_mode = st.radio("Mismatch losses", ["Same for every year", "Varies from year to year"], horizontal=True)
    mm_const, mm_yearly = 1.0, None
    if mm_mode.startswith("Same"):
        mm_const = st.number_input("Mismatch loss (% per year, applied as 1 - loss)", 0.0, 50.0, 1.0, 0.1)
    else:
        mm_df = st.data_editor(
            pd.DataFrame({"Year": list(range(years + 1)), "Mismatch loss (%)": [1.0] * (years + 1)}),
            disabled=["Year"], hide_index=True, key=f"mm_{years}", width="stretch",
        )
        mm_yearly = [float(x) if pd.notna(x) else 0.0 for x in mm_df["Mismatch loss (%)"]]

    # ---- sizing control / container selection
    with st.expander("Container selection and sizing control"):
        sel_mode = st.radio("Container selection", ["Automatic (nearest C-rate in database)", "Choose manually"], horizontal=True)
        if sel_mode.startswith("Automatic"):
            cands = select_by_c_rate(db, crate)
        else:
            labels = {c.id: c.label for c in db}
            chosen = st.multiselect("Containers", list(labels), default=[db[0].id], format_func=lambda i: labels[i])
            cands = [c for c in db if c.id in chosen]
        m1, m2, m3 = st.columns(3)
        mg_min = m1.number_input("Initial energy margin - min (%)", 0.0, 100.0, 14.0, 0.5)
        mg_max = m2.number_input("Initial energy margin - max (%)", 0.0, 100.0, 20.0, 0.5)
        manual_n = int(m3.number_input("Fixed number of containers (0 = automatic)", 0, 100000, 0, 1))
        ext = st.checkbox("If 14-20 % is not enough, search beyond the range", value=True)
        mpt_src = st.selectbox(
            "Main power transformer efficiency taken from",
            ["Transformer efficiency - discharging (spec section 4)", "HV transformer row of the database"],
        )
    if not cands:
        st.error("Select at least one container.")
        st.stop()
    exact = sorted({c.c_rate for c in cands})
    st.info(
        f"Requested C-rate {crate:g} → database C-rate {', '.join(f'{x:g}' for x in exact)}: "
        + "; ".join(c.label for c in cands)
        + (" (both options are sized)" if len(cands) > 1 else "")
    )

    # ---- additional aux / standby (section 14)
    with st.expander("Additional auxiliary and standby energy (section 14)"):
        if not aux_required:
            st.caption("Auxiliary consumption is switched off - these values are ignored.")
        ea = yesno("Additional auxiliary energy applicable?", "ea")
        ea_val = st.number_input("Additional auxiliary energy (MWh per cycle)", 0.0, 1e5, 0.0, 0.1) if ea else 0.0
        sb = yesno("Standby energy applicable?", "sb")
        sb_mode, sb_val = "manual", 0.0
        if sb:
            sb_mode = "manual" if st.radio("Standby energy", ["Enter value", "From database standby load x idle hours"], horizontal=True).startswith("Enter") else "database"
            if sb_mode == "manual":
                sb_val = st.number_input("Standby energy (MWh per cycle)", 0.0, 1e5, 0.0, 0.1)
        side = st.selectbox("Book additional / standby energy on", ["Discharge side", "Charge side"])

    # ---- FAT / SAT (section 16)
    with st.expander("FAT / SAT period (section 16)", expanded=True):
        fs = yesno("FAT / SAT applicable?", "fs", True)
        fs_months, cal_over = 3, None
        if fs:
            fs_months = st.slider("Duration (months, max 6)", 1, 6, 3)
            try:
                idx = band_index(cands[0], temp)
                pre = [round(r[idx] * 100, 4) for r in cands[0].cal_rows[:fs_months]]
                band = cands[0].temp_labels[idx]
            except ValueError as exc:
                pre, band = [0.0] * fs_months, None
                st.warning(str(exc))
            if band:
                st.caption(f"Monthly calendar degradation from the database for {temp:g} °C (band {band}):")
            if st.checkbox("Enter monthly values manually"):
                ed = st.data_editor(
                    pd.DataFrame({"Month": range(1, fs_months + 1), "Calendar degradation (%)": pre}),
                    disabled=["Month"], hide_index=True, key=f"cal_{cands[0].id}_{temp}_{fs_months}",
                )
                cal_over = [float(x) for x in ed["Calendar degradation (%)"].fillna(0.0)]
            else:
                st.dataframe(pd.DataFrame({"Month": range(1, fs_months + 1), "Calendar degradation (%)": pre}), hide_index=True)
        else:
            st.caption("No FAT/SAT storage period: calendar-degradation factor = 100 %.")

    # ---- augmentation (section 17)
    aug_on = False
    aug_events: list[AugEvent] = []
    aug_basis = "own"
    with st.expander("Augmentation (section 17)"):
        aug_on = yesno("Augmentation capacity applicable?", "aug")
        if aug_on:
            st.dataframe(
                pd.DataFrame(
                    {
                        "Container": [c.label for c in cands],
                        "DC energy / container (MWh)": [c.energy_mwh for c in cands],
                        "Aux discharge (MW)": [effective_params(c, Inputs(aux_kw_fix=aux_kw_fix))["aux_dis"] for c in cands],
                        "Aux charge (MW)": [effective_params(c, Inputs(aux_kw_fix=aux_kw_fix))["aux_ch"] for c in cands],
                    }
                ),
                hide_index=True, width="stretch",
            )
            ev = st.data_editor(
                pd.DataFrame({"Year": [min(5, years)], "Containers": [4], "Type": ["Add"]}),
                num_rows="dynamic", hide_index=True, width="stretch", key=f"aug_{years}",
                column_config={
                    "Year": st.column_config.NumberColumn("Year of augmentation", min_value=1, max_value=years, step=1),
                    "Containers": st.column_config.NumberColumn("Containers added / replaced", min_value=1, step=1),
                    "Type": st.column_config.SelectboxColumn("Type", options=["Add", "Replace"]),
                },
            )
            for _, row in ev.dropna().iterrows():
                aug_events.append(AugEvent(int(row["Year"]), int(row["Containers"]), str(row["Type"])))
            aug_basis = "own" if st.radio(
                "SOH / DC RTE of augmented containers",
                ["Age since augmentation", "Same table index as the project year"],
                horizontal=True,
            ).startswith("Age") else "year"
            st.caption("Containers are added at the start of the stated year. 'Replace' removes initial containers and installs new ones.")

    # ---- overrides
    ac_over: dict[str, dict] = {}
    aux_over: dict[str, dict] = {}
    with st.expander("Override database values (optional)"):
        st.caption("Values are pre-filled from the database. Change a cell to override it for that container; empty aux cells mean 'not in database'.")
        base_for_view = Inputs(aux_kw_fix=aux_kw_fix, mpt_source="hv" if mpt_src.startswith("HV") else "spec")
        for c in cands:
            ep = effective_params(c, base_for_view)
            rows = [
                {"Parameter": "Aux load - discharge (MW per container)", "Value": ep["aux_dis"] if (c.aux_discharge_mw is not None) else None},
                {"Parameter": "Aux load - charge (MW per container)", "Value": ep["aux_ch"] if (c.aux_charge_mw is not None) else None},
                {"Parameter": "Aux load - standby (MW per container)", "Value": ep["aux_sb"] if (c.aux_standby_mw is not None) else None},
            ] + [{"Parameter": f"{AC_LABELS[k]} (%)", "Value": ep["ac"][k] * 100} for k in AC_KEYS]
            st.markdown(f"**{c.label}**")
            edited = st.data_editor(pd.DataFrame(rows), disabled=["Parameter"], hide_index=True, key=f"ov_{c.id}_{aux_kw_fix}_{mpt_src[:3]}", width="stretch")
            a_o, x_o = {}, {}
            for i, k in enumerate(["discharge", "charge", "standby"]):
                new, old = edited.iloc[i]["Value"], rows[i]["Value"]
                if pd.notna(new) and (old is None or abs(float(new) - float(old)) > 1e-12):
                    x_o[k] = float(new)
            for i, k in enumerate(AC_KEYS):
                new, old = edited.iloc[3 + i]["Value"], rows[3 + i]["Value"]
                if pd.notna(new) and abs(float(new) - float(old)) > 1e-9:
                    a_o[k] = float(new) / 100.0
            ac_over[c.id], aux_over[c.id] = a_o, x_o

    # ---- required values by year (section 15)
    st.subheader("2. Required values at the end of each year (section 15)")
    st.caption("Pre-filled: energy runs linearly from the BOL to the EOL requirement. Edit any cell; the calculated values are compared against these.")
    tmp = Inputs(project_years=years, bol_energy_mwh=bol_e, eol_energy_mwh=eol_e, min_rte_eol_pct=min_rte, required_power_mw=req_p)
    defaults = default_requirements(tmp)
    req_ed = st.data_editor(
        defaults, disabled=["Year"], hide_index=True, width="stretch",
        key=f"req_{years}_{bol_e}_{eol_e}_{min_rte}_{req_p}",
    )
    req_energy = [float(x) if pd.notna(x) else float(d) for x, d in zip(req_ed.iloc[:, 1], defaults.iloc[:, 1])]
    req_rte = [float(x) if pd.notna(x) else float(d) for x, d in zip(req_ed.iloc[:, 2], defaults.iloc[:, 2])]
    req_pow = [float(x) if pd.notna(x) else float(d) for x, d in zip(req_ed.iloc[:, 3], defaults.iloc[:, 3])]

inp = Inputs(
    required_energy_mwh=req_e,
    required_power_mw=req_p,
    c_rate=crate,
    time_mode="manual" if manual_time else "c_rate",
    discharge_time_h=td_in,
    charge_time_h=tc_in,
    mismatch_mode="constant" if mm_mode.startswith("Same") else "yearly",
    mismatch_constant_pct=mm_const,
    mismatch_yearly_pct=mm_yearly,
    operating_temp_c=temp,
    availability_pct=avail,
    bol_energy_mwh=bol_e,
    eol_energy_mwh=eol_e,
    min_rte_eol_pct=min_rte,
    aux_required=aux_required,
    project_years=years,
    cycles_required=cycles_req,
    dod_pct=dod,
    extra_aux_applicable=ea,
    extra_aux_mwh=ea_val,
    standby_applicable=sb,
    standby_mode=sb_mode,
    standby_mwh=sb_val,
    extras_side="discharge" if side.startswith("Discharge") else "charge",
    fatsat_applicable=fs,
    fatsat_months=fs_months,
    caldeg_override_pct=cal_over,
    aug_enabled=aug_on,
    aug_events=aug_events,
    aug_age_basis=aug_basis,
    plant_infra_power_mw=infra_p,
    req_energy_by_year=req_energy,
    req_rte_by_year_pct=req_rte,
    req_power_by_year=req_pow,
    margin_min_pct=mg_min,
    margin_max_pct=max(mg_max, mg_min),
    allow_extended_margin=ext,
    manual_containers=manual_n,
    mpt_source="hv" if mpt_src.startswith("HV") else "spec",
    aux_kw_fix=aux_kw_fix,
)

# ---------------------------------------------------------------------------
# RESULTS
# ---------------------------------------------------------------------------
outcomes, errors = [], []
for c in cands:
    ci = replace(inp, ac_overrides=ac_over.get(c.id, {}), aux_overrides=aux_over.get(c.id, {}))
    try:
        outcomes.append(size_container(c, ci))
    except ValueError as exc:
        errors.append(str(exc))


def show_option(o, key):
    r, s = o.result, o.result.summary
    if s["accepted"]:
        st.success("ACCEPTED - energy, power and minimum EOL RTE requirements are met over the complete project duration.")
    else:
        st.error("NOT ACCEPTED - at least one requirement is not met (see the checks below).")
    st.caption(o.note)
    m = st.columns(6)
    with m[0]:
        st.metric("Initial containers", s["n_init"])
        render_calculation_info(
            st,
            "container_selection",
            trace=build_trace_map({"required_energy_mwh": inp.required_energy_mwh, "margin_min_pct": inp.margin_min_pct, "margin_max_pct": inp.margin_max_pct, "container_energy_mwh": r.container.energy_mwh}, result=r).get("container_selection"),
            title="Container count",
        )
    with m[1]:
        st.metric("Installed DC (MWh)", f"{s['installed_mwh']:.1f}")
        render_calculation_info(
            st,
            "guaranteed_energy",
            trace=build_trace_map(result=r).get("guaranteed_energy"),
            title="Installed DC",
        )
    with m[2]:
        st.metric("Margin applied", f"{o.margin_applied_pct:.1f} %", f"effective {s['effective_margin_pct']:.1f} %", delta_color="off")
    with m[3]:
        st.metric("BOL energy at POI (MWh)", f"{s['bol_energy']:.1f}")
        render_calculation_info(st, "guaranteed_energy", trace=build_trace_map(result=r).get("guaranteed_energy"), title="BOL energy")
    with m[4]:
        st.metric("EOL energy at POI (MWh)", f"{s['eol_energy']:.1f}")
        render_calculation_info(st, "guaranteed_energy", trace=build_trace_map(result=r).get("guaranteed_energy"), title="EOL energy")
    with m[5]:
        st.metric("EOL RTE incl. aux", f"{s['rte_eol_incl']:.2f} %")
        render_calculation_info(st, "rte", trace=build_trace_map(result=r).get("rte"), title="RTE")
    for w in r.warnings:
        st.warning(w)

    t_ver, t_dis, t_chg, t_perf, t_chain, t_chart = st.tabs(
        ["Verification", "Discharging", "Charging", "RTE & power", "AC chain", "Charts"]
    )
    with t_ver:
        checks = pd.DataFrame(
            {
                "Check": [
                    "Energy at BOL ≥ BOL requirement",
                    "Energy at EOL ≥ EOL requirement",
                    "RTE at EOL ≥ minimum RTE",
                    "Energy, RTE and power ≥ required in every year",
                    "Cycle capability ≥ required cycles",
                ],
                "Result": status_labels(
                    pd.Series([s["bol_ok"], s["eol_ok"], s["rte_eol_ok"], s["years_ok"], s["cycles_ok"]]), status_style
                ).values,
            }
        )
        show_df(checks, ["Result"])
        st.markdown("**Required vs calculated, end of each year**")
        show_df(verification_frame(r, status_style), ["Energy", "RTE", "Power", "Overall"])
    with t_dis:
        st.caption(f"Discharge time {s['discharge_time_h']:.2f} h · calendar degradation factor {s['calendar_factor_pct']:.2f} % ({s['calendar_source']})")
        show_df(r.dis)
        if inp.aug_enabled:
            st.markdown("**Augmentation - discharging (section 18-19)**")
            cols = ["Initial containers", "Augmented containers", "Augmented capacity (MWh)", "Augmented DC capacity (MWh)",
                    "Augmented energy incl. aux (MWh)", "Plant infrastructure energy (MWh)", "AC MV energy incl. augmentation & aux at POI (MWh)"]
            tab = r.dis[cols].copy()
            tab["Required (MWh)"] = r.verify["Required energy (MWh)"]
            tab["Status"] = status_labels(tab[cols[-1]] >= tab["Required (MWh)"] - 1e-9, status_style)
            show_df(tab, ["Status"])
    with t_chg:
        st.caption(f"Charge time {s['charge_time_h']:.2f} h")
        show_df(r.chg)
        if inp.aug_enabled:
            st.markdown("**Augmentation - charging (section 20-21)**")
            show_df(r.chg[["Initial containers", "Augmented containers", "Augmented energy incl. aux (MWh)",
                           "Plant infrastructure energy (MWh)", "AC MV energy incl. augmentation & aux at POI (MWh)"]])
    with t_perf:
        show_df(r.perf)
    with t_chain:
        show_df(ac_chain_table(r.container, r.inputs))
        for n_ in r.container.notes:
            st.caption("⚠ " + n_)
    with t_chart:
        v = r.verify
        st.markdown("**Energy at POI (MWh)**")
        st.line_chart(pd.DataFrame({"Required": v["Required energy (MWh)"], "Calculated": v["Calculated energy (MWh)"]}))
        st.markdown("**AC RTE (%)**")
        st.line_chart(pd.DataFrame({"Excl. aux": r.perf["AC RTE excl. aux (%)"], "Incl. aux": r.perf["AC RTE incl. aux (%)"], "Required": v["Required RTE (%)"]}))


if tab_dashboard.open:
    if outcomes:
        dashboard_options = [
            item.result.summary.get("container", item.result.container.label)
            for item in outcomes
        ]
        dashboard_selection = st.selectbox(
            "Dashboard container option",
            range(len(outcomes)),
            format_func=lambda index: dashboard_options[index],
            key="bess_dashboard_container_option",
        )
        render_engineering_dashboard(
            st,
            outcomes[dashboard_selection].result,
            comparison_cases=[item.result for item in outcomes],
        )
    else:
        dashboard_error = "; ".join(errors) if errors else None
        render_engineering_dashboard(st, None, error=dashboard_error)


with tab_res:
    for c in cands:
        if c.aux_suspect and not aux_kw_fix and c.id not in aux_over.get(c.id, {}) and not aux_over.get(c.id):
            st.warning(f"{c.label}: database aux loads ({c.aux_discharge_mw:g} MW) look like kW. Tick 'Read flagged aux loads as kW' in the sidebar if that is the case.")
    for e in errors:
        st.error(e)
    if not outcomes:
        st.info("No container option could be calculated - adjust the inputs.")
    else:
        summary = pd.DataFrame(
            [
                {
                    "Container": o.result.summary["container"],
                    "Initial containers": o.result.summary["n_init"],
                    "Installed DC (MWh)": o.result.summary["installed_mwh"],
                    "Margin applied (%)": o.margin_applied_pct,
                    "Effective margin (%)": o.result.summary["effective_margin_pct"],
                    "BOL energy (MWh)": o.result.summary["bol_energy"],
                    "EOL energy (MWh)": o.result.summary["eol_energy"],
                    "EOL RTE incl. aux (%)": o.result.summary["rte_eol_incl"],
                    "Result": status_labels(pd.Series([o.result.summary["accepted"]]), status_style).iloc[0],
                }
                for o in outcomes
            ]
        )
        st.subheader("Container options")
        show_df(summary, ["Result"])
        if len(outcomes) > 1:
            st.caption("Two containers match the requested C-rate, so the sizing is shown for both.")
        tabs = st.tabs([o.result.summary["container"] for o in outcomes])
        for t, o in zip(tabs, outcomes):
            with t:
                show_option(o, o.result.container.id)
        st.download_button(
            "Download Excel report",
            build_excel(outcomes, status_style),
            file_name="bess_sizing_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

# ---------------------------------------------------------------------------
# ANALYSER
# ---------------------------------------------------------------------------
with tab_an:
    st.subheader("BESS sizing analyser")
    st.caption(
        "Change a parameter and see its effect on the whole system with the same number of containers. "
        "Type e.g. “reduce DOD to 90% and SOH by 2%”, “availability 97”, “temperature +10”, “aux x1.5”, “add 3 containers”."
    )
    if not outcomes:
        st.info("Run a valid sizing first.")
    else:
        names = [o.result.summary["container"] for o in outcomes]
        pick = st.selectbox("Baseline container option", names)
        base_o = outcomes[names.index(pick)]
        base = base_o.result
        key = api_key()
        use_llm = bool(key) and st.checkbox("Add Claude narration / answer open questions", value=True)
        if "chat" not in st.session_state:
            st.session_state.chat = []

        def respond(changes: dict, user_text: str):
            st.session_state.chat.append({"role": "user", "text": user_text})
            try:
                inp_s, n_s, notes = analyser.apply_changes(base.inputs, base.n_init, changes)
                scen = compute_case(base.container, n_s, inp_s)
            except ValueError as exc:
                st.session_state.chat.append({"role": "assistant", "text": f"Could not evaluate that scenario: {exc}"})
                return
            comp = analyser.compare(base, scen)
            need = analyser.containers_to_comply(base.container, inp_s, base.n_init)
            text = "**Interpreted as:** " + "; ".join(notes) + "\n\n" + analyser.explain(changes, base, scen, comp, need)
            if use_llm:
                try:
                    ctx = {
                        "baseline": analyser.result_context(base),
                        "scenario": analyser.result_context(scen),
                        "changes": notes,
                        "eol_comparison": comp["tables"][list(comp["tables"])[1]].round(3).to_dict("records"),
                        "extra_containers_needed": need,
                    }
                    text += "\n\n**Claude's analysis**\n\n" + analyser.llm_reply(
                        key, os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), ctx,
                        "Explain the system-level impact of these changes as a BESS engineer.",
                    )
                except Exception as exc:  # noqa: BLE001
                    text += f"\n\n_(Claude narration unavailable: {exc})_"
            st.session_state.chat.append({"role": "assistant", "text": text, "tables": comp["tables"], "years": comp["years"]})

        with st.expander("Adjust parameters manually"):
            g1, g2, g3, g4 = st.columns(4)
            n_dod = g1.number_input("DOD (%)", 10.0, 100.0, float(base.inputs.dod_pct), 1.0, key="a_dod")
            n_av = g2.number_input("Availability (%)", 50.0, 100.0, float(base.inputs.availability_pct), 0.1, key="a_av")
            n_mm = g3.number_input("Mismatch loss (%)", 0.0, 50.0, float(base.inputs.mismatch_constant_pct), 0.1, key="a_mm")
            n_tp = g4.number_input("Temperature (°C)", -20.0, 60.0, float(base.inputs.operating_temp_c), 1.0, key="a_tp")
            g1, g2, g3, g4 = st.columns(4)
            n_soh = g1.number_input("SOH change (pp)", -20.0, 20.0, 0.0, 0.5, key="a_soh")
            n_rte = g2.number_input("DC RTE change (pp)", -10.0, 10.0, 0.0, 0.1, key="a_rte")
            n_aux = g3.number_input("Aux load multiplier (x)", 0.0, 10.0, 1.0, 0.1, key="a_aux")
            n_n = g4.number_input("Containers", 1, 100000, int(base.n_init), 1, key="a_n")
            if st.button("Run what-if"):
                ch = {}
                if n_dod != base.inputs.dod_pct: ch["dod"] = {"mode": "abs", "value": n_dod}
                if n_av != base.inputs.availability_pct: ch["availability"] = {"mode": "abs", "value": n_av}
                if n_mm != base.inputs.mismatch_constant_pct: ch["mismatch"] = {"mode": "abs", "value": n_mm}
                if n_tp != base.inputs.operating_temp_c: ch["temperature"] = {"mode": "abs", "value": n_tp}
                if n_soh: ch["soh"] = {"mode": "delta", "value": n_soh}
                if n_rte: ch["rte"] = {"mode": "delta", "value": n_rte}
                if n_aux != 1.0: ch["aux"] = {"mode": "scale", "value": n_aux}
                if n_n != base.n_init: ch["containers"] = {"mode": "abs", "value": n_n}
                if ch:
                    respond(ch, "Manual what-if: " + ", ".join(analyser.PARAM_TITLES[k] for k in ch))
                else:
                    st.info("Nothing differs from the baseline.")

        hist = st.container()
        prompt = st.chat_input("Ask about a parameter change…")
        if prompt:
            ch = analyser.parse_changes(prompt)
            if ch:
                respond(ch, prompt)
            elif use_llm:
                st.session_state.chat.append({"role": "user", "text": prompt})
                try:
                    ans = analyser.llm_reply(key, os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), analyser.result_context(base), prompt)
                except Exception as exc:  # noqa: BLE001
                    ans = f"Claude is unavailable: {exc}"
                st.session_state.chat.append({"role": "assistant", "text": ans})
            else:
                st.session_state.chat.append({"role": "user", "text": prompt})
                st.session_state.chat.append(
                    {
                        "role": "assistant",
                        "text": "I could not find a parameter to change. I understand DOD, SOH, availability, mismatch, temperature, "
                        "DC RTE, aux load, discharge time and number of containers - for example "
                        "“reduce DOD to 90% and SOH by 2%”.",
                    }
                )
        with hist:
            if st.session_state.chat and st.button("Clear conversation"):
                st.session_state.chat = []
                st.rerun()
            for msg in st.session_state.chat:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["text"])
                    if "tables" in msg:
                        for label, df in msg["tables"].items():
                            with st.expander(f"Before / after - {label}"):
                                show_df(df)
                        with st.expander("Year-wise performance"):
                            show_df(msg["years"])
                            st.line_chart(msg["years"].set_index("Year")[["Required (MWh)", "Before (MWh)", "After (MWh)"]])

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
with tab_db:
    st.subheader("Product database")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Container": c.label,
                    "Sheet": c.sheet,
                    "DC energy (MWh)": c.energy_mwh,
                    "C-rate": c.c_rate,
                    "Cycles / day": c.cycles_per_day,
                    "Aux discharge (MW)": c.aux_discharge_mw,
                    "Aux charge (MW)": c.aux_charge_mw,
                    "Aux standby (MW)": c.aux_standby_mw,
                    "SOH / RTE data to year": c.max_years,
                    "Temperature range": f"{c.temp_labels[0]} … {c.temp_labels[-1]}",
                    "Data notes": len(c.notes),
                }
                for c in db
            ]
        ),
        hide_index=True, width="stretch",
    )
    for c in db:
        with st.expander(f"{c.label} - details"):
            for n_ in c.notes:
                st.warning(n_)
            a, b = st.columns(2)
            with a:
                st.markdown("**SOH (excl. calendar degradation) and DC RTE**")
                st.dataframe(pd.DataFrame({"Year": range(len(c.soh)), "SOH": c.soh, "DC RTE": c.dc_rte}), hide_index=True)
            with b:
                st.markdown("**Calendar degradation by month and temperature**")
                st.dataframe(pd.DataFrame(c.cal_rows, columns=c.temp_labels, index=range(1, len(c.cal_rows) + 1)))
            st.markdown("**AC chain (mapped to the specification)**")
            st.dataframe(ac_chain_table(c, Inputs(aux_kw_fix=aux_kw_fix)), hide_index=True, width="stretch")
