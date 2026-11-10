#!/usr/bin/env python3
"""
Plan vs Actual variance analysis for the FP&A demo agent.

Standard library only (no numpy / pandas).

Input: the JSON returned by the MCP tool get_plan_vs_actual, either
  - the full response: {"meta": {...}, "rows": [...]}
  - or just the list of rows: [...]
Each row needs: period, region, department, cost_center, account, plan, actual
(cost_center_name is optional).

Usage:
  python analyze_variance.py input.json
  cat input.json | python analyze_variance.py
  python analyze_variance.py input.json --top-n 5 --outlier-pct 15

Or import it:
  from analyze_variance import analyze
  result = analyze(tool_response_dict)

Output: one JSON object on stdout (see SKILL.md for the shape).
Variance convention: variance = actual - plan. For opex, positive = overspend.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict

REQUIRED_FIELDS = ("period", "region", "department", "cost_center", "account", "plan", "actual")

# Histogram bin edges for row-level variance %, with open-ended ends.
# A bin is [lower, upper). The first and last bins are open-ended.
DEFAULT_BIN_EDGES = [-20.0, -10.0, -5.0, 0.0, 5.0, 10.0, 20.0, 50.0]

# A driver line counts as "one_off" if a single period carries at least this
# share of its total variance.
ONE_OFF_SHARE = 0.8

# Lines whose total |variance %| is below this are labelled "within_tolerance"
# instead of getting a pattern, so noise is not narrated as a story.
TOLERANCE_PCT = 5.0

# A line is "run_rate" when every period deviates in the same direction by at
# least this much.
RUN_RATE_PCT = 8.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pct(variance: float, plan: float) -> float | None:
    if plan == 0:
        return None
    return round(variance / plan * 100, 1)


def _num(value) -> float | None:
    """Coerce a plan/actual value to float; None if it is not a finite number."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _money(x: float) -> int:
    return int(round(x))


def _fmt_money(x: float, currency: str) -> str:
    sign = "-" if x < 0 else "+"
    a = abs(x)
    if a >= 1_000_000:
        s = f"{a / 1_000_000:.2f}M"
    elif a >= 1_000:
        s = f"{a / 1_000:.0f}K"
    else:
        s = f"{a:.0f}"
    return f"{sign}{s} {currency}"


def _fmt_pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p:+.1f}%"


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------

def _extract_rows(data) -> tuple[list[dict], dict]:
    if isinstance(data, list):
        return data, {}
    if isinstance(data, dict):
        if "error" in data and "rows" not in data:
            raise ValueError(f"MCP tool returned an error: {data['error']}")
        rows = data.get("rows")
        if not isinstance(rows, list):
            raise ValueError("Input object has no 'rows' list.")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        return rows, meta
    raise ValueError("Input must be a JSON object with 'rows' or a JSON list of rows.")


def _clean_rows(raw_rows: list) -> tuple[list[dict], list[dict]]:
    clean, rejected = [], []
    for i, r in enumerate(raw_rows):
        if not isinstance(r, dict):
            rejected.append({"index": i, "reason": "row is not an object"})
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in r]
        if missing:
            rejected.append({"index": i, "reason": f"missing fields: {', '.join(missing)}"})
            continue
        plan, actual = _num(r["plan"]), _num(r["actual"])
        if plan is None or actual is None:
            rejected.append({"index": i, "reason": "plan/actual not numeric"})
            continue
        variance = actual - plan
        clean.append(
            {
                "period": str(r["period"]),
                "region": str(r["region"]),
                "department": str(r["department"]),
                "cost_center": str(r["cost_center"]),
                "cost_center_name": str(r.get("cost_center_name") or r["cost_center"]),
                "account": str(r["account"]),
                "plan": plan,
                "actual": actual,
                "variance": variance,
                "variance_pct": _pct(variance, plan),
            }
        )
    return clean, rejected


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------

def _totals(rows: list[dict]) -> dict:
    plan = sum(r["plan"] for r in rows)
    actual = sum(r["actual"] for r in rows)
    var = actual - plan
    return {
        "plan": _money(plan),
        "actual": _money(actual),
        "variance": _money(var),
        "variance_pct": _pct(var, plan),
    }


