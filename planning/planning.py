import json
import re
from datetime import datetime, timezone
import pandas as pd

BS_KEYS = ["current_assets", "current_liabilities", "cash", "short_investments", "inventory", "receivables",
           "ppe", "total_assets", "total_liabilities", "equity", "retained_earnings", "short_debt", "long_debt"]
IS_KEYS = ["revenue", "cogs", "gross_profit", "operating_income", "net_income", "interest_expense"]

ALIASES = {
    "current_assets":      ["Assets, Current", "Total Current Assets"],
    "current_liabilities": ["Liabilities, Current", "Total Current Liabilities"],
    "cash":                ["Cash and Cash Equivalents, at Carrying Value", "Cash and Cash Equivalents"],
    "short_investments":   ["Short-term Investments", "Marketable Securities, Current"],
    "inventory":           ["Inventory, Net", "Inventories"],
    "receivables":         ["Accounts Receivable, after Allowance for Credit Loss, Current", "Accounts Receivable, Net, Current"],
    "ppe":                 ["Property, Plant and Equipment, Net",
                            "Property, Plant, and Equipment and Finance Lease Right-of-Use Asset, after Accumulated Depreciation and Amortization"],
    "total_assets":        ["Assets"],
    "total_liabilities":   ["Liabilities"],
    "equity":              ["Stockholders' Equity Attributable to Parent",
                            "Stockholders' Equity, Including Portion Attributable to Noncontrolling Interest"],
    "retained_earnings":   ["Retained Earnings (Accumulated Deficit)", "Retained Earnings, Accumulated Deficit"],
    "short_debt":          ["DebtCurrent", "Debt, Current", "LongTermDebtAndFinanceLeasesCurrent", "Long-term Debt, Current Maturities"],
    "long_debt":           ["LongTermDebtAndCapitalLeaseObligations", "Long-term Debt, Excluding Current Maturities",
                            "LongTermDebtAndFinanceLeasesNoncurrent", "Long-term Debt, Noncurrent"],
    "revenue":             ["Revenues", "Revenue from Contract with Customer, Excluding Assessed Tax", "Revenue"],
    "cogs":                ["Cost of Revenue", "Cost of Goods and Services Sold"],
    "gross_profit":        ["Gross Profit"],
    "operating_income":    ["Operating Income (Loss)"],
    "net_income":          ["Net Income (Loss) Attributable to Parent", "Net Income (Loss)"],
    "interest_expense":    ["Interest Expense", "Interest Expense, Non-operating"],
}

BENCH = {
    "current_ratio":     {"hib": True,  "weak": 1.0,  "strong": 1.5,   "src": "General rule of thumb: <1.0 weak, 1.5-2.0 healthy"},
    "quick_ratio":       {"hib": True,  "weak": 0.7,  "strong": 1.0,   "src": "General rule of thumb: >=1.0 healthy"},
    "cash_ratio":        {"hib": True,  "weak": 0.2,  "strong": 0.5,   "src": "General rule of thumb"},
    "debt_to_equity":    {"hib": False, "weak": 2.0,  "strong": 1.0,   "src": "General rule of thumb: <1.0 conservative, >2.0 scrutiny"},
    "debt_ratio":        {"hib": False, "weak": 0.7,  "strong": 0.5,   "src": "General rule of thumb"},
    "interest_coverage": {"hib": True,  "weak": 1.5,  "strong": 3.0,   "src": "General: >=2 good, <1.5 red flag; manufacturing >=3"},
    "equity_ratio":      {"hib": True,  "weak": 0.25, "strong": 0.40,  "src": "General rule of thumb"},
    "gross_margin":      {"hib": True,  "weak": 0.10, "strong": 0.188, "src": "Auto industry 10-yr median ~18.8% (override per industry)"},
    "operating_margin":  {"hib": True,  "weak": 0.02, "strong": 0.065, "src": "Auto industry 10-yr median ~6.5% (override per industry)"},
    "net_margin":        {"hib": True,  "weak": 0.0,  "strong": 0.05,  "src": "General rule of thumb"},
    "revenue_growth":    {"hib": True,  "weak": 0.0,  "strong": 0.05,  "src": "General rule of thumb"},
    "net_income_growth": {"hib": True,  "weak": 0.0,  "strong": 0.05,  "src": "General rule of thumb"},
}


