# BESS Sizing & Performance Analysis Software

**Size. Simulate. Validate. Analyze.**

A Python-based Battery Energy Storage System (BESS) sizing application that evaluates battery-container requirements, verifies energy and power performance, and analyzes operating scenarios over a project's lifetime.

<p align="center">
  <a href="https://bess-sizing-web.streamlit.app/">
    <img src="https://static.streamlit.io/badges/streamlit_badge_black_white.svg" alt="Open Live App">
  </a>
</p>

<p align="center">
  <a href="https://bess-sizing-web.streamlit.app/"><strong>Launch Live Application</strong></a>
  &nbsp; | &nbsp;
  <a href="https://github.com/venkat24-dev/bess-deomonstartion-repo"><strong>View Source Code</strong></a>
</p>

---

## Links

- **Live Application:** https://bess-sizing-web.streamlit.app/
- **GitHub Repository:** https://github.com/venkat24-dev/bess-deomonstartion-repo

## Overview

This application helps users assess BESS configurations using battery-container specifications, project energy and power requirements, operating assumptions, efficiency parameters, and battery degradation.

It combines engineering calculations with interactive analysis to compare sizing scenarios and evaluate whether a proposed configuration satisfies supported project requirements throughout its operating lifetime.

## Key Features

- **Battery Sizing:** Estimate the number of battery containers required based on energy, power, and C-rate constraints.
- **BOL and EOL Verification:** Evaluate beginning-of-life (BOL) and end-of-life (EOL) energy performance.
- **Lifetime Analysis:** Assess degradation and verify supported energy, power, and round-trip efficiency (RTE) requirements across project years.
- **What-If Analysis:** Explore how changes in engineering assumptions affect sizing and performance.
- **Operating Parameters:** Configure supported parameters such as availability, depth of discharge (DOD), mismatch losses, and auxiliary consumption.
- **Augmentation Analysis:** Evaluate supported battery augmentation scenarios over the project lifetime.
- **Interactive Results:** Review calculation outputs, comparison tables, and engineering explanations.
- **Excel Reporting:** Export available calculation results for further analysis and documentation.
- **Battery Catalog:** Use an Excel-based battery-container catalog to support sizing calculations.

## Live Demo

**Try the application:** https://bess-sizing-web.streamlit.app/

1. Open the live application.
2. Configure project energy and power requirements.
3. Select the available battery container and operating parameters.
4. Review sizing results and lifetime performance verification.
5. Explore what-if scenarios and download the available report.

## Technology Stack

- **Programming:** Python
- **Application and UI:** Streamlit
- **Data Processing:** Pandas, NumPy
- **Data Source and Reporting:** Excel
- **Database Foundation:** PostgreSQL, SQLAlchemy
- **API Foundation:** FastAPI
- **Testing:** Pytest
- **Database Migrations:** Alembic

**Architecture note:** The deployed Streamlit application currently runs as a standalone, Excel-backed sizing calculator. The repository also contains separate FastAPI and PostgreSQL components, but the deployed UI is not yet integrated with that backend.

## Project Structure

| Component | Purpose |
|---|---|
| `app.py` | Streamlit application and user interface |
| `bess/engine.py` | Sizing calculations, yearly calculations, and performance verification |
| `bess/database.py` | Loads the Excel battery catalog |
| `bess/analyser.py` | What-if analysis and engineering explanations |
| `bess/report.py` | Excel report generation |
| `api.py` | Separate REST API |
| `bess/models/` | Database models |
| `bess/db_connection.py` | Database connection foundation |
| `alembic/` | Database migration configuration |
| `tests/` | Automated tests |
| `data/BESS_DATABASE.xlsx` | Battery-container catalog |

## Engineering and Validation

The sizing workflow evaluates container requirements and checks supported project constraints over the operating lifetime.

Depending on the selected configuration and available catalog data, the calculations account for:

- Battery-container energy and power specifications
- Availability and depth of discharge
- Round-trip efficiency and conversion losses
- Battery state of health and degradation
- Auxiliary consumption and mismatch losses
- Year-by-year performance requirements
- Supported augmentation scenarios

The repository includes automated tests for sizing calculations. API and PostgreSQL integration tests have separate configuration requirements.

**Important limitation:** The API does not support every input and analysis option available in the Streamlit interface. Its calculation coverage is limited to the inputs represented in the API request contract.

## Local Setup

### Prerequisites

- Python installed
- Git installed
- The project repository and Excel battery catalog available locally

### 1. Clone the repository

```bash
git clone https://github.com/venkat24-dev/bess-deomonstartion-repo.git
cd bess-deomonstartion-repo
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the Streamlit application

```bash
streamlit run app.py
```

The Excel battery catalog must be available at:

```text
data/BESS_DATABASE.xlsx
```

## API and PostgreSQL Foundation

The repository contains a separate REST API and PostgreSQL persistence foundation for project management and sizing requests.

The API uses database configuration through `DATABASE_URL` and requires configured OpenID Connect (OIDC) bearer-token authentication for protected project and sizing endpoints. Database migrations must be applied before using the database-backed functionality.

To start the API locally after configuring the required environment and database:

```bash
uvicorn api:app --host 127.0.0.1 --port 8000
```

The API includes endpoints for:

- Health and database connectivity checks
- Retrieving battery-container options and catalog metadata
- Creating and retrieving projects
- Managing authorized project memberships
- Submitting sizing requests and retrieving saved results

**Integration status:** The deployed Streamlit interface does not currently call the FastAPI backend. PostgreSQL-backed catalog functionality is also separate from the Excel catalog currently used by the sizing workflow.

Keep database credentials, authentication settings, and API keys out of source control. Use environment variables or an appropriate secrets manager for sensitive configuration.

## Testing

Run the general test suite from the repository root:

```bash
pytest -q
```

API tests can be run separately:

```bash
pytest -q tests/test_api.py
```

PostgreSQL integration tests require a dedicated test database and the appropriate environment configuration. Consult the repository's testing instructions before running them.

## Future Improvements

Potential next steps include:

- Integrating the Streamlit interface with the authenticated API
- Connecting the sizing workflow to a versioned PostgreSQL catalog
- Expanding automated tests for engineering calculations and edge cases
- Improving deployment monitoring and operational documentation
- Extending API input coverage to match additional supported UI controls

These are potential improvements, not claims of already completed functionality.

---


