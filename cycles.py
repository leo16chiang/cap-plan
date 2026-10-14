"""Review cycles. Add a new entry each spring/fall; the last one is the open cycle.

Each cycle projects four quarters, measured against the average of the three months
before the first projected quarter (the baseline)."""
import calendar


def add_months(ym, n):
    year, month = ym
    idx = year * 12 + (month - 1) + n
    return (idx // 12, idx % 12 + 1)


def month_label(ym):
    return f"{calendar.month_abbr[ym[1]]} {str(ym[0])[2:]}"


def _cycle(cycle_id, label, first_month, quarter_names):
    return {
        "id": cycle_id,
        "label": label,
        "quarters": [
            {"label": name, "months": [add_months(first_month, 3 * i + k) for k in range(3)]}
            for i, name in enumerate(quarter_names)
        ],
        "baseline_months": [add_months(first_month, k - 3) for k in range(3)],
    }


CYCLES = [
    _cycle("2025-H2", "Fall 2025 review", (2025, 10), ["Q4 2025", "Q1 2026", "Q2 2026", "Q3 2026"]),
    _cycle("2026-H1", "Spring 2026 review", (2026, 4), ["Q2 2026", "Q3 2026", "Q4 2026", "Q1 2027"]),
    _cycle("2026-H2", "Fall 2026 review", (2026, 10), ["Q4 2026", "Q1 2027", "Q2 2027", "Q3 2027"]),
]
BY_ID = {c["id"]: c for c in CYCLES}
CURRENT = CYCLES[-1]


def previous(cycle_id):
    idx = [c["id"] for c in CYCLES].index(cycle_id)
    return CYCLES[idx - 1] if idx > 0 else None


def quarter_labels(cycle_id):
    return [q["label"] for q in BY_ID[cycle_id]["quarters"]]