def load_statement(path):
    """Rows = line items, columns = dates (newest first). Auto-fixes orientation, strips tag prefixes."""
    df = pd.read_csv(path, index_col=0)
    try:
        pd.to_datetime(df.columns)
    except Exception:
        df = df.T
    df.columns = pd.to_datetime(df.columns)
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df[df.index.notna() & (df.index.astype(str).str.strip() != "")]
    df.index = [re.sub(r"^[A-Za-z]+(-[A-Za-z]+)?_", "", str(i)) for i in df.index]
    df = df[~df.index.duplicated(keep="first")]
    return df.sort_index(axis=1, ascending=False)


def pick(df, key, notes, aliases=None):
    """Coalesce per period: first alias with a value wins; fallbacks are logged."""
    aliases = aliases or ALIASES
    result = None
    
    for alias in aliases[key]:
        if alias not in df.index:
            continue
        row = df.loc[alias]
        if result is None:
            result = row.copy()
        else:
            fill = result.isna() & row.notna()
            if fill.any():
                result[fill] = row[fill]
                notes.append(f"{key}: {int(fill.sum())} period(s) filled from fallback row '{alias}'")
    if result is None:
        notes.append(f"{key}: not found in file")
    return result


def build_items(bs, inc, aliases=None):
    notes, items = [], {}
    for k in BS_KEYS:
        s = pick(bs, k, notes, aliases) 
        if s is not None:
            items[k] = s
    for k in IS_KEYS:
        s = pick(inc, k, notes, aliases)
        if s is not None:
            items[k] = s
    d = pd.DataFrame(items)
    d = d.sort_index(ascending=False)
    if "total_liabilities" not in d and {"total_assets", "equity"} <= set(d.columns):
        d["total_liabilities"] = d.total_assets - d.equity
        notes.append("total_liabilities derived = total_assets - equity")
    if "gross_profit" not in d and {"revenue", "cogs"} <= set(d.columns):
        d["gross_profit"] = d.revenue - d.cogs
        notes.append("gross_profit derived = revenue - cogs")
    sd = d["short_debt"].fillna(0) if "short_debt" in d else 0
    ld = d["long_debt"].fillna(0) if "long_debt" in d else 0
    d["total_debt"] = sd + ld
    if "short_debt" not in d and "long_debt" not in d:
        notes.append("total_debt: no debt lines found, treated as 0")
    return d, notes


def sdiv(a, b):
    return a / b.where(b != 0)


def compute(d):
    r = pd.DataFrame(index=d.index)
    has = lambda *c: all(x in d for x in c)
    if has("current_assets", "current_liabilities"):
        r["current_ratio"] = sdiv(d.current_assets, d.current_liabilities)
        if has("inventory"):
            r["quick_ratio"] = sdiv(d.current_assets - d.inventory, d.current_liabilities)
    if has("cash", "current_liabilities"):
        inv = d["short_investments"].fillna(0) if "short_investments" in d else 0
        r["cash_ratio"] = sdiv(d.cash + inv, d.current_liabilities)
    if has("equity"):
        r["debt_to_equity"] = sdiv(d.total_debt, d.equity)
    if has("total_assets"):
        if has("total_liabilities"):
            r["debt_ratio"] = sdiv(d.total_liabilities, d.total_assets)
        if has("equity"):
            r["equity_ratio"] = sdiv(d.equity, d.total_assets)
        if has("ppe"):
            r["ppe_share_of_assets"] = sdiv(d.ppe, d.total_assets)
        if has("inventory"):
            r["inventory_share_of_assets"] = sdiv(d.inventory, d.total_assets)
        if has("revenue"):
            r["asset_turnover"] = sdiv(d.revenue, d.total_assets)
    if has("retained_earnings", "equity"):
        r["retained_earnings_to_equity"] = sdiv(d.retained_earnings, d.equity)
    if has("operating_income", "interest_expense"):
        r["interest_coverage"] = sdiv(d.operating_income, d.interest_expense.abs())
    if has("revenue"):
        if has("gross_profit"):
            r["gross_margin"] = sdiv(d.gross_profit, d.revenue)
        if has("operating_income"):
            r["operating_margin"] = sdiv(d.operating_income, d.revenue)
        if has("net_income"):
            r["net_margin"] = sdiv(d.net_income, d.revenue)
        r["revenue_growth"] = d.revenue.sort_index().pct_change().sort_index(ascending=False)
    if has("net_income"):
        r["net_income_growth"] = d.net_income.sort_index().pct_change().sort_index(ascending=False)
    return r


