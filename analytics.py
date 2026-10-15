"""Turns monthly usage plus submitted answers into chart series and projection-vs-actual numbers."""
from statistics import mean

from cycles import CURRENT, add_months, month_label
from sample_usage import monthly_msu

# How a cycle's baseline (starting point) is worked out from the months before its first quarter.
# Capacity Planning picks one per cycle and per team on the admin Plan settings page.
BASELINE_METHODS = {
    "last_month": {"label": "Last month", "months": 1, "agg": mean},
    "avg_3m": {"label": "3-month average", "months": 3, "agg": mean},
    "avg_6m": {"label": "6-month average", "months": 6, "agg": mean},
    "peak_12m": {"label": "12-month peak", "months": 12, "agg": max},
    "manual": {"label": "Manual value"},
}
COMPUTED_METHODS = [m for m, spec in BASELINE_METHODS.items() if spec.get("months")]


def _num(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def baseline_months(cycle, method):
    n = BASELINE_METHODS[method].get("months") or 1
    return [add_months(cycle["start_month"], -k) for k in range(n, 0, -1)]


def computed_baseline(msu, cycle, method):
    """Baseline MSU for a computed method, or None if any month in its window has no data."""
    values = [msu.get(m) for m in baseline_months(cycle, method)]
    if any(v is None for v in values):
        return None
    return round(BASELINE_METHODS[method]["agg"](values), 1)


def baseline_candidates(msu, cycle):
    """{method: MSU} for every computed method, so admins can compare starting points side by side."""
    return {m: computed_baseline(msu, cycle, m) for m in COMPUTED_METHODS}


def baseline_basis(cycle, method):
    """Short description of where a baseline came from, e.g. 'Last month (Sep 26)'."""
    if method == "manual":
        return "Set by Capacity Planning"
    months = baseline_months(cycle, method)
    window = month_label(months[0]) if len(months) == 1 else f"{month_label(months[0])}–{month_label(months[-1])}"
    return f"{BASELINE_METHODS[method]['label']} ({window})"


def msu_for(base, pct):
    return round(base * (1 + pct / 100), 1) if base is not None and pct is not None else None


def projected_by_month(growth, cycle, base):
    """A cycle's quarterly % answers as {month: projected MSU}."""
    out = {}
    if base is None:
        return out
    for quarter, raw in zip(cycle["quarters"], growth or []):
        pct = _num(raw)
        if pct is None:
            continue
        for m in quarter["months"]:
            out[m] = msu_for(base, pct)
    return out


def outcomes(team_id, cycle, answers, base, adjusted=None):
    """Projected vs actual % change per quarter for one cycle's submission.

    adjusted: Capacity Planning's per-quarter planning % (None where the team's number stands).
    """
    msu = monthly_msu(team_id)
    growth = (answers or {}).get("growth") or [""] * len(cycle["quarters"])
    adjusted = adjusted or [None] * len(cycle["quarters"])
    rows = []
    for quarter, raw, adj in zip(cycle["quarters"], growth, adjusted):
        values = [msu.get(m) for m in quarter["months"]]
        known = [v for v in values if v is not None]
        actual_pct = round((mean(values) / base - 1) * 100, 1) if base and len(known) == len(values) else None
        projected_pct = _num(raw)
        rows.append({
            "label": quarter["label"],
            "projected_pct": projected_pct,
            "adjusted_pct": adj,
            "actual_pct": actual_pct,
            "diff": round(actual_pct - projected_pct, 1) if actual_pct is not None and projected_pct is not None else None,
            "status": "complete" if actual_pct is not None else ("in progress" if known else "future"),
        })
    return {"baseline": base, "quarters": rows, "has_adjusted": any(a is not None for a in adjusted)}


def _month_index(ym):
    return ym[0] * 12 + ym[1] - 1


def statistical_projection(msu, cycle, base):
    """A purely mathematical projection from actual usage, as a reference point for the team.

    Fits a straight-line trend to every month of history, then multiplies by a seasonal index per
    calendar month (the average of actual / trend for that month, e.g. December runs 15% above trend).
    Returns {"quarters": [{"label", "msu", "pct"}], "by_month": {month: MSU}, "n_months": int}.
    """
    points = [(_month_index(m), v) for m, v in msu.items() if v is not None]
    if len(points) < 12:
        return None  # not enough history to separate trend from seasonality
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    slope = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    trend = lambda x: my + slope * (x - mx)  # noqa: E731

    ratios = {}
    for x, y in points:
        ratios.setdefault(x % 12, []).append(y / trend(x))
    season = {k: mean(v) for k, v in ratios.items()}

    by_month, quarters = {}, []
    for quarter in cycle["quarters"]:
        values = []
        for m in quarter["months"]:
            x = _month_index(m)
            by_month[m] = round(max(trend(x), 0) * season.get(x % 12, 1), 1)
            values.append(by_month[m])
        avg = round(mean(values), 1)
        quarters.append({"label": quarter["label"], "msu": avg,
                         "pct": round((avg / base - 1) * 100, 1) if base else None})
    return {"quarters": quarters, "by_month": by_month, "n_months": n}


def team_overview(team_id, base, prev_cycle=None, prev_answers=None, prev_base=None, adjusted=None):
    """Everything the team page's stat cards and chart need.

    base: the current cycle's baseline, {"value", "method", "basis"} (see app.team_baseline).
    prev_base: the baseline last cycle's projection was made against.
    adjusted: Capacity Planning's per-quarter planning %, None where the team's number stands.
    """
    msu = monthly_msu(team_id)
    history = list(msu)
    end = CURRENT["quarters"][-1]["months"][-1]
    months = list(history)
    while months[-1] != end:
        months.append(add_months(months[-1], 1))

    prev_proj = {}
    if prev_cycle and prev_answers:
        prev_proj = projected_by_month(prev_answers.get("growth"), prev_cycle, prev_base)

    lookback = None
    elapsed = [m for m in prev_proj if m in msu]
    if elapsed:
        proj_avg = mean(prev_proj[m] for m in elapsed)
        act_avg = mean(msu[m] for m in elapsed)
        lookback = {
            "cycle_label": prev_cycle["label"],
            "window": f"{month_label(elapsed[0])}–{month_label(elapsed[-1])}",
            "projected": round(proj_avg, 1),
            "actual": round(act_avg, 1),
            "variance_pct": round((act_avg / proj_avg - 1) * 100, 1),
        }

    adj_proj = projected_by_month(adjusted, CURRENT, base["value"]) if adjusted and any(
        a is not None for a in adjusted) else {}

    stats = statistical_projection(msu, CURRENT, base["value"])

    last12 = history[-12:]
    peak_month = max(last12, key=msu.get)
    recent6 = mean(msu[m] for m in history[-6:])
    prior6 = mean(msu[m] for m in history[-18:-12])

    return {
        "labels": [month_label(m) for m in months],
        "n_hist": len(history),
        "actual": [msu.get(m) for m in months],
        "last_projection": [prev_proj.get(m) for m in months],
        "adjusted_projection": [adj_proj.get(m) for m in months] if adj_proj else None,
        "statistical": stats and stats["quarters"],
        "statistical_months": stats and stats["n_months"],
        "statistical_projection": [stats["by_month"].get(m) for m in months] if stats else None,
        "prev_label": prev_cycle["label"] if prev_proj else None,
        "baseline": base["value"],
        "baseline_basis": base["basis"],
        "baseline_n": BASELINE_METHODS.get(base.get("method"), {}).get("months") or 1,
        "peak": msu[peak_month],
        "peak_label": month_label(peak_month),
        "yoy_pct": round((recent6 / prior6 - 1) * 100, 1),
        "lookback": lookback,
    }
