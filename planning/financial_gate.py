r"""
financial_gate.py (v2) - Financial approval gate for ANY company's balance sheet + income statement CSV.

What it does
  1. Reads both CSVs (periods as columns or rows; ISO, MM/DD/YYYY or year-only headers; annual or quarterly).
  2. Proposes how every row maps to the standard fields, in two layers:
       a) deterministic matching (exact accounting names, patterns) - fast, repeatable
       b) optional LLM mapping (needs an API key, same settings as sentiment.py) for rows the first layer
          cannot place. The LLM only PROPOSES: every proposal is checked by code before it is accepted.
  3. Guards that no model, pattern or user choice can switch off: a row can feed only one field, "current debt"
     can never be a "...excluding current maturities" row, totals must be totals, rows must exist in the right file.
  4. Derives only what accounting identities allow (gross profit, total debt, total liabilities = assets - equity),
     and labels every derived or partial figure.
  5. Shows the user what was found / proposed / derived / missing; only after approval writes the frozen dataset.

Run:   python financial_gate.py --company "Amazon" --balance "C:\Users\adeel\Desktop\notebooks\vultr\assets\AMZN_balance_sheet_annual_2020_2026 (1).csv" --income "C:\Users\adeel\Desktop\notebooks\vultr\assets\AMZN_income_statement_annual_2020_2026.csv" --units thousands --currency USD
Common options: --units thousands --currency USD --frequency auto|annual|quarterly --entity "consolidated group"
                --report-only (ask nothing)  --yes (accept defaults)  --no-llm  --decisions decisions.json
LLM env (optional):  VULTR_INFERENCE_BASE, VULTR_INFERENCE_API_KEY, VULTR_CHAT_MODEL   (Groq works today)

Outputs (output/financial_<company>_<time>/): gate_report.json, approved_financials.json, mapping_approved.json, decisions.json
Input format: first column = line-item names, other columns = periods (or the transposed layout). Other formats
must be converted to this layout first.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median

SCHEMA_VERSION = 2
TOLERANCE = 0.01
JUMP_LIMIT = 0.70
API_BASE = os.environ.get("VULTR_INFERENCE_BASE", "https://api.vultrinference.com/v1").rstrip("/")
API_KEY = os.environ.get("VULTR_INFERENCE_API_KEY", "")
CHAT_MODEL = os.environ.get("VULTR_CHAT_MODEL", "deepseek-v4.1-flash")

# field -> (statement, aliases in priority order, anchored patterns on the normalised label, meaning)
FIELDS = {
    "current_assets":      ("balance", ["Assets, Current", "Total Current Assets"], [r"^(total )?current assets$"], "total current assets (a subtotal)"),
    "current_liabilities": ("balance", ["Liabilities, Current", "Total Current Liabilities"], [r"^(total )?current liabilities$"], "total current liabilities (a subtotal)"),
    "cash":                ("balance", ["Cash and Cash Equivalents, at Carrying Value", "Cash and Cash Equivalents"], [r"^cash( and cash equivalents)?( at carrying value)?$"], "cash and cash equivalents"),
    "short_investments":   ("balance", ["Debt Securities, Available-for-sale, Current", "Short-term Investments", "Marketable Securities, Current"], [r"short term investments?$", r"marketable securities current$"], "short-term investments / marketable securities (current)"),
    "inventory":           ("balance", ["Inventory, Net", "Inventories"], [r"^inventor(y|ies)( net)?$"], "inventory"),
    "receivables":         ("balance", ["Accounts Receivable, after Allowance for Credit Loss, Current", "Accounts Receivable, Net, Current"], [r"^accounts receivable.*current$", r"^(trade )?receivables( net)?$"], "trade receivables (current)"),
    "ppe":                 ("balance", ["Property, Plant and Equipment, Net", "Property, Plant, and Equipment and Finance Lease Right-of-Use Asset, after Accumulated Depreciation and Amortization"], [r"^property plant (and )?equipment( net)?$"], "property, plant and equipment (net)"),
    "total_assets":        ("balance", ["Assets", "Total Assets"], [r"^(total )?assets$"], "TOTAL assets (not current or noncurrent only)"),
    "total_liabilities":   ("balance", ["Liabilities", "Total Liabilities"], [r"^(total )?liabilities$"], "TOTAL liabilities (not current only, not 'liabilities and equity')"),
    "equity":              ("balance", ["Stockholders' Equity Attributable to Parent", "Total Stockholders' Equity", "Total Equity", "Stockholders' Equity, Including Portion Attributable to Noncontrolling Interest"], [r"^(total )?(stockholders|shareholders) equity( attributable to parent)?$", r"^total equity$"], "total shareholders' equity"),
    "retained_earnings":   ("balance", ["Retained Earnings (Accumulated Deficit)", "Retained Earnings, Accumulated Deficit", "Retained Earnings"], [r"^retained earnings"], "retained earnings"),
    "short_debt":          ("balance", ["DebtCurrent", "Debt, Current", "LongTermDebtAndFinanceLeasesCurrent", "Long-term Debt, Current Maturities"], [r"^debt current$", r"^(long term )?debt,? current (maturities|portion)"], "debt due within one year (current portion / short-term borrowings)"),
    "long_debt":           ("balance", ["LongTermDebtAndCapitalLeaseObligations", "Long-term Debt, Excluding Current Maturities", "LongTermDebtAndFinanceLeasesNoncurrent", "Long-term Debt, Noncurrent"], [r"^long term debt.*(noncurrent|excluding current)"], "long-term debt due after one year"),
    "revenue":             ("income", ["Revenues", "Revenue from Contract with Customer, Excluding Assessed Tax", "Revenue", "Total Revenue", "Net Sales"], [r"^(total )?revenues?$", r"^net sales$"], "total revenue / net sales"),
    "cogs":                ("income", ["Cost of Goods and Services Sold", "Cost of Revenue", "Cost of Sales"], [r"^cost of (goods|revenue|sales)"], "cost of goods sold / cost of revenue"),
    "gross_profit":        ("income", ["Gross Profit"], [r"^gross (profit|margin)$"], "gross profit"),
    "operating_income":    ("income", ["Operating Income (Loss)", "Operating Income"], [r"^operating income"], "operating income"),
    "net_income":          ("income", ["Net Income (Loss) Attributable to Parent", "Net Income (Loss)", "Net Income"], [r"^net income( loss)?( attributable to parent)?$"], "net income"),
    "interest_expense":    ("income", ["Interest Expense", "Interest Expense, Non-operating", "Interest Expense, Debt"], [r"^interest expense"], "interest expense"),
}
DERIVED = {   # field -> (formula text, parts, function(values)->number|None, partial_allowed)
    "gross_profit":      ("revenue - cogs", ["revenue", "cogs"], lambda v: v[0] - v[1], False),
    "total_debt":        ("short_debt + long_debt", ["short_debt", "long_debt"], lambda v: sum(x for x in v if x is not None), True),
    "total_liabilities": ("total_assets - equity", ["total_assets", "equity"], lambda v: v[0] - v[1], False),
}
METRICS = {
    "current_ratio": ["current_assets", "current_liabilities"], "quick_ratio": ["current_assets", "inventory", "current_liabilities"],
    "cash_ratio": ["cash", "current_liabilities"], "debt_to_equity": ["total_debt", "equity"],
    "debt_ratio": ["total_liabilities", "total_assets"], "equity_ratio": ["equity", "total_assets"],
    "ppe_share_of_assets": ["ppe", "total_assets"], "inventory_share_of_assets": ["inventory", "total_assets"],
    "asset_turnover": ["revenue", "total_assets"], "retained_earnings_to_equity": ["retained_earnings", "equity"],
    "interest_coverage": ["operating_income", "interest_expense"], "gross_margin": ["revenue", "gross_profit"],
    "operating_margin": ["revenue", "operating_income"], "net_margin": ["revenue", "net_income"],
    "revenue_growth": ["revenue"], "net_income_growth": ["net_income"],
}
MAPPABLE_WHEN_MISSING = {f for need in METRICS.values() for f in need} - set(DERIVED)
BLOCKING_MIN_FIELDS = ("total_assets", "revenue")
REJECT = re.compile(r"attributable to noncontrolling interest$|redeemable|per share")


# ------------------------------------------------------------------ labels and semantic guards
def norm_label(s: str) -> str:
    s = re.sub(r"^(us-gaap|ifrs-full|[a-z]{1,8})[:_](?=[A-Za-z])", "", s.strip())
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = s.lower().replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


NONCUR = r"excluding current|noncurrent|non current|net of current|less current|after one year"


def violates(field: str, label: str) -> str | None:
    """Meaning guards applied to deterministic matches, LLM proposals and user picks. Returns the reason or None."""
    n = norm_label(label)
    if REJECT.search(n) and "including" not in n:
        return "noncontrolling-interest-only, redeemable or per-share row"
    if field == "short_debt" and re.search(NONCUR, n):
        return "looks like long-term debt (excludes the current portion)"
    if field == "long_debt" and re.search(r"current (maturities|portion)|short term|due within", n) and not re.search(NONCUR, n):
        return "looks like the current portion of debt"
    if field in ("total_assets", "total_liabilities") and re.search(r"\bcurrent\b|noncurrent|non current", n):
        return "a current/noncurrent subtotal, not the total"
    if field == "total_liabilities" and re.search(r"and (stockholders|shareholders|owners)? ?(equity|funds)|and equity", n):
        return "'liabilities and equity' is not total liabilities"
    if field == "total_assets" and "liabilities" in n:
        return "contains liabilities"
    if field == "current_assets" and re.search(r"noncurrent|non current", n):
        return "noncurrent row"
    if field == "current_liabilities" and re.search(r"noncurrent|non current", n):
        return "noncurrent row"
    if field == "equity" and re.search(r"liabilities|noncontrolling interest$", n) and "including" not in n:
        return "not total equity"
    if field in ("revenue", "operating_income", "net_income") and re.search(r"\bcost of\b|expense", n):
        return "an expense row"
    if field == "cogs" and "revenue" in n and "cost" not in n:
        return "a revenue row"
    return None


ALIAS_OWNERS: dict[str, set] = {}
for _f, (_s, _al, _pt, _d) in FIELDS.items():
    for _a in _al:
        ALIAS_OWNERS.setdefault(norm_label(_a), set()).add(_f)


# ------------------------------------------------------------------ reading
def parse_number(x):
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "null", "-", "--", "n/a", "na", "\u2014"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[,$\u20ac\u00a3\s()]", "", s)
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"not a number: {x!r}")
    return -v if neg else v


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_period(h: str, dayfirst: bool = False):
    """-> (date, assumed:bool) or None. Accepts 2025-12-31, 12/31/2025, 31/12/2025, Dec 31 2025, 2025, FY2025."""
    h = str(h).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?", h)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3])), False
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", h)
        if m:
            a, b = int(m[1]), int(m[2])
            d, mo = (a, b) if dayfirst else (b, a)
            return date(int(m[3]), mo, d), False
        m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", h)
        if m and m[1].lower() in MONTHS:
            return date(int(m[3]), MONTHS[m[1].lower()], int(m[2])), False
        m = re.fullmatch(r"(\d{1,2}) ([A-Za-z]{3})[a-z]*\.?,? (\d{4})", h)
        if m and m[2].lower() in MONTHS:
            return date(int(m[3]), MONTHS[m[2].lower()], int(m[1])), False
    except ValueError:
        return None
    m = re.fullmatch(r"(?:FY\s?)?(\d{4})", h, re.I)
    if m:
        return date(int(m[1]), 12, 31), True
    return None


def detect_dayfirst(headers) -> tuple[bool, bool]:
    """-> (dayfirst, ambiguous). Looks at slash dates: a first number above 12 means day-first."""
    first, second = [], []
    for h in headers:
        m = re.fullmatch(r"\s*(\d{1,2})[/.-](\d{1,2})[/.-]\d{4}\s*", str(h))
        if m:
            first.append(int(m[1]))
            second.append(int(m[2]))
    if not first:
        return False, False
    if any(x > 12 for x in first):
        return True, False
    if any(x > 12 for x in second):
        return False, False
    return False, True


def read_statement(path: Path) -> dict:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(text.splitlines()) if any(c.strip() for c in r)]
    if len(rows) < 2 or len(rows[0]) < 2:
        raise ValueError("file has no table (need a label column and at least one period column)")
    head = rows[0][1:]
    dayfirst, ambiguous = detect_dayfirst(head)
    if sum(parse_period(h, dayfirst) is not None for h in head) < max(1, len(head) // 2):
        col0 = [r[0] for r in rows[1:]]
        dayfirst, ambiguous = detect_dayfirst(col0)
        if sum(parse_period(h, dayfirst) is not None for h in col0) >= max(1, len(col0) // 2):
            rows = [list(c) for c in zip(*rows)]
            head = rows[0][1:]
        else:
            raise ValueError("could not recognise period headers (expected dates like 2025-12-31, 12/31/2025 or 2025)")
    periods, assumed, bad_cols = [], False, []
    for h in head:
        p = parse_period(h, dayfirst)
        if p is None:
            bad_cols.append(h)
            periods.append(None)
        else:
            periods.append(p[0].isoformat())
            assumed = assumed or p[1]
    data, issues, dup = {}, [], []
    for r in rows[1:]:
        label = r[0].strip()
        if not label:
            continue
        if label in data:
            dup.append(label)
        vals = {}
        for p, cell in zip(periods, r[1:]):
            if p is None:
                continue
            try:
                vals[p] = parse_number(cell)
            except ValueError as e:
                issues.append(f"{label} / {p}: {e}")
                vals[p] = None
        data[label] = vals
    good = [p for p in periods if p]
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "rows": data,
            "periods": sorted(set(good), reverse=True), "duplicate_periods": len(good) != len(set(good)),
            "periods_assumed_year_end": assumed, "date_format_ambiguous": ambiguous,
            "ignored_columns": bad_cols, "duplicate_labels": dup, "unreadable_cells": issues}


def detect_frequency(periods: list[str]) -> str:
    if len(periods) < 2:
        return "unknown"
    gaps = [(date.fromisoformat(a) - date.fromisoformat(b)).days for a, b in zip(periods, periods[1:])]
    g = median(gaps)
    return "annual" if 330 <= g <= 400 else "quarterly" if 75 <= g <= 105 else "other"


# ------------------------------------------------------------------ deterministic mapping
def coverage(vals: dict, periods) -> int:
    return sum(vals.get(p) is not None for p in periods)


def latest_of(vals: dict, periods):
    return next((vals[p] for p in periods if vals.get(p) is not None), None)


def find_candidates(field: str, stmt: dict) -> list[dict]:
    _, aliases, patterns, _ = FIELDS[field]
    alias_norm = [norm_label(a) for a in aliases]
    found = {}
    for label in stmt["rows"]:
        n = norm_label(label)
        if violates(field, label):
            continue
        owners = ALIAS_OWNERS.get(n, set())
        if owners and field not in owners:
            continue                        # this row is an exact alias of a different field: never reuse it here
        if n in alias_norm:
            found[label] = ("exact", alias_norm.index(n))
        elif any(re.search(p, n) for p in patterns):
            found.setdefault(label, ("pattern", 50))
        else:
            best = max((difflib.SequenceMatcher(None, n, a).ratio() for a in alias_norm), default=0)
            if best >= 0.86:
                found.setdefault(label, ("fuzzy", 90))
    out = [{"label": l, "method": m, "rank": r, "periods_with_data": coverage(stmt["rows"][l], stmt["periods"]),
            "latest_value": latest_of(stmt["rows"][l], stmt["periods"])} for l, (m, r) in found.items()]
    return sorted(out, key=lambda c: (c["rank"], -c["periods_with_data"]))


def overlapping_conflict(cands: list[dict], stmt: dict) -> bool:
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            for p in stmt["periods"]:
                x, y = stmt["rows"][a["label"]].get(p), stmt["rows"][b["label"]].get(p)
                if x is not None and y is not None and abs(x - y) > TOLERANCE * max(abs(x), abs(y), 1):
                    return True
    return False


def build_series(chain: list[str], stmt: dict, periods) -> tuple[dict, dict]:
    values, source = {}, {}
    for p in periods:
        values[p], source[p] = None, None
        for label in chain:
            v = stmt["rows"][label].get(p)
            if v is not None:
                values[p], source[p] = v, label
                break
    return values, source


def suggest_rows(field: str, stmt: dict, n: int = 6) -> list[dict]:
    words = set()
    for a in FIELDS[field][1] + [FIELDS[field][3]]:
        words |= {w for w in norm_label(a).split() if len(w) > 2}
    scored = []
    for label, vals in stmt["rows"].items():
        if violates(field, label) or not coverage(vals, stmt["periods"]):
            continue
        ov = len(words & set(norm_label(label).split()))
        if ov:
            scored.append((ov, {"label": label, "latest_value": latest_of(vals, stmt["periods"]),
                                "periods_with_data": coverage(vals, stmt["periods"])}))
    return [c for _, c in sorted(scored, key=lambda t: -t[0])[:n]]


def set_chain(entry: dict, chain: list[str], stmt: dict, periods) -> None:
    entry["chain"] = chain
    vals, src = build_series(chain, stmt, periods)
    entry["series"], entry["source_by_period"] = vals, src
    used = {s for s in src.values() if s}
    if len(used) > 1:
        entry["notes"].append("different source rows supply different periods: " + ", ".join(sorted(used)))


def propose_mapping(statements: dict, periods: list[str], choices: dict, defaults: set | None = None) -> dict:
    defaults = defaults or set()
    fields = {}
    for field, (sname, aliases, patterns, desc) in FIELDS.items():
        stmt = statements[sname]
        cands = find_candidates(field, stmt)
        entry = {"field": field, "description": desc, "statement": sname, "candidates": cands,
                 "status": "missing", "chain": [], "needs_choice": False, "notes": []}
        chosen = choices.get(field)
        if chosen and chosen not in stmt["rows"]:
            entry["notes"].append(f"chosen row {chosen!r} not found in the file")
            chosen = None
        if chosen:
            set_chain(entry, [chosen], stmt, periods)
            entry["status"] = "default_accepted" if field in defaults else "chosen_by_user"
            why = violates(field, chosen)
            if why:
                entry["notes"].append(f"warning: the chosen row {why}")
        elif cands:
            exact = [c for c in cands if c["method"] == "exact"] or cands
            if len(exact) > 1 and overlapping_conflict(exact, stmt):
                set_chain(entry, [exact[0]["label"]], stmt, periods)
                entry.update(options=exact, needs_choice=True, status="needs_choice")
                entry["notes"].append("several rows hold different numbers for the same period; using the first alias until you choose")
            else:
                set_chain(entry, [c["label"] for c in exact], stmt, periods)
                entry["status"] = "proposed_" + exact[0]["method"]
                if exact[0]["method"] != "exact":
                    entry["notes"].append(f"matched by {exact[0]['method']} similarity, please confirm")
        if entry["status"] == "missing":
            entry["suggestions"] = suggest_rows(field, stmt)
        fields[field] = entry
    return fields


def add_derived(fields: dict, periods: list[str]) -> None:
    for field, (formula, parts, fn, partial_ok) in DERIVED.items():
        e = fields.get(field)
        if e and e.get("series") and any(v is not None for v in e["series"].values()):
            continue
        series, partial = {}, False
        for p in periods:
            vs = [(fields.get(x, {}).get("series") or {}).get(p) for x in parts]
            if all(v is not None for v in vs):
                series[p] = fn(vs)
            elif partial_ok and any(v is not None for v in vs):
                series[p] = fn(vs)
                partial = True
            else:
                series[p] = None
        if any(v is not None for v in series.values()):
            notes = ["derived, not read from a statement row"]
            if partial:
                missing = [x for x in parts if not any(v is not None for v in (fields.get(x, {}).get("series") or {}).values())]
                notes.append("PARTIAL: " + (", ".join(missing) or "some components") + " not available, so this is a lower bound")
            if field == "total_liabilities":
                notes.append("equals total assets minus the chosen equity row; if that row excludes noncontrolling interest, liabilities include it")
            fields[field] = {"field": field, "description": formula, "statement": "derived", "candidates": [], "status": "derived",
                             "formula": formula, "chain": [], "needs_choice": False, "series": series, "partial": partial,
                             "source_by_period": {p: formula for p in periods if series[p] is not None}, "notes": notes}
        elif field not in fields:
            fields[field] = {"field": field, "description": formula, "statement": "derived", "candidates": [], "status": "missing",
                             "chain": [], "needs_choice": False, "notes": []}


# ------------------------------------------------------------------ LLM mapping (proposals only)
SYSTEM_PROMPT = (
    "You are a financial-statement mapping assistant. You get the exact row labels of a company's balance sheet and "
    "income statement (with latest value and number of periods filled) and a list of standard fields with meanings. "
    "For each field choose the ONE row whose accounting meaning matches, or none. Rules: use labels exactly as given; "
    "a row may serve only ONE field; totals must be totals, not components; 'short_debt' is debt due within one year and "
    "must never be a row that says 'excluding current maturities' or 'noncurrent'; 'long_debt' is the portion due after "
    "one year; 'total_liabilities' is the TOTAL, not current liabilities and not 'liabilities and equity'; never invent "
    "or calculate rows; if unsure, return no row. Rows may be in any language. Reply with ONLY JSON: "
    '{"mappings":[{"field":"...","row":"<exact label or null>","confidence":0.0-1.0,"reason":"short"}]}'
)


def call_llm(messages: list[dict]):
    """Returns the model text, or None (no key / error). Replaced in tests."""
    if not API_KEY:
        return None
    try:
        import httpx
    except ImportError:
        return None
    for attempt in range(4):
        try:
            r = httpx.post(f"{API_BASE}/chat/completions", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=90,
                           json={"model": CHAT_MODEL, "messages": messages, "temperature": 0, "max_tokens": 3000})
        except httpx.HTTPError:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt * 3)
            continue
        print(f"(LLM mapping unavailable: HTTP {r.status_code})")
        return None
    return None


def extract_json_object(text: str):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b < a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None


def llm_proposals(statements: dict, periods: list[str], fields: dict) -> tuple[dict, list]:
    """-> ({field: {row, confidence, reason}}, rejected[{field,row,why}])"""
    lines = []
    for sname in ("balance", "income"):
        lines.append(f"[{sname.upper()} SHEET ROWS]")
        for label, vals in statements[sname]["rows"].items():
            lines.append(f"- {label} | latest={latest_of(vals, statements[sname]['periods'])} | periods_filled={coverage(vals, statements[sname]['periods'])}")
    catalogue = "\n".join(f"- {f} ({v[0]} sheet): {v[3]}" for f, v in FIELDS.items())
    out = call_llm([{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "STANDARD FIELDS:\n" + catalogue + "\n\n" + "\n".join(lines)}])
    obj = extract_json_object(out) if out else None
    if not obj or not isinstance(obj.get("mappings"), list):
        return {}, []
    taken = {l for e in fields.values() for l in e["chain"] if e["status"] not in ("missing",)}
    good, rejected = {}, []
    for m in obj["mappings"]:
        if not isinstance(m, dict) or m.get("field") not in FIELDS or not m.get("row"):
            continue
        f, row = m["field"], str(m["row"])
        stmt = statements[FIELDS[f][0]]
        why = ("row is not in the " + FIELDS[f][0] + " file") if row not in stmt["rows"] else violates(f, row)
        if not why and not coverage(stmt["rows"][row], stmt["periods"]):
            why = "row has no numbers"
        if not why and any(g["row"] == row for g in good.values()):
            why = "same row already proposed for another field"
        if why:
            rejected.append({"field": f, "row": row, "why": why})
            continue
        try:
            conf = max(0.0, min(1.0, float(m.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        good[f] = {"row": row, "confidence": conf, "reason": str(m.get("reason", ""))[:200]}
    return good, rejected


def reconcile_llm(fields: dict, statements: dict, periods: list[str], good: dict, rejected: list) -> list[dict]:
    notes = []
    for r in rejected:
        notes.append({"severity": "warning", "code": "llm_rejected", "message": f"model proposal rejected for {r['field']}: '{r['row']}' ({r['why']})", "field": r["field"]})
    for f, p in good.items():
        e = fields[f]
        stmt = statements[e["statement"]]
        used_elsewhere = [g for g, x in fields.items() if g != f and p["row"] in x["chain"]]
        if used_elsewhere:
            notes.append({"severity": "warning", "code": "llm_rejected", "message": f"model proposal for {f} rejected: '{p['row']}' already feeds {used_elsewhere[0]}", "field": f})
            continue
        if e["status"] == "missing":
            set_chain(e, [p["row"]], stmt, periods)
            e["status"] = "llm_proposed"
            e["llm"] = p
            e.pop("suggestions", None)
            e["notes"].append(f"proposed by the model ({p['confidence']:.0%}): {p['reason']}")
        elif p["row"] not in e["chain"]:
            e["llm_alternative"] = p
            if e["needs_choice"] and p["row"] in [o["label"] for o in e.get("options", [])]:
                e["notes"].append(f"model recommends '{p['row']}': {p['reason']}")
            else:
                notes.append({"severity": "warning", "code": "llm_disagrees", "field": f,
                              "message": f"{f}: the model would use '{p['row']}' instead of '{e['chain'][0] if e['chain'] else '-'}' ({p['reason']})"})
    return notes


# ------------------------------------------------------------------ checks
def run_checks(fields: dict, periods: list[str], statements: dict, bundle: dict) -> list[dict]:
    checks = []

    def add(sev, code, msg, **kw):
        checks.append({"severity": sev, "code": code, "message": msg, **kw})

    def v(f, p):
        return (fields.get(f, {}).get("series") or {}).get(p)

    for s in ("balance", "income"):
        st = statements[s]
        if st["duplicate_periods"]:
            add("blocking", "duplicate_periods", f"{s}: the same period appears twice")
        if st["unreadable_cells"]:
            add("warning", "unreadable_cells", f"{s}: {len(st['unreadable_cells'])} cells are not numbers (treated as missing)", examples=st["unreadable_cells"][:3])
        if st["duplicate_labels"]:
            add("warning", "duplicate_labels", f"{s}: repeated row names {st['duplicate_labels'][:3]} (last one wins)")
        if st["ignored_columns"]:
            add("warning", "ignored_columns", f"{s}: columns not recognised as periods were ignored: {st['ignored_columns'][:3]}")
        if st["periods_assumed_year_end"]:
            add("info", "assumed_year_end", f"{s}: year-only headers were read as 31 December")
        if st["date_format_ambiguous"]:
            add("info", "date_format_assumed", f"{s}: dates like 12/31/2025 were read as month/day/year")
    bs, is_ = set(statements["balance"]["periods"]), set(statements["income"]["periods"])
    if bs != is_:
        add("warning", "period_mismatch", "balance sheet and income statement cover different periods; only the shared ones are used",
            balance_only=sorted(bs - is_), income_only=sorted(is_ - bs))
    freq = bundle["frequency"]
    if len(periods) < 2:
        add("warning", "single_period", "only one shared period: trends and growth rates cannot be calculated")
    lo, hi = (330, 400) if freq == "annual" else (75, 105) if freq == "quarterly" else (0, 10**6)
    gaps = [(a, b) for a, b in zip(periods, periods[1:]) if not lo <= (date.fromisoformat(a) - date.fromisoformat(b)).days <= hi]
    if gaps:
        add("warning", "irregular_periods", f"periods are not evenly spaced for {freq} data", examples=gaps[:3])
    if freq == "quarterly":
        add("warning", "quarterly_flows", "quarterly data: income-statement figures from 10-Q filings may be cumulative year-to-date; confirm they are single-quarter values before comparing periods")
    if freq == "other":
        add("warning", "frequency_unclear", "periods are neither about 90 nor about 365 days apart; set --frequency if known")
    for f in BLOCKING_MIN_FIELDS:
        if not any(x is not None for x in (fields.get(f, {}).get("series") or {}).values()):
            add("blocking", "missing_core_field", f"no usable row found for {f.replace('_', ' ')}", field=f)
    users: dict[str, list] = {}
    for f, e in fields.items():
        for label in e["chain"]:
            users.setdefault(label, []).append(f)
    for label, fs in users.items():
        if len(fs) > 1:
            add("blocking", "row_reused", f"the row '{label}' feeds more than one field ({', '.join(fs)}); this would double count", fields=fs)
    for p in periods:
        a, l, e = v("total_assets", p), v("total_liabilities", p), v("equity", p)
        derived_l = fields.get("total_liabilities", {}).get("status") == "derived"
        if None not in (a, l, e) and not derived_l and abs(a - (l + e)) > TOLERANCE * abs(a):
            alt = [c["label"] for c in fields.get("equity", {}).get("candidates", [])
                   if statements["balance"]["rows"][c["label"]].get(p) is not None
                   and abs(a - (l + statements["balance"]["rows"][c["label"]][p])) <= TOLERANCE * abs(a)]
            hint = f"; it balances with the row '{alt[0]}'" if alt else " (equity may exclude noncontrolling interest)"
            add("warning", "balance_identity", f"{p}: assets {a:,.0f} differ from liabilities + equity {l + e:,.0f} by {abs(a - l - e) / abs(a):.1%}{hint}", period=p)
        ca, ta = v("current_assets", p), v("total_assets", p)
        if None not in (ca, ta) and ca > ta * (1 + TOLERANCE):
            add("warning", "subtotal_order", f"{p}: current assets exceed total assets", period=p)
        cl, tl = v("current_liabilities", p), v("total_liabilities", p)
        if None not in (cl, tl) and cl > tl * (1 + TOLERANCE):
            add("warning", "subtotal_order", f"{p}: current liabilities exceed total liabilities", period=p)
        if ca is not None:
            parts = [v(x, p) for x in ("cash", "short_investments", "inventory", "receivables")]
            if sum(x for x in parts if x is not None) > ca * (1 + TOLERANCE):
                add("warning", "components_exceed_subtotal", f"{p}: cash + investments + inventory + receivables exceed current assets", period=p)
        for f in ("total_assets", "total_liabilities", "cash", "inventory", "ppe", "revenue"):
            x = v(f, p)
            if x is not None and x < 0:
                add("warning", "negative_value", f"{p}: {f} is negative ({x:,.0f})", period=p, field=f)
        rev, cg, gp = v("revenue", p), v("cogs", p), (fields.get("gross_profit", {}).get("series") or {}).get(p)
        if None not in (rev, cg, gp) and fields["gross_profit"]["status"] != "derived" and abs(rev - cg - gp) > TOLERANCE * abs(rev):
            add("warning", "gross_profit_identity", f"{p}: revenue - cost of sales does not equal gross profit", period=p)
    stock_like = ("total_assets", "total_liabilities", "equity", "total_debt", "ppe", "current_assets", "current_liabilities", "long_debt", "short_debt", "cash")
    for f, e in fields.items():
        ser = e.get("series")
        if not ser:
            continue
        if f in stock_like or (f == "revenue" and freq == "annual"):
            for newer, older in zip(periods, periods[1:]):
                x, y = ser.get(newer), ser.get(older)
                if x is not None and y not in (None, 0) and abs(x / y - 1) > JUMP_LIMIT:
                    add("warning", "large_jump", f"{f}: {older} -> {newer} changes by {x / y - 1:+.0%}; check the row definition did not change", field=f)
        miss = [p for p in periods if ser.get(p) is None]
        if miss and len(miss) < len(periods):
            add("warning", "gaps_in_series", f"{f}: no value for {', '.join(miss)}", field=f, periods=miss)
        if e.get("partial"):
            add("warning", "partial_total", f"{f}: {e['notes'][-1] if e['notes'] else 'partial'}", field=f)
    ta, rev = (v("total_assets", periods[0]), v("revenue", periods[0])) if periods else (None, None)
    if ta and rev and not 0.02 <= ta / rev <= 100:
        add("warning", "unit_mismatch_suspected", "total assets and revenue differ by an unusual factor: the two files may use different units")
    for f, e in fields.items():
        if e["status"] == "needs_choice":
            opts = "; ".join(f"{c['label']} = {c['latest_value']:,.0f}" for c in e.get("options", e["candidates"]) if c["latest_value"] is not None)
            add("decision", "choose_row", f"{f}: several rows disagree ({opts}); pick the correct one", field=f)
        if e["status"] == "llm_proposed":
            add("info", "llm_proposed", f"{f}: row '{e['chain'][0]}' was proposed by the model ({e['llm']['confidence']:.0%}) - {e['llm']['reason']}", field=f)
        for n in e.get("notes", []):
            if "different source rows" in n:
                add("warning", "definition_mix", f"{f}: {n}. The definition may change between periods.", field=f)
    if not bundle.get("units") or bundle["units"] == "unknown":
        add("info", "units_unknown", "units not stated (thousands/millions?). Ratios are unaffected if both files use the same units.")
    if not bundle.get("currency"):
        add("info", "currency_unknown", "currency not stated")
    return checks


def metric_availability(fields: dict, periods: list[str]) -> dict:
    out = {}
    for m, need in METRICS.items():
        have = []
        for p in periods:
            ok = all((fields.get(f, {}).get("series") or {}).get(p) is not None for f in need)
            if m.endswith("_growth"):
                i = periods.index(p)
                ok = ok and i + 1 < len(periods) and ((fields[need[0]].get("series") or {}).get(periods[i + 1]) not in (None, 0))
            if ok:
                have.append(p)
        out[m] = {"periods_available": len(have), "latest_available": bool(periods) and periods[0] in have, "needs": need,
                  "missing_fields": [f for f in need if not any(x is not None for x in (fields.get(f, {}).get("series") or {}).values())],
                  "uses_partial": [f for f in need if fields.get(f, {}).get("partial")]}
    return out


# ------------------------------------------------------------------ proposal / approval
def build_proposal(company: str, balance: Path, income: Path, choices: dict | None = None, currency: str = "",
                   units: str = "unknown", entity: str = "", defaults: set | None = None,
                   frequency: str = "auto", use_llm: bool = True) -> dict:
    choices = choices or {}
    statements = {"balance": read_statement(balance), "income": read_statement(income)}
    periods = sorted(set(statements["balance"]["periods"]) & set(statements["income"]["periods"]), reverse=True)
    shared = bool(periods)
    if not periods:
        periods = sorted(set(statements["balance"]["periods"]) | set(statements["income"]["periods"]), reverse=True)
    freq = detect_frequency(periods) if frequency == "auto" else frequency
    fields = propose_mapping(statements, periods, choices, defaults)
    llm_notes, llm_used = [], False
    if use_llm and API_KEY and any(e["status"] in ("missing", "needs_choice") or e["status"].startswith("proposed_") for e in fields.values()):
        good, rejected = llm_proposals(statements, periods, fields)
        llm_used = bool(good or rejected)
        llm_notes = reconcile_llm(fields, statements, periods, good, rejected)
    add_derived(fields, periods)
    bundle = {"units": units, "currency": currency, "frequency": freq}
    checks = run_checks(fields, periods, statements, bundle) + llm_notes
    if not shared:
        checks.append({"severity": "blocking", "code": "no_shared_periods", "message": "the two files share no period"})
    avail = metric_availability(fields, periods)
    limits = [f"{m}: cannot be calculated (missing {', '.join(a['missing_fields'])})" for m, a in avail.items() if a["missing_fields"]]
    limits += [f"{m}: not available for the latest period ({periods[0]})" for m, a in avail.items() if not a["missing_fields"] and not a["latest_available"] and periods]
    limits += [f"{m}: uses a partial figure ({', '.join(a['uses_partial'])}), treat as a lower bound" for m, a in avail.items() if a["uses_partial"]]
    sev = {c["severity"] for c in checks}
    status = "blocked" if "blocking" in sev else "needs_decisions" if "decision" in sev else "ready_for_review"
    return {"schema_version": SCHEMA_VERSION, "company": company, "currency": currency, "units": units, "frequency": freq,
            "entity_scope": entity, "created_at": datetime.now(timezone.utc).isoformat(), "status": status, "periods": periods,
            "llm": {"used": llm_used, "model": CHAT_MODEL if llm_used else None},
            "source_files": {k: {"path": s["path"], "sha256": s["sha256"], "rows": len(s["rows"]), "periods": s["periods"]} for k, s in statements.items()},
            "fields": fields, "checks": checks, "metric_availability": avail, "limitations": limits, "choices": choices}


def apply_decisions(proposal: dict, approved: bool, approver: str = "user", mode: str = "interactive"):
    if proposal["status"] == "blocked":
        raise ValueError("blocked: fix the blocking problems and upload corrected files")
    if proposal["status"] == "needs_decisions":
        raise ValueError("unanswered row choices remain")
    if not approved:
        raise ValueError("not approved")
    fields = {f: {"description": e["description"], "status": e["status"], "source_rows": e["chain"], "formula": e.get("formula"),
                  "partial": e.get("partial", False), "series": e.get("series"), "source_by_period": e.get("source_by_period"),
                  "llm": e.get("llm"), "notes": e.get("notes", [])} for f, e in proposal["fields"].items() if e.get("series")}
    mapping = {"aliases": {f: e["chain"] for f, e in proposal["fields"].items() if e["chain"]}}
    return {"schema_version": SCHEMA_VERSION, "approved_at": datetime.now(timezone.utc).isoformat(), "approved_by": approver,
            "approval_mode": mode, "company": proposal["company"], "currency": proposal["currency"], "units": proposal["units"],
            "frequency": proposal["frequency"], "entity_scope": proposal["entity_scope"], "periods": proposal["periods"],
            "source_files": proposal["source_files"], "llm": proposal["llm"], "fields": fields,
            "accepted_warnings": [c for c in proposal["checks"] if c["severity"] in ("warning", "info")],
            "limitations": proposal["limitations"], "metric_availability": proposal["metric_availability"], "choices": proposal["choices"],
            "note": "Approval records acceptance of the disclosed inputs and limitations; it does not prove the statements are correct."}, mapping


# ------------------------------------------------------------------ terminal flow
def show(proposal: dict) -> None:
    f = proposal["fields"]
    print(f"\n=== {proposal['company']} | {proposal['frequency']} | periods {proposal['periods'][-1]} .. {proposal['periods'][0]} ({len(proposal['periods'])}) | status: {proposal['status']} ===")
    print(f"{'field':20} {'status':18} {'source row':58} {'periods':>7}")
    for name, e in f.items():
        src = e["chain"][0] if e["chain"] else (e.get("formula") or "-")
        n = sum(v is not None for v in (e.get("series") or {}).values())
        flag = " (partial)" if e.get("partial") else ""
        print(f"{name:20} {e['status']:18} {(src[:50] + flag):58} {n:>3}/{len(proposal['periods'])}")
    for sev in ("blocking", "decision", "warning", "info"):
        items = [c for c in proposal["checks"] if c["severity"] == sev]
        if items:
            print(f"\n{sev.upper()} ({len(items)})")
            for c in items[:12]:
                print("  -", c["message"])
            if len(items) > 12:
                print(f"  ... and {len(items) - 12} more (see gate_report.json)")
    if proposal["limitations"]:
        print(f"\nLIMITATIONS ({len(proposal['limitations'])}):")
        for l in proposal["limitations"][:6]:
            print("  -", l)


def ask(prompt: str, default: str = "") -> str:
    try:
        ans = input(prompt).strip()
    except EOFError:
        ans = ""
    return ans or default


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company", required=True)
    ap.add_argument("--balance", type=Path, required=True)
    ap.add_argument("--income", type=Path, required=True)
    ap.add_argument("--currency", default="")
    ap.add_argument("--units", default="unknown", choices=["unknown", "units", "thousands", "millions", "billions"])
    ap.add_argument("--frequency", default="auto", choices=["auto", "annual", "quarterly"])
    ap.add_argument("--entity", default="", help='e.g. "consolidated group", "Budapest office"')
    ap.add_argument("--outdir", type=Path, default=Path("output"))
    ap.add_argument("--decisions", type=Path)
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--yes", action="store_true", help="accept proposed defaults without questions")
    ap.add_argument("--no-llm", action="store_true", help="deterministic matching only")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    use_llm = not a.no_llm
    if use_llm and not API_KEY:
        print("(no VULTR_INFERENCE_API_KEY set: using deterministic matching only)")
    saved = json.loads(a.decisions.read_text(encoding="utf-8")) if a.decisions else {}
    choices = dict(saved.get("choices", {}))
    kw = dict(currency=a.currency, units=a.units, entity=a.entity, frequency=a.frequency, use_llm=use_llm)
    try:
        proposal = build_proposal(a.company, a.balance, a.income, choices, **kw)
    except (ValueError, OSError) as e:
        sys.exit(f"Cannot read the files: {e}")
    out = a.outdir / f"financial_{re.sub(r'[^a-z0-9]+', '-', a.company.lower()).strip('-')}_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    interactive = not (a.report_only or a.yes or saved.get("approved"))
    defaults: set = set()
    for _ in range(3):
        pending = [e for e in proposal["fields"].values() if e["needs_choice"]]
        if not pending or a.report_only:
            break
        for e in pending:
            if interactive:
                print(f"\nField '{e['field']}' ({e['description']}): rows disagree.")
                opts = e.get("options", e["candidates"])
                for i, c in enumerate(opts, 1):
                    print(f"  {i}. {c['label']}  (latest {c['latest_value']}, {c['periods_with_data']} periods)")
                rec = [n for n in e["notes"] if n.startswith("model recommends")]
                if rec:
                    print("  " + rec[0])
                pick = ask(f"Which row is correct? [1-{len(opts)}, Enter = 1]: ", "1")
                try:
                    choices[e["field"]] = opts[int(pick) - 1]["label"]
                except (ValueError, IndexError):
                    choices[e["field"]] = opts[0]["label"]
            else:
                choices[e["field"]] = e["chain"][0]
                defaults.add(e["field"])
        proposal = build_proposal(a.company, a.balance, a.income, choices, defaults=defaults, **kw)
    if interactive:
        asked = False
        for e in proposal["fields"].values():
            if e["status"] == "missing" and e.get("suggestions") and e["field"] in MAPPABLE_WHEN_MISSING:
                print(f"\nNo row was matched for '{e['field']}' ({e['description']}). Closest rows in the file:")
                for i, c in enumerate(e["suggestions"], 1):
                    print(f"  {i}. {c['label']}  (latest {c['latest_value']}, {c['periods_with_data']} periods)")
                pick = ask(f"Use one of these? [1-{len(e['suggestions'])}, Enter = leave unmapped]: ", "")
                if pick.isdigit() and 1 <= int(pick) <= len(e["suggestions"]):
                    choices[e["field"]] = e["suggestions"][int(pick) - 1]["label"]
                    asked = True
        if asked:
            proposal = build_proposal(a.company, a.balance, a.income, choices, defaults=defaults, **kw)
    (out / "gate_report.json").write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    show(proposal)
    print(f"\nproposal saved -> {out / 'gate_report.json'}")
    if a.report_only:
        return
    if proposal["status"] == "blocked":
        sys.exit("\nBLOCKED: please upload corrected files (see BLOCKING above).")
    approved = bool(saved.get("approved")) or a.yes
    mode = "auto" if a.yes else "decisions_file" if saved.get("approved") else "interactive"
    if not approved:
        approved = ask("\nThese inputs, model proposals and limitations will be used. Approve and continue? [y/N]: ").lower() in ("y", "yes")
    if not approved:
        sys.exit("Not approved. Upload corrected statements and run the gate again.")
    dataset, mapping = apply_decisions(proposal, True, mode=mode)
    (out / "approved_financials.json").write_text(json.dumps(dataset, indent=2), encoding="utf-8")
    (out / "mapping_approved.json").write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    (out / "decisions.json").write_text(json.dumps({"choices": choices, "approved": True}, indent=2), encoding="utf-8")
    print(f"APPROVED -> {out / 'approved_financials.json'}")


if __name__ == "__main__":
    main()