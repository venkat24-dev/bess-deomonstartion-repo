# BESS Sizing Software

Streamlit app implementing the *BESS Sizing Software* specification (BESS_SIZING_2_0) on top of the
product team's container database (`data/BESS_DATABASE.xlsx`).

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## HTTP API and PostgreSQL

The Streamlit app remains usable on its own and continues to load its sizing catalog from
the Excel workbook. The REST API persists requests and results in the managed `bess` schema;
the database must already have the reviewed Alembic migrations applied. API requests do not
create or alter tables. Configure PostgreSQL in the `DATABASE_URL` environment variable,
then start:

```bash
uvicorn api:app --host 127.0.0.1 --port 8000
```

The API requires OIDC bearer-token authentication for project and sizing endpoints.
Configure `OIDC_ISSUER`, `OIDC_AUDIENCE`, and `OIDC_JWKS_URL` using values supplied by
your trusted identity provider. Tokens are verified with RS256, issuer and audience
validation, required `iss`/`sub`/`aud`/`exp`/`iat` claims, and a short clock-skew allowance.
The corresponding `(issuer, subject)` user must already exist and be active in
`bess.users`; user provisioning is an administrative operation and is not performed from
untrusted token claims. There is no development authentication bypass. Protected routes
fail closed if OIDC settings are missing or invalid.

Project access is controlled by organization membership plus project membership.
Organization owners and engineering members can create projects; the creator becomes the
project owner in the same transaction. Project owners/editors can update project metadata, submit project
simulations, save completed runs as immutable versions, and restore an earlier version as
a new version. Project owners alone can share projects and transfer ownership. Viewers can
read project results and version history but cannot modify or share projects.

Organization `owner`, `admin`, `manager`, and `member` roles have separate permissions.
Organization administrators manage memberships and can issue explicitly scoped, audited
manager grants; they do not automatically receive project-content access. Managers need
project membership or a grant for that specific project. A `management` grant permits
metadata/archival actions, while `content_read` also permits reading project results and
version snapshots. Grants can expire and are revoked when a manager loses that organization
role. Project metadata updates require an `expected_version`; version saves/restores require
an `expected_current_version`. Stale requests receive HTTP 409 rather than overwriting newer
changes. Audit events and project versions are append-only in PostgreSQL.

The policy matrix is enforced in `bess/authorization.py` and again at repository
boundaries:

| Scope | Role | Allowed operations |
|---|---|---|
| Organization | owner | Organization ownership/configuration, membership assignments, scoped manager grants, project creation |
| Organization | admin | Administrative settings, membership/role administration below owner/admin, scoped manager grants; no automatic project-content access |
| Organization | manager | No organization-wide access; project access requires membership or a scoped grant |
| Organization | member | Create projects; project operations come only from project membership |
| Project | owner | Read, edit, run/save/restore sizing, share, transfer ownership, archive |
| Project | editor | Read, edit, run/save/restore sizing |
| Project | viewer | Read project details, results and version history |
| Project grant | management | Manager reads metadata and archives the specifically granted project |
| Project grant | content_read | Manager reads the specifically granted project's results and version snapshots |

Projectless sizing requests remain supported for compatibility and are owned by the
authenticated creator. Idempotency keys are scoped to that user. The Streamlit app now
provides a backend project workspace when company OIDC is configured, while retaining the
existing local calculator as a separate workflow.

The API is not yet a complete production identity lifecycle: configure TLS at the trusted
deployment edge, provision and deactivate users through an administrative process, and
review provider-specific key-rotation and operational monitoring before exposure. It provides:

