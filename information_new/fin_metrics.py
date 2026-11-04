"""
fin_metrics.py - financial metrics from an APPROVED dataset (output of financial_gate.py).

For every metric it gives the three financial assessments that need no outside knowledge of the company:
  absolute   latest value against a generic rule-of-thumb threshold (NOT an industry benchmark)
  historical direction over the available periods (improving / deteriorating / stable)
  relative   against peer values, only if a peer benchmark file is supplied
Nothing is invented: a metric whose inputs are missing is reported as unavailable, and metrics that use a
PARTIAL input (for example total debt without the current portion) are flagged as lower bounds.

Peer benchmark file (optional JSON), either of:
  {"current_ratio": 1.4, "operating_margin": 0.08, ...}                 one value per metric
  {"peers": {"Peer A": {"operating_margin": 0.07, ...}, "Peer B": {...}}} median across peers is used
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median

# metric -> (needs, function(values dict)->number|None, weak, strong, higher_is_better, definition)
def _div(a, b):
    return None if a is None or b in (None, 0) else a / b

METRIC_DEFS = {
    "current_ratio": (["current_assets", "current_liabilities"], lambda v: _div(v["current_assets"], v["current_liabilities"]), 1.0, 1.5, True, "current assets / current liabilities"),
    "quick_ratio": (["current_assets", "inventory", "current_liabilities"], lambda v: _div(v["current_assets"] - v["inventory"], v["current_liabilities"]), 0.7, 1.0, True, "(current assets - inventory) / current liabilities"),
    "cash_ratio": (["cash", "current_liabilities"], lambda v: _div(v["cash"] + (v.get("short_investments") or 0), v["current_liabilities"]), 0.2, 0.5, True, "(cash + short-term investments) / current liabilities"),
    "debt_to_equity": (["total_debt", "equity"], lambda v: _div(v["total_debt"], v["equity"]), 2.0, 1.0, False, "total debt / equity"),
    "debt_ratio": (["total_liabilities", "total_assets"], lambda v: _div(v["total_liabilities"], v["total_assets"]), 0.7, 0.5, False, "total liabilities / total assets"),
    "equity_ratio": (["equity", "total_assets"], lambda v: _div(v["equity"], v["total_assets"]), 0.25, 0.4, True, "equity / total assets"),
    "interest_coverage": (["operating_income", "interest_expense"], lambda v: _div(v["operating_income"], abs(v["interest_expense"])), 1.5, 3.0, True, "operating income / interest expense"),
    "gross_margin": (["revenue", "gross_profit"], lambda v: _div(v["gross_profit"], v["revenue"]), None, None, True, "gross profit / revenue (industry-specific, no generic threshold)"),
    "operating_margin": (["revenue", "operating_income"], lambda v: _div(v["operating_income"], v["revenue"]), 0.03, 0.10, True, "operating income / revenue"),
    "net_margin": (["revenue", "net_income"], lambda v: _div(v["net_income"], v["revenue"]), 0.0, 0.05, True, "net income / revenue"),
    "asset_turnover": (["revenue", "total_assets"], lambda v: _div(v["revenue"], v["total_assets"]), None, None, True, "revenue / total assets (industry-specific)"),
    "ppe_share_of_assets": (["ppe", "total_assets"], lambda v: _div(v["ppe"], v["total_assets"]), None, None, None, "PPE / total assets (descriptive)"),
    "inventory_share_of_assets": (["inventory", "total_assets"], lambda v: _div(v["inventory"], v["total_assets"]), None, None, None, "inventory / total assets (descriptive)"),
    "retained_earnings_to_equity": (["retained_earnings", "equity"], lambda v: _div(v["retained_earnings"], v["equity"]), None, None, None, "retained earnings / equity (descriptive)"),
}
GROWTH = {"revenue_growth": ("revenue", 0.0, 0.05), "net_income_growth": ("net_income", 0.0, 0.05)}
THRESHOLD_SOURCE = "generic rule of thumb, not an industry benchmark"


def band(value, weak, strong, hib):
    if value is None or weak is None or hib is None:
        return None
    if hib:
        return "strength" if value >= strong else "weakness" if value < weak else "neutral"
    return "strength" if value <= strong else "weakness" if value > weak else "neutral"


def trend(history: list[float], hib) -> str:
    """history is newest first. Compares the latest value with the average of up to 3 earlier values."""
    pts = [x for x in history if x is not None]
    if len(pts) < 2 or hib is None:
        return "insufficient_history" if len(pts) < 2 else "descriptive"
    ref = sum(pts[1:4]) / len(pts[1:4])
    scale = max(abs(ref), 0.05)
    change = (pts[0] - ref) / scale
    if abs(change) < 0.10:
        return "stable"
    better = change > 0 if hib else change < 0
    return "improving" if better else "deteriorating"


def load_peers(path: Path | None) -> dict:
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data.get("peers"), dict):
        out = {}
        for m in set(k for p in data["peers"].values() for k in p):
            vals = [p[m] for p in data["peers"].values() if isinstance(p.get(m), (int, float))]
            if vals:
                out[m] = {"value": median(vals), "n_peers": len(vals), "source": "median of " + ", ".join(data["peers"])}
        return out
    return {k: {"value": v, "n_peers": 1, "source": "benchmark file"} for k, v in data.items() if isinstance(v, (int, float))}


def compute(approved: dict, peers: dict | None = None) -> dict:
    peers = peers or {}
    periods = approved["periods"]                      # newest first
    lag = 4 if approved.get("frequency") == "quarterly" else 1
    ser = {f: (e.get("series") or {}) for f, e in approved["fields"].items()}
    partial = {f for f, e in approved["fields"].items() if e.get("partial")}
    out = {}

    def finish(name, values, definition, weak, strong, hib, uses):
        hist = [values.get(p) for p in periods]
        latest_p = next((p for p in periods if values.get(p) is not None), None)
        latest = values.get(latest_p) if latest_p else None
        rec = {"metric": name, "definition": definition, "available": latest is not None, "as_of": latest_p,
               "latest": latest, "latest_is_current_period": latest_p == periods[0] if periods else False,
               "history": {p: values.get(p) for p in periods}, "higher_is_better": hib,
               "absolute_band": band(latest, weak, strong, hib) if latest_p == periods[0] else None,
               "threshold": ({"weak": weak, "strong": strong, "source": THRESHOLD_SOURCE} if weak is not None else None),
               "trend": trend(hist, hib) if latest_p == periods[0] else "insufficient_history", "uses_partial_input": sorted(set(uses) & partial), "relative": None}
        pr = peers.get(name)
        if pr and latest is not None and hib is not None:
            ref = pr["value"]
            gap = (latest - ref) / max(abs(ref), 0.05)
            rel = "in_line" if abs(gap) < 0.10 else ("above_peers" if gap > 0 else "below_peers")
            favourable = (rel == "above_peers") == bool(hib) if rel != "in_line" else None
            rec["relative"] = {"peer_value": ref, "n_peers": pr["n_peers"], "source": pr["source"], "position": rel, "favourable": favourable}
        out[name] = rec



    for name, (needs, fn, weak, strong, hib, definition) in METRIC_DEFS.items():
        values = {}
        ttm = name == "asset_turnover" and lag == 4
        for i, p in enumerate(periods):
            if ttm:
                last4 = [ser.get("revenue", {}).get(q) for q in periods[i:i + 4]]
                assets = ser.get("total_assets", {}).get(p)
                values[p] = sum(last4) / assets if len(last4) == 4 and None not in last4 and assets else None
                continue
            vs = {f: ser.get(f, {}).get(p) for f in needs + ["short_investments"]}
            values[p] = fn(vs) if all(vs[f] is not None for f in needs) else None
        if ttm:
            definition = "revenue of the last four quarters / total assets (industry-specific)"
        finish(name, values, definition, weak, strong, hib, needs)


    for name, (field, weak, strong) in GROWTH.items():
        values = {}
        for i, p in enumerate(periods):
            prev = periods[i + lag] if i + lag < len(periods) else None
            a, b = ser.get(field, {}).get(p), ser.get(field, {}).get(prev) if prev else None
            values[p] = None if a is None or b in (None, 0) else (a - b) / abs(b)
        finish(name, values, f"{field} change vs {'same quarter last year' if lag == 4 else 'prior year'}", weak, strong, True, [field])
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Print metrics from an approved_financials.json")
    ap.add_argument("approved", type=Path)
    ap.add_argument("--peers", type=Path)
    a = ap.parse_args()
    m = compute(json.loads(a.approved.read_text(encoding="utf-8")), load_peers(a.peers))
    for k, r in m.items():
        v = "n/a" if r["latest"] is None else f"{r['latest']:.4f}"
        print(f"{k:28} {v:>10}  {str(r['absolute_band']):10} {r['trend']:20} {'PARTIAL ' if r['uses_partial_input'] else ''}{r['relative'] and r['relative']['position'] or ''}")


if __name__ == "__main__":
    main()