def classify(name, value):
    b = BENCH.get(name)
    if b is None or pd.isna(value):
        return None
    if b["hib"]:
        return "strength" if value >= b["strong"] else "weakness" if value <= b["weak"] else "neutral"
    return "strength" if value <= b["strong"] else "weakness" if value >= b["weak"] else "neutral"


def trend(series, name):
    s = series.dropna()
    if len(s) < 3:
        return "insufficient_history"
    vals = pd.Series(s.sort_index().tail(4).values)
    diffs = vals.diff().dropna()
    direction = "rising" if (diffs > 0).all() else "falling" if (diffs < 0).all() else "mixed"
    b = BENCH.get(name)
    if b is None or direction == "mixed":
        return direction
    improving = (direction == "rising") == b["hib"]
    return f"{direction}_{'improving' if improving else 'deteriorating'}"



def analyze(bs_path, is_path, out_path="client_analysis.json", client="client",
            aliases=None, extra_notes=None, unreliable=None):
    unreliable = unreliable or {}
    bs, inc = load_statement(bs_path), load_statement(is_path)
    items, notes = build_items(bs, inc, aliases)
    notes = (extra_notes or []) + notes
    ratios = compute(items)
    latest = ratios.index[0]
    for c in ["current_assets", "current_liabilities", "total_assets", "equity", "revenue"]:
        if c not in items:
            notes.append(f"WARNING: core item {c} missing - dependent metrics skipped")

    metrics = {}
    for name in ratios.columns:
        valid = ratios[name].dropna()
        if valid.empty:
            notes.append(f"{name}: no data in any period")
            continue
        as_of, v = valid.index[0], valid.iloc[0]
        b = BENCH.get(name)
        band, flag = classify(name, v), None
        if as_of != latest:
            band = "stale"
            flag = f"latest data is from {as_of.strftime('%Y-%m-%d')}, not {latest.strftime('%Y-%m-%d')}"
        if name in unreliable:
            band = "needs_verification"
            flag = f"data check failed: {unreliable[name]}"
        metrics[name] = {
            "as_of": as_of.strftime("%Y-%m-%d"),
            "latest_value": round(float(v), 4),
            "band": band,
            "flag": flag,
            "threshold": {"weak": b["weak"], "strong": b["strong"], "higher_is_better": b["hib"],
                          "source": b["src"]} if b else None,
            "trend": trend(ratios[name], name),
            "history": {d.strftime("%Y-%m-%d"): (None if pd.isna(x) else round(float(x), 4))
                        for d, x in ratios[name].items()},
        }

    pick_band = lambda label: [k for k, v in metrics.items() if v["band"] == label]
    result = {
        "client": client,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "latest_period": latest.strftime("%Y-%m-%d"),
        "periods_available": [d.strftime("%Y-%m-%d") for d in ratios.index],
        "inputs_used_latest": {k: (None if pd.isna(v) else float(v)) for k, v in items.loc[latest].items()},
        "metrics": metrics,
        "summary": {
            "strengths": pick_band("strength"),
            "weaknesses": pick_band("weakness"),
            "neutral": pick_band("neutral"),
            "unbanded": [k for k, v in metrics.items() if v["band"] is None],
            "needs_verification": pick_band("needs_verification"),
            "stale": pick_band("stale"),
        },
        "data_notes": notes,
    }
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2)
    return result