* `GET /health` - confirms PostgreSQL connectivity.
* `GET /api/v1/containers` - lists container options from the bundled workbook.
* `GET /api/v1/catalog/manifest` - returns the bundled workbook SHA-256 and available container metadata.
* `GET /api/v1/me` - returns the authenticated, provisioned profile and organization memberships.
* `POST /api/v1/projects` - creates a project for an organization owner or engineering member; the creator becomes project owner.
* `GET /api/v1/projects` and `GET /api/v1/projects/{project_id}` - list/retrieve projects where the caller has project membership or an explicit manager grant.
* `PATCH /api/v1/projects/{project_id}` - update project metadata as owner/editor; requires `expected_version`.
* `POST /api/v1/projects/{project_id}/members` - share with a same-organization member as editor/viewer.
* `PATCH /api/v1/projects/{project_id}/members/{member_user_id}/role` and `DELETE /api/v1/projects/{project_id}/members/{member_user_id}` - change/remove non-owner project shares as the project owner.
* `POST /api/v1/projects/{project_id}/ownership` - transfer ownership to an existing project member with a compare-and-swap version.
* `POST /api/v1/projects/{project_id}/versions` - save a completed project sizing run as the next immutable version.
* `GET /api/v1/projects/{project_id}/versions` and `/versions/{version_number}` - retrieve authorized version history and snapshots.
* `POST /api/v1/projects/{project_id}/versions/{version_number}/restore` - create a new version from historical inputs/results; does not overwrite history.
* `POST /api/v1/projects/{project_id}/manager-access` and `DELETE /manager-access/{grant_id}` - grant/revoke auditable project-scoped manager access.
* `POST /api/v1/projects/{project_id}/archive` - archive a project as its owner or a manager with management scope.
* `PATCH /api/v1/organizations/{organization_id}/members/{member_user_id}/role` - assign permitted organization roles; an owner transfer has a separate route.
* `POST /api/v1/organizations/{organization_id}/members` and `DELETE /api/v1/organizations/{organization_id}/members/{member_user_id}` - add/remove provisioned users with role boundaries; removal also revokes project shares/grants and requires project ownership transfers first.
* `POST /api/v1/organizations/{organization_id}/ownership` - transfer organization ownership.
* `POST /api/v1/sizing` - validates a sizing request, calculates results, and persists them. An optional `project_id` associates it with an authorized project.
* `GET /api/v1/sizing/{request_id}` - retrieves a saved run only for its creator, an authorized project member, or a manager with explicit content-read scope.
* `GET /api/v1/sizing/recent?limit=10` - lists recent runs visible to the authenticated user, including project members and explicitly granted content readers.

### Profile dashboard and employment verification

The Streamlit sidebar provides Home / Projects and Profile navigation. The profile page
shows only backend-provisioned identity fields, organization memberships, authorized
project summaries, recent sizing activity, account creation time, and active account
status. `/api/v1/me` returns the account ID, display name, email, creation timestamp,
active/inactive status, and the caller's organization roles. It does not expose a last
login timestamp.

Employee/contractor number, department, designation, employment type, and HR verification
status are not present in the current schema or connected to an authorized HR directory.
The profile therefore explicitly reports those fields as unavailable and displays an
employment-verification pending notice. It does not collect an employee number or claim
that a person has been HR-verified. Before enabling verification, the company must provide
an authenticated HR/identity provisioning source and define the verification workflow,
uniqueness rules, correction authority, audit requirements, and retention policy.

Current organization roles are displayed as organization owner, administrator, manager,
and member. They are not mapped to `system_admin`, `department_manager`, `employee`, or
`contractor`: employment type and equivalent global privileges cannot be inferred safely
from current roles. Role assignment remains server-side through existing organization
membership APIs. There is no user-directory or employee-verification administration UI;
an administrator must use the organization's approved provisioning process and existing
authorized backend operations.

The project workspace includes authorized-project search and active/archived filters,
project owner/status/current-version metadata, optimistic-version project archival, and
read-only version comparison from existing immutable snapshots. Archive and restore/save
operations are still authorized and concurrency-checked by FastAPI; the UI is not an
authorization boundary. No schema migration was needed for these additions.

### Streamlit OIDC and project workspace configuration

Streamlit uses its native OIDC `st.login` / `st.logout` implementation. The API client
forwards only `st.user.tokens["access"]` over a server-side HTTPS request; it never sends
the identity token or accepts user IDs and roles from browser input. Tokens are held in
the Streamlit session and are cleared with calculator/project widget state at logout or
after an API 401. API authorization remains enforced by FastAPI.

Company identity-provider registration is not included in this repository. An identity
administrator must provide/register the actual tenant, client, redirect URI, API scope,
and delegated consent. Do not copy placeholder values into deployment. Store the Streamlit
OIDC `client_secret` and `cookie_secret` in the hosting platform's server-side secret store
(`.streamlit/secrets.toml` only for local development; never commit it). The Streamlit
provider configuration must expose an access token and request the API's delegated scope:

