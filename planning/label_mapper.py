import json
import os
import pandas as pd
import planning as P

DESCRIPTIONS = {
    "current_assets": "total current assets",
    "current_liabilities": "total current liabilities",
    "cash": "cash and cash equivalents",
    "short_investments": "short-term investments / marketable securities",
    "inventory": "inventory, net",
    "receivables": "accounts receivable, current",
    "ppe": "property, plant and equipment, net (include versions that add operating lease or equipment on lease)",
    "total_assets": "total assets",
    "total_liabilities": "total liabilities",
    "equity": "stockholders' equity attributable to the parent",
    "retained_earnings": "retained earnings / accumulated deficit",
    "short_debt": "ALL current debt: current portion of long-term debt, short-term borrowings, finance arm debt due within one year",
    "long_debt": "ALL non-current debt: long-term debt, notes, finance arm long-term debt, finance leases",
    "revenue": "total revenue / net sales",
    "cogs": "cost of revenue / cost of goods sold",
    "gross_profit": "gross profit",
    "operating_income": "operating income (loss)",
    "net_income": "net income attributable to the parent",
    "interest_expense": "interest expense",
}


def propose(bs_labels, is_labels, keys, llm_json, feedback=""):
    prompt = f"""Match each needed item to row labels from the two files.
BALANCE SHEET LABELS: {bs_labels}
INCOME STATEMENT LABELS: {is_labels}
NEEDED ITEMS: { {k: DESCRIPTIONS[k] for k in keys} }
Rules:
- Use ONLY labels copied exactly from the lists above.
- You may return several labels per item, best first.
- Use [] if nothing fits. Do not guess.
- Balance sheet items must come from the balance sheet list, income items from the income statement list.
{feedback}
Return JSON: {{"item_name": {{"labels": ["..."], "reason": "..."}}}}"""
    return llm_json(prompt)


def validate_numbers(d):
    """Returns a list of (message, [metrics that depend on the failed check])."""
    errors = []
    a, l, e = d.get("total_assets"), d.get("total_liabilities"), d.get("equity")
    if a is not None and l is not None and e is not None:
        gap = ((a - l - e).abs() / a).max()
        if gap > 0.03:
            errors.append((f"assets != liabilities + equity (max gap {gap:.1%})",
                           ["debt_ratio", "equity_ratio", "debt_to_equity"]))
    rev, cogs, gp = d.get("revenue"), d.get("cogs"), d.get("gross_profit")
    if rev is not None and cogs is not None and gp is not None:
        gap = ((rev - cogs - gp).abs() / rev).max()
        if gap > 0.02:
            errors.append((f"revenue - cogs != gross_profit (max gap {gap:.1%})", ["gross_margin"]))
    if rev is not None and (rev.dropna().sort_index().pct_change().abs() > 0.9).any():
        errors.append(("revenue changes by more than 90% between years - wrong row?",
                       ["revenue_growth", "gross_margin", "operating_margin", "net_margin", "asset_turnover"]))
    td = d.get("total_debt")
    if td is not None:
        jump = td.replace(0, pd.NA).astype(float).sort_index().pct_change().abs()
        if (jump > 0.7).any():
            errors.append(("total_debt changes by more than 70% in a year - debt rows may be incomplete or redefined",
                           ["debt_to_equity"]))
    return errors


def resolve(bs_path, is_path, llm_json, cache_path=None, max_tries=3):
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    bs, inc = P.load_statement(bs_path), P.load_statement(is_path)
    bs_labels, is_labels = list(bs.index), list(inc.index)
    latest = bs.columns[0]

    def frame(k):
        return bs if k in P.BS_KEYS else inc

    def has_latest(k):
        df = frame(k)
        return any(a in df.index and pd.notna(df.loc[a, latest]) for a in aliases[k])

    aliases = {k: list(v) for k, v in P.ALIASES.items()}
    unresolved = [k for k in aliases if not has_latest(k)]
    feedback, errors = "", []
    last, last_snapshot = {}, None

    for attempt in range(max_tries):
        if unresolved:
            proposal = propose(bs_labels, is_labels, unresolved, llm_json, feedback)
            for k in unresolved:
                entry = proposal.get(k, {})
                labels = [l for l in entry.get("labels", []) if l in frame(k).index]
                aliases[k] = labels + [a for a in P.ALIASES[k] if a not in labels]
                last[k] = {"labels": labels, "reason": entry.get("reason", "")}
            snapshot = json.dumps(aliases, sort_keys=True)
            if snapshot == last_snapshot:
                break
            last_snapshot = snapshot
        d, _ = P.build_items(bs, inc, aliases)
        errors = validate_numbers(d)
        if not errors:
            break
        feedback = "Previous mapping failed these checks: " + "; ".join(m for m, _ in errors)
        unresolved = list(aliases)

    log = [{"item": k, **e} for k, e in last.items() if aliases[k] != P.ALIASES[k]]
    unreliable = {}
    for msg, affected in errors:
        for m in affected:
            unreliable[m] = msg

    result = {
        "aliases": aliases,
        "llm_decisions": log,
        "validation_errors": [m for m, _ in errors],
        "unreliable_metrics": unreliable,
    }
    if cache_path:
        with open(cache_path, "w") as f:
            json.dump(result, f, indent=2)
    return result