def _breakdown(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    out = [{key: k, **_totals(g)} for k, g in groups.items()]
    if key == "period":
        out.sort(key=lambda x: x["period"])
    else:
        out.sort(key=lambda x: -x["variance"])
    return out


def _classify(by_period: dict[str, float], line_pct: float | None, pct_by_period: dict[str, float | None]) -> str:
    """Label the shape of a driver line's variance across periods."""
    if line_pct is not None and abs(line_pct) < TOLERANCE_PCT:
        return "within_tolerance"
    total = sum(by_period.values())
    if len(by_period) > 1 and total != 0:
        top = max(by_period.values(), key=lambda v: v * (1 if total > 0 else -1))
        if top / total >= ONE_OFF_SHARE:
            return "one_off"
    pcts = [p for p in pct_by_period.values() if p is not None]
    if len(pcts) > 1 and (all(p >= RUN_RATE_PCT for p in pcts) or all(p <= -RUN_RATE_PCT for p in pcts)):
        return "run_rate"
    return "mixed" if len(by_period) > 1 else "single_period"


def _drivers(rows: list[dict], top_n: int, net_variance: float) -> tuple[list[dict], dict]:
    """Rank cost_center x account lines (summed across periods) by variance."""
    lines: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        lines[(r["cost_center"], r["account"])].append(r)

    ranked = []
    for (cc, acct), g in lines.items():
        t = _totals(g)
        by_period = defaultdict(float)
        plan_by_period = defaultdict(float)
        for r in g:
            by_period[r["period"]] += r["variance"]
            plan_by_period[r["period"]] += r["plan"]
        pct_by_period = {p: _pct(v, plan_by_period[p]) for p, v in by_period.items()}
        peak_period = max(by_period, key=lambda p: abs(by_period[p]))
        ranked.append(
            {
                "cost_center": cc,
                "cost_center_name": g[0]["cost_center_name"],
                "region": g[0]["region"],
                "department": g[0]["department"],
                "account": acct,
                **t,
                "pattern": _classify(dict(by_period), t["variance_pct"], pct_by_period),
                "peak_period": peak_period,
                "share_of_net_variance_pct": _pct(t["variance"], net_variance) if net_variance else None,
            }
        )

    over = sorted((d for d in ranked if d["variance"] > 0), key=lambda d: -d["variance"])
    under = sorted((d for d in ranked if d["variance"] < 0), key=lambda d: d["variance"])

    gross_over = sum(d["variance"] for d in over)
    gross_under = sum(d["variance"] for d in under)
    top_over_sum = sum(d["variance"] for d in over[:top_n])
    concentration = {
        "lines_total": len(ranked),
        "lines_over_plan": len(over),
        "lines_under_plan": len(under),
        "gross_overspend": _money(gross_over),
        "gross_underspend": _money(gross_under),
        f"top_{top_n}_share_of_gross_overspend_pct": round(top_over_sum / gross_over * 100, 1) if gross_over else None,
    }
    return {"overspend": over[:top_n], "underspend": under[:top_n]}, concentration


def _histogram(rows: list[dict], edges: list[float]) -> dict:
    edges = sorted(edges)
    bins = []
    lowers = [None] + edges
    uppers = edges + [None]
    for lo, hi in zip(lowers, uppers):
        if lo is None:
            label = f"< {hi:g}%"
        elif hi is None:
            label = f">= {lo:g}%"
        else:
            label = f"{lo:g}% to {hi:g}%"
        bins.append({"label": label, "lower": lo, "upper": hi, "count": 0, "variance_sum": 0.0})

    excluded = 0
    for r in rows:
        p = r["variance_pct"]
        if p is None:
            excluded += 1
            continue
        for b in bins:
            if (b["lower"] is None or p >= b["lower"]) and (b["upper"] is None or p < b["upper"]):
                b["count"] += 1
                b["variance_sum"] += r["variance"]
                break

    for b in bins:
        b["variance_sum"] = _money(b["variance_sum"])

    return {
        "metric": "variance_pct",
        "unit": "percent of plan",
        "granularity": "one observation per row (cost center x account x period)",
        "bin_rule": "lower <= x < upper; first and last bins are open-ended",
        "bins": bins,
        "observations": sum(b["count"] for b in bins),
        "excluded_zero_plan": excluded,
        "ascii": _ascii_histogram(bins),
    }


def _ascii_histogram(bins: list[dict], width: int = 30) -> str:
    peak = max((b["count"] for b in bins), default=0) or 1
    label_w = max(len(b["label"]) for b in bins)
    lines = []
    for b in bins:
        bar = "#" * max(1 if b["count"] else 0, round(b["count"] / peak * width))
        lines.append(f"{b['label'].rjust(label_w)} | {bar} {b['count']}")
    return "\n".join(lines)


def _outliers(rows: list[dict], outlier_pct: float, limit: int = 20) -> dict:
    hits = [r for r in rows if r["variance_pct"] is not None and abs(r["variance_pct"]) >= outlier_pct]
    hits.sort(key=lambda r: -abs(r["variance"]))
    return {
        "threshold_pct": outlier_pct,
        "count": len(hits),
        "rows": [
            {
                "period": r["period"],
                "cost_center": r["cost_center"],
                "cost_center_name": r["cost_center_name"],
                "account": r["account"],
                "plan": _money(r["plan"]),
                "actual": _money(r["actual"]),
                "variance": _money(r["variance"]),
                "variance_pct": r["variance_pct"],
            }
            for r in hits[:limit]
        ],
        "truncated": len(hits) > limit,
    }


def _headline_facts(totals, by_region, drivers, concentration, top_n, currency) -> list[str]:
    """Pre-computed sentences the agent can quote, so it never does arithmetic itself."""
    facts = [
        f"Total: actual {_fmt_money(totals['actual'], currency).lstrip('+')} vs plan "
        f"{_fmt_money(totals['plan'], currency).lstrip('+')}, variance "
        f"{_fmt_money(totals['variance'], currency)} ({_fmt_pct(totals['variance_pct'])})."
    ]
    if len(by_region) > 1:
        facts.append(
            "By region: "
            + "; ".join(f"{g['region']} {_fmt_pct(g['variance_pct'])}" for g in by_region)
            + "."
        )
    share = concentration.get(f"top_{top_n}_share_of_gross_overspend_pct")
    over = drivers["overspend"]
    if over and share is not None:
        facts.append(
            f"The top {len(over)} overspending lines (of {concentration['lines_over_plan']} over plan) "
            f"account for {share:.0f}% of gross overspend."
        )
    for d in over[:3]:
        facts.append(
            f"{d['cost_center_name']} ({d['cost_center']}) / {d['account']}: "
            f"{_fmt_money(d['variance'], currency)} ({_fmt_pct(d['variance_pct'])}), "
            f"pattern {d['pattern']}, peak {d['peak_period']}."
        )
    under = drivers["underspend"]
    if under:
        d = under[0]
        facts.append(
            f"Largest underspend: {d['cost_center_name']} ({d['cost_center']}) / {d['account']}: "
            f"{_fmt_money(d['variance'], currency)} ({_fmt_pct(d['variance_pct'])}), pattern {d['pattern']}."
        )
    return facts


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def analyze(data, top_n: int = 5, outlier_pct: float = 15.0, bin_edges: list[float] | None = None) -> dict:
    raw_rows, meta = _extract_rows(data)
    rows, rejected = _clean_rows(raw_rows)
    if not rows:
        raise ValueError("No usable rows to analyse." + (f" {len(rejected)} rows rejected." if rejected else ""))

    currency = str(meta.get("currency") or "USD")
    totals = _totals(rows)
    by_region = _breakdown(rows, "region")
    drivers, concentration = _drivers(rows, top_n, totals["variance"])

    return {
        "schema_version": "1.0",
        "context": {
            "source": meta.get("source"),
            "fiscal_period": meta.get("fiscal_period"),
            "currency": currency,
            "versions": meta.get("versions"),
            "filters": meta.get("filters"),
            "periods": sorted({r["period"] for r in rows}),
            "rows_analysed": len(rows),
            "rows_rejected": len(rejected),
            "input_truncated": bool(meta.get("truncated", False)),
            "variance_convention": "variance = actual - plan; positive = over plan (overspend for opex)",
        },
        "totals": totals,
        "breakdowns": {
            "by_region": by_region,
            "by_department": _breakdown(rows, "department"),
            "by_account": _breakdown(rows, "account"),
            "by_period": _breakdown(rows, "period"),
        },
        "drivers": drivers,
        "concentration": concentration,
        "outliers": _outliers(rows, outlier_pct),
        "histogram": _histogram(rows, bin_edges or DEFAULT_BIN_EDGES),
        "headline_facts": _headline_facts(totals, by_region, drivers, concentration, top_n, currency),
        "rejected_rows": rejected[:10],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Plan vs Actual variance analysis (stdlib only).")
    ap.add_argument("input", nargs="?", help="JSON file; omit to read stdin")
    ap.add_argument("--top-n", type=int, default=5, help="drivers to return per direction (default 5)")
    ap.add_argument("--outlier-pct", type=float, default=15.0, help="|variance %%| threshold for outliers (default 15)")
    ap.add_argument("--bins", help="comma-separated histogram edges, e.g. -20,-10,-5,0,5,10,20,50")
    ap.add_argument("--pretty", action="store_true", help="indent the output JSON")
    args = ap.parse_args(argv)

    try:
        if args.input:
            with open(args.input, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)
        edges = [float(x) for x in args.bins.split(",")] if args.bins else None
        if args.top_n < 1:
            raise ValueError("--top-n must be at least 1")
        result = analyze(data, top_n=args.top_n, outlier_pct=args.outlier_pct, bin_edges=edges)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1

    print(json.dumps(result, indent=2 if args.pretty else None, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