```toml
[auth]
redirect_uri = "https://<approved-streamlit-host>/oauth2callback"
cookie_secret = "<random server-side secret>"
expose_tokens = ["access"]

[auth.microsoft]
client_id = "<registered-streamlit-client-id>"
client_secret = "<server-side client secret>"
server_metadata_url = "https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration"
client_kwargs = { "scope" = "openid profile email api://<registered-api-app-id>/<approved-delegated-scope>" }
```

Set `BESS_OIDC_PROVIDER=microsoft` (the default) and `BESS_API_BASE_URL` in the Streamlit
server environment. For local development, the redirect URI normally uses
`http://localhost:8501/oauth2callback`; local HTTP is permitted only for loopback API URLs.
The deployment redirect URI must exactly match the URI registered with the identity
provider. Streamlit authentication also requires `Authlib>=1.3.2`, included in
`requirements.txt`.

Configure the API independently with `DATABASE_URL`, `OIDC_ISSUER`, `OIDC_AUDIENCE`, and
`OIDC_JWKS_URL`. The API issuer must match the provider's metadata issuer; the access
token issued for the delegated scope must have this issuer, the API's configured audience,
and an RS256 signature discoverable from the configured JWKS URL. The identity
administrator must confirm the correct scope/audience and consent model. An ID token is
not a substitute for an API access token. Users must also be provisioned and active in
`bess.users` before project routes will authorize them. No shared service token or
development authentication bypass is provided.

The Home / Projects workspace lists authorized projects and recent runs, creates and opens
projects, submits backend sizing runs with **Run Calculation**, then saves a completed run
as an immutable project version only after the user selects **Save Version**. It also
loads version history, reviews snapshots, and restores history as a new version. Project owners can add, change, or remove shares
using the existing project-member API routes; target users must be known by their backend
user ID because no user-directory endpoint is provided. FastAPI remains the authority for
every membership change. Profile identity and organization memberships are read-only and
come from `GET /api/v1/me`. The project sizing form intentionally exposes only fields
represented by the backend request contract and uses the server's bundled catalog; the full local calculator still supports the existing workbook, advanced
settings, analyser, charts, and Excel report. Local uploads and local calculation results
are not silently saved as backend projects. API 409 responses require a refresh/retry;
they never overwrite a newer version.

`bess/api_client.py` is the only Streamlit-to-API communication boundary. It enforces
HTTPS outside loopback, uses request timeouts, sends correlation IDs and idempotency keys
for sizing, does not follow redirects, and maps backend failures to safe user messages.
Calls are server-side, so browser CORS is not needed. If deployment changes to direct
browser API calls, configure a narrow explicit CORS allow-list.

### Sizing input coverage

The API sizing request rejects unrecognized fields. The project workspace submits only the
supported subset; the separate local calculator retains all controls without claiming that
unsupported fields were used in a backend run.

| Streamlit control | API representation |
| --- | --- |
| Required project energy, required power, C-rate | `required_energy_mwh`, `required_power_mw`, `c_rate` |
| Duration from C-rate | Implicit in API calculation using C-rate |
| Manual charge/discharge duration and separate times | Not represented |
| Project years, operating temperature, availability, DOD | `project_years`, `operating_temp_c`, `availability_pct`, `dod_pct` |
| BOL/EOL energy, minimum EOL RTE, cycles | `bol_energy_mwh`, `eol_energy_mwh`, `min_rte_eol_pct`, `cycles_required` |
| Auxiliary consumption enabled | `aux_required` |
| Plant infrastructure power | Not represented |
| Constant mismatch loss | `mismatch_constant_pct` |
| Year-varying mismatch loss and yearly required energy/RTE/power tables | Not represented |
| Automatic nearest-C-rate selection or manual multi-container selection | Not represented; API requires one exact `container_id` |
| Minimum/maximum margin, fixed container count, extended margin search | `margin_min_pct`, `margin_max_pct`, `manual_containers`, `allow_extended_margin` |
| Main transformer source, AC/auxiliary overrides, auxiliary kW correction | Not represented |
| Extra auxiliary and standby toggles, values, source and booking side | Not represented |
| FAT/SAT toggle, duration and manually entered monthly degradation | Not represented |
| Augmentation toggle, event table and SOH age basis | Not represented |
| Workbook upload | Not represented; API uses its own bundled workbook |
| Result-label style, analyser controls, Claude narration, Excel report download | Presentation/analysis features; not represented by sizing request |

