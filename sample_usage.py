"""Sample monthly MSU data per team. Replace monthly_msu() with a query against your
real capacity data (e.g. an SMF/RMF-derived table keyed by service or report class)."""
import random

from cycles import add_months

FIRST_MONTH = (2024, 10)
N_MONTHS = 24  # Oct 2024 – Sep 2026

# base MSU, monthly growth, seasonal peaks {month: uplift}
PROFILES = {
    "PAY": {"base": 420, "growth": 0.010, "peaks": {11: 0.06, 12: 0.18}},
    "CLM": {"base": 260, "growth": 0.004, "peaks": {1: 0.10, 3: 0.05}},
    "BIL": {"base": 185, "growth": -0.008, "peaks": {11: 0.12, 1: 0.05}},
    "CRD": {"base": 350, "growth": 0.014, "peaks": {11: 0.10, 12: 0.25}},
    "GLD": {"base": 140, "growth": 0.002, "peaks": {12: 0.08, 1: 0.30}},
    "PRL": {"base": 90, "growth": 0.001, "peaks": {12: 0.10}},
}


def monthly_msu(team_id):
    """{(year, month): average MSU}, oldest first."""
    p = PROFILES[team_id]
    rng = random.Random(team_id)  # deterministic per team
    out = {}
    for i in range(N_MONTHS):
        ym = add_months(FIRST_MONTH, i)
        value = p["base"] * (1 + p["growth"]) ** i * (1 + p["peaks"].get(ym[1], 0)) * (1 + rng.gauss(0, 0.025))
        out[ym] = round(value, 1)
    return out
