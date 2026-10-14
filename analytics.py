"""Turns monthly usage plus submitted answers into chart series and projection-vs-actual numbers."""
from statistics import mean

from cycles import CURRENT, add_months, month_label
from sample_usage import monthly_msu


def _num(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def baseline(msu, cycle):
    values = [msu.get(m) for m in cycle["baseline_months"]]
    return round(mean(values), 1) if all(v is not None for v in values) else None


def projected_by_month(answers, cycle, base):
    """A cycle's quarterly % answers as {month: projected MSU}."""
    out = {}
    if base is None:
        return out
    for quarter, raw in zip(cycle["quarters"], (answers or {}).get("growth") or []):
        pct = _num(raw)
        if pct is None:
            continue
        for m in quarter["months"]:
            out[m] = round(base * (1 + pct / 100), 1)
    return out


def outcomes(team_id, cycle, answers):
    """Projected vs actual % change per quarter for one cycle's submission."""
    msu = monthly_msu(team_id)
    base = baseline(msu, cycle)
    growth = (answers or {}).get("growth") or [""] * len(cycle["quarters"])
    rows = []
    for quarter, raw in zip(cycle["quarters"], growth):
        values = [msu.get(m) for m in quarter["months"]]
        known = [v for v in values if v is not None]
        actual_pct = round((mean(values) / base - 1) * 100, 1) if base and len(known) == len(values) else None
        projected_pct = _num(raw)
        rows.append({
            "label": quarter["label"],
            "projected_pct": projected_pct,
            "actual_pct": actual_pct,
            "diff": round(actual_pct - projected_pct, 1) if actual_pct is not None and projected_pct is not None else None,
            "status": "complete" if actual_pct is not None else ("in progress" if known else "future"),
        })
    return {"baseline": base, "quarters": rows}


def team_overview(team_id, prev_cycle=None, prev_answers=None):
    """Everything the team page's stat cards and chart need."""
    msu = monthly_msu(team_id)
    history = list(msu)
    end = CURRENT["quarters"][-1]["months"][-1]
    months = list(history)
    while months[-1] != end:
        months.append(add_months(months[-1], 1))

    prev_proj = {}
    if prev_cycle and prev_answers:
        prev_proj = projected_by_month(prev_answers, prev_cycle, baseline(msu, prev_cycle))

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

    last12 = history[-12:]
    peak_month = max(last12, key=msu.get)
    recent6 = mean(msu[m] for m in history[-6:])
    prior6 = mean(msu[m] for m in history[-18:-12])

    return {
        "labels": [month_label(m) for m in months],
        "n_hist": len(history),
        "actual": [msu.get(m) for m in months],
        "last_projection": [prev_proj.get(m) for m in months],
        "prev_label": prev_cycle["label"] if prev_proj else None,
        "baseline": baseline(msu, CURRENT),
        "baseline_window": f"{month_label(CURRENT['baseline_months'][0])}–{month_label(CURRENT['baseline_months'][-1])}",
        "peak": msu[peak_month],
        "peak_label": month_label(peak_month),
        "yoy_pct": round((recent6 / prior6 - 1) * 100, 1),
        "lookback": lookback,
    }