### Engineering dashboard

From the sidebar, open **Calculator** and select **Engineering Dashboard**. The dashboard
uses the local calculator's current `CaseResult`; when multiple containers are sized, use
the container selector to inspect each option. It includes summary KPIs, an energy-design
flow, year-wise discharge/charging/RTE/power charts, modelled SOH and capacity lifecycle
views, augmentation events, and requirement verification. Chart traces are built from the
engine's `dis`, `chg`, `perf`, and `verify` DataFrames and the result summary; missing or
non-finite optional values are shown as unavailable or as gaps.

The flow view is a calculated design representation, not a live SCADA display. The lifecycle
SOH is catalog/model-derived, not BMS-measured. The engine does not provide live telemetry,
rack temperatures, or individual converter loss measurements, so the dashboard does not
display those values. Dashboard charts render only while their tab is selected. If the
current inputs fail calculation, the dashboard shows the calculation errors instead of
reusing an older result.

Sizing requests continue to use the bundled Excel catalog. Persisted snapshots include the
resolved engine inputs, selected container data, workbook SHA-256, engine source hash,
application/runtime versions, calculation output, and engine warnings. An optional
`Idempotency-Key` request header prevents accidental duplicate submissions.
Successful sizing responses retain their existing fields and add `effective_inputs`,
`catalog_sha256` for the exact bundled workbook bytes used, and `warnings`. Every yearly
record includes an explicit `Year` value. These additions do not provide the full local
container/result objects required by the Streamlit analyser or Excel report.

Before submitting sizing, clients must retrieve `GET /api/v1/catalog/manifest` and send
its `catalog_sha256` value as the required `catalog_sha256` field in `POST /api/v1/sizing`.
The manifest response contains `schema_version` (`catalog-manifest-v1`), the lowercase
64-character SHA-256 digest, `container_ids`, and `containers` with the same metadata
fields as `GET /api/v1/containers`. The sizing endpoint reads one workbook snapshot,
compares the submitted digest to that snapshot, and calculates from the same parsed
snapshot. A stale or mismatched digest returns HTTP 409 with
`detail.code = "catalog_mismatch"`; retrieve the manifest and retry. Missing or malformed
digests return HTTP 422. The digest is a consistency precondition, not an authentication
mechanism. `BESSAPIClient.get_catalog_manifest()` validates the manifest contract; pass
the returned digest to `submit_sizing(..., catalog_sha256=...)`. Older sizing clients must
be updated because requests without the precondition are intentionally rejected.

Demonstrated calculation parity is limited to database-free tests of supported API inputs
against `size_container()` using identical workbook bytes. It does not demonstrate parity
for Streamlit controls absent from the API request schema, the analyser, or Excel report
generation. User-uploaded Streamlit workbooks remain local-only and are not sent to the
API or treated as equivalent to the server catalog. Company SSO remains deployment-gated
until the approved identity-provider settings and user provisioning are supplied.

Example request:

```json
{
  "container_id": "5.015MWh_0.25C",
  "catalog_sha256": "<64-character digest from the catalog manifest>",
  "required_energy_mwh": 100,
  "required_power_mw": 20,
  "project_years": 10
}
```

For client code, retrieve the manifest before submitting:

```python
manifest = client.get_catalog_manifest()
result = client.submit_sizing(
    request,
    catalog_sha256=manifest["catalog_sha256"],
)
```

Never commit `DATABASE_URL`; set it in the process environment or your deployment's secret
manager. Set the three OIDC values in the process environment or local `.env`; do not
invent provider values or commit production settings. Run the API tests with
`pytest -q tests/test_api.py`.

