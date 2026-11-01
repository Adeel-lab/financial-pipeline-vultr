"""
evidence_financials.py - computed facts from an approved_financials.json, searchable like article passages.

Why: a root cause should be checked against the company's own numbers as well as news. This module turns the
approved statements into short, exact sentences (year-on-year changes, cost-of-sales share of revenue, where an
operating-income change came from, and what-if arithmetic). Every number is calculated by code from the approved
file, never by the model. A fact can show THAT something changed, never WHY.

Used by rca.py. No network, no API key.
  python evidence_financials.py path/to/approved_financials.json "liabilities increased in 2023"
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from evidence_articles import ArticleIndex

LABELS = {
    "current_assets": ("Current assets", ""), "current_liabilities": ("Current liabilities", ""),
    "cash": ("Cash and cash equivalents", "cash"), "short_investments": ("Short-term investments", "marketable securities"),
    "inventory": ("Inventory", "stock"), "receivables": ("Receivables", "accounts receivable"),
    "ppe": ("Property, plant and equipment", "ppe capital assets"), "total_assets": ("Total assets", "assets"),
    "total_liabilities": ("Total liabilities", "liabilities obligations"), "equity": ("Shareholders' equity", "equity"),
    "retained_earnings": ("Retained earnings", "accumulated deficit"), "short_debt": ("Short-term debt", "debt borrowings"),
    "long_debt": ("Long-term debt", "debt borrowings"), "total_debt": ("Total debt", "debt borrowings"),
    "revenue": ("Revenue", "sales net sales"), "cogs": ("Cost of sales", "cost of goods sold cost of revenue costs"),
    "gross_profit": ("Gross profit", "gross margin"), "operating_income": ("Operating income", "operating profit loss earnings"),
    "interest_expense": ("Interest expense", "interest"), "net_income": ("Net income", "profit earnings"),
}
PCT_METRICS = {"operating_margin", "net_margin", "gross_margin", "revenue_growth", "net_income_growth",
               "ppe_share_of_assets", "inventory_share_of_assets", "debt_ratio", "equity_ratio", "retained_earnings_to_equity"}
UNIT_MULT = {"units": 1.0, "thousands": 1e3, "millions": 1e6, "billions": 1e9}
UNIT_NAME = {1.0: "plain units", 1e3: "thousands", 1e6: "millions", 1e9: "billions"}
CURRENCY = {"USD": "$", "EUR": "EUR ", "GBP": "GBP "}
MAX_PLAUSIBLE = 2e13       # no company has 20 trillion in assets or sales; a larger number means the unit label is off


def _unit(approved: dict, ser: dict):
    declared = str(approved.get("units") or "unknown").lower()
    mult = UNIT_MULT.get(declared)
    if mult is None:
        return None, None
    ref = None
    for f in ("total_assets", "revenue"):
        vals = [abs(v) for v in (ser.get(f) or {}).values() if isinstance(v, (int, float))]
        if vals:
            ref = max(vals)
            break
    note = None
    if ref is not None and ref * mult > MAX_PLAUSIBLE:
        while mult > 1 and ref * mult > MAX_PLAUSIBLE:
            mult /= 1000
        note = (f"Unit check: the approved file says '{declared}', but its values only make sense as "
                f"{UNIT_NAME.get(mult, 'smaller units')}; amounts here assume {UNIT_NAME.get(mult, 'smaller units')}. Percentages are not affected.")
    return mult, note


def _money(v, mult, cur):
    if v is None:
        return "n/a"
    if mult is None:
        return f"{v:,.0f}"
    d = v * mult
    a = abs(d)
    s = f"{a / 1e9:,.1f}B" if a >= 1e9 else f"{a / 1e6:,.1f}M" if a >= 1e6 else f"{a:,.0f}"
    return ("-" if d < 0 else "") + cur + s


def _pct(x):
    return f"{x * 100:+.1f}%"


def _delta(v, mult, cur):
    """A signed change: +$2.3B or -$14.4B."""
    s = _money(abs(v), mult, cur)
    return ("+" if v > 0 else "-" if v < 0 else "") + s


def build_facts(approved: dict) -> list[dict]:
    periods = approved.get("periods") or []                     # newest first
    fields = approved.get("fields") or {}
    ser = {f: (e.get("series") or {}) for f, e in fields.items()}
    mult, unit_note = _unit(approved, ser)
    cur = CURRENCY.get(str(approved.get("currency") or "").upper(), (str(approved.get("currency") or "") + " ").lstrip())
    lab = (lambda p: p[:4]) if approved.get("frequency") != "quarterly" else (lambda p: p)
    m = lambda f, p: _money(ser.get(f, {}).get(p), mult, cur)

    jump, mixed, gaps = defaultdict(list), {}, {}
    for w in approved.get("accepted_warnings") or []:
        f, code = w.get("field"), w.get("code")
        pair = re.search(r"(\d{4}-\d{2}-\d{2}) -> (\d{4}-\d{2}-\d{2})", w.get("message") or "")
        if code == "large_jump" and pair:
            jump[(f, pair.group(1), pair.group(2))].append(1)
        elif code == "definition_mix":
            mixed[f] = True
        elif code == "gaps_in_series":
            gaps[f] = True

    def caution(f, old=None, new=None):
        c = []
        if (f, old, new) in jump:
            c.append("the approved statements flag this jump as a possible row-definition change")
        if mixed.get(f):
            c.append("different source rows supply different periods of this line")
        if gaps.get(f):
            c.append("some periods are missing for this line")
        return ("Data caution: " + "; ".join(c) + ". Treat with care.") if c else ""

    facts = []

    def add(kind, key, text, kw="", **extra):
        facts.append({"id": f"fact:{kind}:{key}", "text": text, "kw": kw, "computed": True, "caution": bool(extra.get("c")),
                      **{k: v for k, v in extra.items() if k != "c"}})

    if unit_note:
        add("note", "units", unit_note, fields=[], periods=[])

    # 1. year-on-year change of every line
    for f, (name, words) in LABELS.items():
        s = ser.get(f)
        if not s:
            continue
        for i in range(len(periods) - 1):
            new, old = periods[i], periods[i + 1]
            a, b = s.get(old), s.get(new)
            if a is None or b is None:
                continue
            if a > 0 and b >= 0:
                ch = (b - a) / a
                verb = "rose" if ch > 0.0005 else "fell" if ch < -0.0005 else "was flat"
                body = (f"{name} {verb} {abs(ch) * 100:.1f}% from {lab(old)} to {lab(new)} ({m(f, old)} to {m(f, new)})." if verb != "was flat"
                        else f"{name} was flat from {lab(old)} to {lab(new)} ({m(f, old)} to {m(f, new)}).")
            else:
                verb = "rose" if b > a else "fell" if b < a else "was flat"
                body = f"{name} {verb} from {m(f, old)} in {lab(old)} to {m(f, new)} in {lab(new)}, a swing of {_delta(b - a, mult, cur)}."
            c = caution(f, old, new)
            kw = f"{words} " + ("increase increased growth grew rise" if verb == "rose" else "decrease decline fell dropped" if verb == "fell" else "")
            add("yoy", f"{f}:{new}", f"{body} {c}".strip(), kw=kw, fields=[f], periods=[old, new], c=c)

    # 2. ratios from fin_metrics, if it is importable
    try:
        import fin_metrics
        for name, mt in fin_metrics.compute(approved).items():
            if not mt["available"]:
                continue
            fmt = (lambda v: "n/a" if v is None else (f"{v * 100:.1f}%" if name in PCT_METRICS else f"{v:.2f}x"))
            hist = ", ".join(f"{lab(p)} {fmt(mt['history'].get(p))}" for p in periods)
            needs = fin_metrics.METRIC_DEFS.get(name, ([],))[0]
            flagged = [f for f in needs if mixed.get(f) or any(k[0] == f for k in jump)]
            c = (("Data caution: this ratio uses " + ", ".join(flagged) + ", which the approved statements flag as possibly changing "
                  "definition between periods. Treat with care.") if flagged else "")
            add("ratio", name, f"{name.replace('_', ' ').capitalize()} ({mt['definition']}): {hist}. Trend {mt['trend'].replace('_', ' ')}. {c}".strip(),
                kw=name.replace("_", " ") + " ratio margin", fields=[name], periods=list(periods), c=c)
    except Exception:
        pass

    rev, cogs, oi = ser.get("revenue", {}), ser.get("cogs", {}), ser.get("operating_income", {})
    gp = ser.get("gross_profit") or {p: (rev[p] - cogs[p]) for p in periods if rev.get(p) is not None and cogs.get(p) is not None}

    # 3. cost of sales as a share of revenue
    share = {p: cogs[p] / rev[p] for p in periods if rev.get(p) and cogs.get(p) is not None}
    if share:
        add("share", "cogs_revenue", "Cost of sales as a share of revenue (cost of goods sold cost of revenue): "
            + ", ".join(f"{lab(p)} {share[p] * 100:.1f}%" for p in periods if p in share) + ".",
            kw="cost of goods sold cost of revenue costs margin ratio what if lever", fields=["cogs", "revenue"], periods=list(share))

    # 4. where an operating-income change came from
    for i in range(len(periods) - 1):
        new, old = periods[i], periods[i + 1]
        if None in (oi.get(new), oi.get(old), gp.get(new), gp.get(old), rev.get(new), rev.get(old), cogs.get(new), cogs.get(old)):
            continue
        d_oi, d_gp = oi[new] - oi[old], gp[new] - gp[old]
        below_new, below_old = gp[new] - oi[new], gp[old] - oi[old]
        add("drivers", new, f"Operating income changed by {_delta(d_oi, mult, cur)} from {lab(old)} to {lab(new)} ({m('operating_income', old)} to {m('operating_income', new)}). "
            f"Gross profit changed by {_delta(d_gp, mult, cur)} (revenue {_delta(rev[new] - rev[old], mult, cur)}, cost of sales {_delta(cogs[new] - cogs[old], mult, cur)}). "
            f"Costs below gross profit changed by {_delta(below_new - below_old, mult, cur)}.",
            kw="operating income operating profit loss earnings drivers decline fell increase gross profit revenue cost of sales costs", fields=["operating_income", "gross_profit", "revenue", "cogs"], periods=[old, new])

    # 5. what-if arithmetic on the latest period (a calculation, not a forecast)
    if periods and rev.get(periods[0]) and cogs.get(periods[0]) is not None and oi.get(periods[0]) is not None:
        p0 = periods[0]
        one_pt = rev[p0] * 0.01
        add("whatif", "one_point", f"In {lab(p0)}, each percentage point of cost of sales as a share of revenue equals {_money(one_pt, mult, cur)} of operating income "
            f"(each percentage point of the cost ratio). This is arithmetic on the approved statements, not a forecast.",
            kw="what if would have been lever sensitivity cost of sales share of revenue operating income", fields=["cogs", "revenue", "operating_income"], periods=[p0])
        for p in periods[1:4]:
            if p not in share:
                continue
            new_cogs = share[p] * rev[p0]
            new_oi = oi[p0] + (cogs[p0] - new_cogs)
            add("whatif", p, f"What if cost of sales had been {share[p] * 100:.1f}% of revenue (the {lab(p)} level) instead of {share[p0] * 100:.1f}% in {lab(p0)}: "
                f"operating income would have been {_money(new_oi, mult, cur)} instead of {m('operating_income', p0)} "
                f"(operating margin {new_oi / rev[p0] * 100:.1f}% instead of {oi[p0] / rev[p0] * 100:.1f}%). "
                f"This is arithmetic on the approved statements, not a forecast.",
                kw="what if would have been lever sensitivity cost of sales share of revenue operating income", fields=["cogs", "revenue", "operating_income"], periods=[p, p0])
    return facts


class FactIndex:
    def __init__(self, facts: list[dict], path: Path, sha: str):
        self.facts = {f["id"]: f for f in facts}
        self.path, self.sha256 = Path(path), sha
        self.index = ArticleIndex([{"event_id": f["id"], "company": "", "start": 0, "end": len(f["text"]), "text": f["text"] + " " + f.get("kw", ""),
                                    "title": "", "source": "financial statements", "url": "", "published_at": "",
                                    "content_hash": "", "url_confirmed": False} for f in facts])

    @staticmethod
    def find_approved(items, draft_path=None):
        for it in items or []:
            if it.get("kind") == "financial":
                for e in it.get("evidence") or []:
                    f = e.get("file")
                    if f and Path(str(f)).is_file():
                        return Path(f)
        if draft_path:
            p = Path(draft_path).resolve()
            for root in (p.parent.parent.parent / "finance", p.parent.parent / "finance"):
                hits = sorted(root.glob("*/approved_financials.json")) if root.is_dir() else []
                if hits:
                    return hits[-1]
        return None

    @classmethod
    def from_items(cls, items, draft_path=None):
        path = cls.find_approved(items, draft_path)
        if not path:
            return None
        raw = path.read_bytes()
        facts = build_facts(json.loads(raw.decode("utf-8-sig")))
        return cls(facts, path, hashlib.sha256(raw).hexdigest()) if facts else None

    def search(self, query: str, k: int = 3, exclude=None, min_rel: float = 0.25, min_score: float = 2.5) -> list[dict]:
        skip = {key[0] for key in (exclude or ())}
        out = []
        for h in self.index.search(query, k=k + len(skip), min_rel=min_rel, max_per_article=1):
            if h["event_id"] in skip or h["score"] < min_score:
                continue
            out.append({**self.facts[h["event_id"]], "score": h["score"]})
            if len(out) >= k:
                break
        return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    ap = Path(sys.argv[1])
    fi = FactIndex(build_facts(json.loads(ap.read_text(encoding="utf-8-sig"))), ap, "")
    print(f"{len(fi.facts)} facts")
    for h in (fi.search(sys.argv[2], k=5) if len(sys.argv) > 2 else []):
        print(f"\n{h['score']} {h['id']}\n{h['text']}")