PostgreSQL integration tests are marked `postgres` and use only the explicitly
configured `BESS_SIMULATION_TEST_DATABASE_URL`; they never fall back to `DATABASE_URL`.
The target must be a dedicated database whose name includes `test`, must differ from the
application database, and must already be at Alembic revision `20261028_0004`. Tests do not
create schemas or apply migrations automatically. Run them with
`pytest -q -m postgres`; set `BESS_REQUIRE_POSTGRES_TESTS=1` to fail instead of reporting
the integration check as blocked when the dedicated target is unavailable. Catalog
PostgreSQL tests use the separate `BESS_CATALOG_TEST_DATABASE_URL` setting and can be
required with `BESS_REQUIRE_CATALOG_TESTS=1`.

### Phase 1 schema migration and recovery

Revision `20261028_0004` adds project version history, immutable audit events, manager
access grants, owner constraints, optimistic version pointers, and user-scoped
idempotency. It does not infer owners for existing projects: every existing project is
placed in `ownership_review` and is unavailable through project endpoints until an
authorized operator verifies its owner from company records, ensures that owner is an
active member of the owning organization, and explicitly marks the project active. New
projects are created active with their owner membership atomically. The migration stops
before applying changes if it finds multiple owners for an organization or project;
resolve those cases using verified records first, never by assigning an administrator.

Before upgrading a deployment, take and verify a restorable PostgreSQL backup, inspect the
target with `alembic current`, run the ownership preflight on that database, and review the
generated SQL/migration. Apply to a disposable or sanitized clone first. Only then schedule
the upgrade for the intended environment with `alembic upgrade head`; the application does
not migrate the database at startup. This revision intentionally refuses downgrade because
dropping version/audit history is destructive. Recovery requires restoring the pre-upgrade
backup using the organization's tested PostgreSQL recovery procedure; neither backup nor
restore has been exercised as part of this code change.

For example, the ownership preflight should review projects without exactly one owner and
organizations with more than one owner:

```sql
SELECT p.id, p.organization_id, count(pm.user_id) FILTER (WHERE pm.role = 'owner') AS owners
FROM bess.projects p
LEFT JOIN bess.project_members pm ON pm.project_id = p.id
GROUP BY p.id, p.organization_id
HAVING count(pm.user_id) FILTER (WHERE pm.role = 'owner') <> 1;

SELECT organization_id, count(*) AS owners
FROM bess.organization_members
WHERE role = 'owner'
GROUP BY organization_id
HAVING count(*) > 1;
```

## PostgreSQL catalog foundation

The SQLAlchemy connection layer reads `DATABASE_URL` from the process environment, or from
the local `.env` file when first used. A suitable URL form is shown in `.env.example`.
Percent-encode reserved characters in URL credentials; do not put real credentials in
`.env.example` or source control. The recommended deployment account is a least-privilege
application role, not the PostgreSQL superuser.

Foundational ORM models and Alembic configuration live in `bess/models/`, `bess/db_connection.py`
and `alembic/`. Revisions `20261012_0001` through `20261012_0003` create the foundational,
catalog-detail, and simulation-persistence structures; revision `20261028_0004` adds
multi-user project history and access controls in the `bess` schema. The versioned workbook catalog importer is in `bess/catalog_import.py`; it reads the
existing Excel parser output, validates the source SHA-256 and engineering data, persists
complete revisions transactionally, and never activates a revision during import.

Run a read-only catalog validation with:

```bash
python -c "from bess.catalog_import import dry_run_catalog; print(dry_run_catalog('data/BESS_DATABASE.xlsx'))"
```

Import and activation are separate service operations; activation must be explicitly requested
with `activate_catalog_revision`. The PostgreSQL catalog is not used by the sizing engine or
API; the Excel-based loading path remains in effect.

## Deploy on Streamlit Community Cloud

1. Push this folder to a GitHub repository (keep `data/BESS_DATABASE.xlsx` in the repo).
2. On <https://share.streamlit.io> choose **New app**, select the repository and set the main file to `app.py`.
3. Optional (Claude narration in the Analyser): in *Advanced settings -> Secrets* add
   `ANTHROPIC_API_KEY = "sk-ant-..."`. Without it the analyser uses its built-in rule-based explanations.

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | Streamlit UI: Inputs, Results, Analyser, Database tabs |
| `bess/database.py` | Reads the Excel database (finds each table by its label, so sheet layouts may differ) |
| `bess/engine.py` | All sizing formulas, year-wise calculation, verification, margin search |
| `bess/analyser.py` | What-if comparison, free-text parser, engineering explanations, optional Claude |
| `bess/report.py` | Excel export |
| `tests/test_bess.py` | `pytest -q` (formulas checked against hand calculations) |

To change the database, replace `data/BESS_DATABASE.xlsx` (same label-based sheet structure) or upload a new file from the sidebar. Settings accepts validated `.xlsx` workbooks up to 200 MB; CSV is not supported. An invalid upload leaves the last valid catalog active. Clear the uploader to return to the bundled six-option catalog.

## How the specification is implemented

* **Container selection (2, 2.1)** - nearest database C-rate; if several containers share it (e.g. 0.33C: 6.626 and 5.015 MWh) every one is sized and shown.
* **Initial containers (2.2-2.3)** - `ceil(Required energy x (1 + margin) / DC energy per container)`. The margin is searched from 14 % to 20 % (0.1 % steps); the lowest margin whose container count satisfies energy, power and RTE in every year is used. If the range is not enough the search is extended and flagged (can be switched off).
* **Discharge (3-6)** - `Installed x Availability x sqrt(DC RTE) x SOH x Calendar degradation x (1 - Mismatch) x DOD`, times the nine AC-chain efficiencies, minus `Aux load x Discharge time`.
* **Charge (7-10)** - DC-side value `/ (AC chain x DC RTE)`, plus `Aux load x Charge time`.
* **RTE (13.6-13.7)** - discharge / charge, excluding and including auxiliaries (excluding aux this equals chain^2 x DC RTE).
* **Verification (12, 15)** - BOL energy, EOL energy, EOL RTE and every year's required energy / RTE / power; green MATCHED / red NOT MATCHED (or TRUE/FALSE).
* **FAT/SAT (16)** - 1-6 months, monthly values from the database for the chosen temperature band (editable).
* **Augmentation (17-21)** - add or replace containers in chosen years; augmented energy per batch, AC MV energy including augmentation, auxiliary and plant-infrastructure power.
* **Analyser (11)** - change inputs in the form or type e.g. "reduce DOD to 90% and SOH by 2%"; before/after tables for BOL and EOL, year-wise comparison, containers needed to restore compliance, and an engineering explanation.

## Interpretations to confirm (the spec is ambiguous here)

1. **Calendar degradation** - the database lists monthly values (months 1-9) per temperature band and the SOH column excludes calendar degradation. The app applies `prod(1 - d_m)` for the FAT/SAT months as a one-off factor kept for the whole project. With FAT/SAT = No the factor is 100 %.
2. **Mismatch loss** is applied as `(1 - loss)` in the DC-side formula of every year (section 11 lists it; section 3.1.5 omits it - set it to 0 % to follow 3.1.5 literally). Availability is also applied to augmented containers.
3. **Required energy by year** - pre-filled linearly from BOL to EOL energy; "Required energy" drives the initial container count. The power check is `net energy at POI / discharge time >= required power`.
4. **Main power transformer** uses "Transformer efficiency - discharging" as written in section 4 (selectable: HV transformer row).
5. **Augmented batch ageing** - SOH / DC RTE by age since augmentation (alternative: same index as project year).
6. **Charging side of augmentation** - section 21 multiplies by the efficiencies; the app divides (as in section 10) so that charging energy rises, not falls, with losses. Plant-infrastructure energy uses charge time on the charge side.
7. **Cycles** - checked as `cycles/day x 365 x years >= required cycles`.
8. **Additional auxiliary / standby energy** (MWh per cycle) is booked on the discharge side by default (selectable).

## Database data-quality notes (shown in the app's Database tab)

* `7.044 MWh 0.25C` has no auxiliary loads - enter them under *Override database values*.
* `6.626 MWh` aux loads (2.25 and 4.06 "MW" per container) are larger than the container's rated power - they look like kW. A sidebar switch reads them as kW.
* DC DB efficiency (6.626 / 7.044 sheets) and main transformer / 11 kV cable (5.015 sheets) are not in the database; 100 % is used and flagged. All are editable.
* `5.015 MWh 0.17C` has SOH data to year 12 only and no temperature band above 50 °C.
