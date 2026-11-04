"""sec_peers.py - peer ratios from SEC filings, for the Comparison tab, the SWOT builder and the Five Whys.

Called by backend/jobs.py when the user enters peer tickers:
  python sec_peers.py --tickers GM TM --outdir <job>/peers --cache-dir <jobs>/_sec_cache
                      --target-name Ford --target-approved <job>/finance/<run>/approved_financials.json

For every peer it reads the annual (10-K) US-GAAP numbers from the SEC "company facts" API, maps them to the same line
items as the approved statements, and computes the same ratios with fin_metrics.py, so definitions match. Ratios are
unit-free, so thousands/millions/billions in the approved file do not matter.

Writes (in --outdir):
  peers.json            {"peers": {name: {metric: value}}}        read by swot_builder.py (--peers)
  peer_comparison.json  target, peers, median, vs_median          read by evidence_peers.py and the Comparison tab
  peers_status.json     one entry per ticker: ok / why not

Needs SEC_USER_AGENT in the environment, for example "Your Name you@example.com" (the SEC requires it).
A peer that cannot be read (unknown ticker, foreign/IFRS filer, no 10-K data) is reported and skipped; the rest still run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median

import httpx

import fin_metrics

def _load_dotenv():
    here = Path(__file__).resolve().parent
    for d in (Path.cwd(), here, here.parent):
        f = d / ".env"
        if f.is_file():
            for line in f.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip().removeprefix("export ").strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v


_load_dotenv()
UA = os.environ.get("SEC_USER_AGENT", "").strip()
ALIGN_DAYS = 100        # a peer fiscal year ending within ~3 months of the target's period counts as aligned
KEEP = 5                # fiscal years kept per peer (growth ratios need the year before)

# field -> (US-GAAP tags in priority order, is it a balance-sheet "instant" value?)
TAGS = {
    "revenue": (["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"], False),
    "cogs": (["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"], False),
    "gross_profit": (["GrossProfit"], False),
    "operating_income": (["OperatingIncomeLoss"], False),
    "interest_expense": (["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"], False),
    "net_income": (["NetIncomeLoss", "ProfitLoss"], False),
    "current_assets": (["AssetsCurrent"], True),
    "current_liabilities": (["LiabilitiesCurrent"], True),
    "cash": (["CashAndCashEquivalentsAtCarryingValue",
              "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], True),
    "short_investments": (["ShortTermInvestments", "MarketableSecuritiesCurrent"], True),
    "inventory": (["InventoryNet"], True),
    "receivables": (["AccountsReceivableNetCurrent"], True),
    "ppe": (["PropertyPlantAndEquipmentNet"], True),
    "total_assets": (["Assets"], True),
    "total_liabilities": (["Liabilities"], True),
    "equity": (["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], True),
    "retained_earnings": (["RetainedEarningsAccumulatedDeficit"], True),
    "short_debt": (["DebtCurrent", "LongTermDebtCurrent", "ShortTermBorrowings"], True),
    "long_debt": (["LongTermDebtNoncurrent", "LongTermDebt"], True),
}


class Sec:
    def __init__(self, cache_dir):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(headers={"User-Agent": UA}, timeout=90, follow_redirects=True)
        self.last = 0.0

    def get(self, url: str, name: str, max_age_days: float):
        path = self.cache / name
        if path.exists() and time.time() - path.stat().st_mtime < max_age_days * 86400:
            return json.loads(path.read_text(encoding="utf-8"))
        for attempt in range(4):
            gap = 0.2 - (time.monotonic() - self.last)
            if gap > 0:
                time.sleep(gap)
            self.last = time.monotonic()
            r = self.client.get(url)
            if r.status_code == 200:
                path.write_text(r.text, encoding="utf-8")
                return r.json()
            if r.status_code == 404:
                raise LookupError("the SEC has no company-facts data for this company")
            if r.status_code in (403, 429, 503):
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
        raise RuntimeError("the SEC kept refusing requests (check SEC_USER_AGENT)")


def _d(s: str) -> date:
    return date.fromisoformat(s)


def annual_series(gaap: dict, tags: list, instant: bool):
    """{fiscal_year_end: value} from 10-K facts; the first tag that has a given year wins."""
    per_tag = []
    for tag in tags:
        node = gaap.get(tag)
        if not node:
            continue
        best = {}
        for e in node.get("units", {}).get("USD", []):
            if not str(e.get("form", "")).startswith("10-K") or not isinstance(e.get("val"), (int, float)):
                continue
            end = e.get("end")
            if not end:
                continue
            if not instant:
                start = e.get("start")
                if not start:
                    continue
                try:
                    days = (_d(end) - _d(start)).days
                except ValueError:
                    continue
                if not 350 <= days <= 380:      # a full fiscal year (52/53-week years included)
                    continue
            if end not in best or e.get("filed", "") > best[end][0]:
                best[end] = (e.get("filed", ""), e["val"])
        per_tag.append((tag, {k: v[1] for k, v in best.items()}))
    out, used = {}, set()
    for tag, s in per_tag:
        for end, val in s.items():
            if end not in out:
                out[end] = val
                used.add(tag)
    return out, used


def build_dataset(name: str, facts: dict):
    gaap = (facts.get("facts") or {}).get("us-gaap")
    if not gaap:
        raise ValueError("no US-GAAP data (a foreign or IFRS filer?)")
    ser, tags_used = {}, {}
    for f, (tags, inst) in TAGS.items():
        s, used = annual_series(gaap, tags, inst)
        if s:
            ser[f], tags_used[f] = s, sorted(used)
    notes, partial = [], set()
    if "gross_profit" not in ser and "revenue" in ser and "cogs" in ser:
        ser["gross_profit"] = {p: ser["revenue"][p] - ser["cogs"][p] for p in ser["revenue"] if p in ser["cogs"]}
        notes.append("gross profit = revenue - cost of sales")
    if "total_liabilities" not in ser and "total_assets" in ser and "equity" in ser:
        ser["total_liabilities"] = {p: ser["total_assets"][p] - ser["equity"][p] for p in ser["total_assets"] if p in ser["equity"]}
        notes.append("total liabilities = total assets - equity (includes minority interests)")
    sd, ld = ser.get("short_debt", {}), ser.get("long_debt", {})
    if sd or ld:
        td = {}
        for p in set(sd) | set(ld):
            td[p] = sd.get(p, 0) + ld.get(p, 0)
            if p not in sd or p not in ld:
                partial.add("total_debt")
        ser["total_debt"] = td
        if "total_debt" in partial:
            notes.append("total debt is missing its short-term or long-term part in some years: lower bound")
    if not ser.get("total_assets"):
        raise ValueError("no annual total assets found in 10-K filings")
    periods = sorted(ser["total_assets"], reverse=True)[:KEEP]
    fields = {f: {"series": s, "partial": f in partial} for f, s in ser.items()}
    return {"company": name, "periods": periods, "frequency": "annual", "fields": fields}, tags_used, notes


def nice_name(n: str) -> str:
    return n.title() if n and n == n.upper() else (n or "")


def vs(latest, ref, hib):
    gap = (latest - ref) / max(abs(ref), 0.05)
    pos = "in_line" if abs(gap) < 0.10 else ("above_peers" if gap > 0 else "below_peers")
    return {"position": pos, "gap": round(gap, 3), "peer_value": ref,
            "favourable": None if pos == "in_line" else ((pos == "above_peers") == bool(hib))}


def run(tickers, outdir, target_name, target_path, sec) -> list:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    target = json.loads(Path(target_path).read_text(encoding="utf-8-sig")) if target_path else None
    t_latest = target["periods"][0] if target and target.get("periods") else None
    status, peers = [], []
    try:
        tick = sec.get("https://www.sec.gov/files/company_tickers.json", "company_tickers.json", 7)
    except Exception as e:
        tick = None
        status = [{"ticker": t, "name": t, "ok": False, "status": "failed",
                   "message": f"could not load the SEC ticker list: {e}"} for t in tickers]
    for t in (tickers if tick else []):
        entry = {"ticker": t.upper(), "name": t.upper(), "ok": False, "status": "failed", "message": ""}
        try:
            row = next((v for v in tick.values() if str(v["ticker"]).upper() == t.upper()), None)
            if not row:
                raise LookupError("ticker not found in the SEC list (US-listed companies only)")
            cik = int(row["cik_str"])
            facts = sec.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", f"facts_{cik}.json", 3)
            name = nice_name(facts.get("entityName") or row.get("title") or t)
            entry.update(name=name, cik=cik)
            ds, tags_used, notes = build_dataset(name, facts)
            mt = fin_metrics.compute(ds)
            if t_latest:
                used = min(ds["periods"], key=lambda p: abs((_d(p) - _d(t_latest)).days))
                aligned = abs((_d(used) - _d(t_latest)).days) <= ALIGN_DAYS
            else:
                used, aligned = ds["periods"][0], True
            values = {m: r["history"].get(used) for m, r in mt.items() if r["history"].get(used) is not None}
            if not values:
                raise ValueError("no ratio could be computed for the chosen fiscal year")
            peers.append({"name": name, "ticker": t.upper(), "cik": cik, "period_used": used, "aligned": aligned,
                          "values_used": values, "periods_available": ds["periods"], "tags_used": tags_used, "notes": notes})
            entry.update(ok=True, status="ok", period_used=used, aligned=aligned, metrics=len(values),
                         message="; ".join(notes))
            print(f"{t.upper()}: {name}, fiscal year ending {used}, {len(values)} ratios" + ("" if aligned else " (not aligned with the target period)"), flush=True)
        except Exception as e:
            entry["message"] = f"{type(e).__name__}: {e}"
            print(f"!! {t.upper()}: {entry['message']}", flush=True)
        status.append(entry)

    med = {}
    for m in {k for p in peers for k in p["values_used"]}:
        vals = [p["values_used"][m] for p in peers if m in p["values_used"]]
        med[m] = {"value": median(vals), "n_peers": len(vals)}
    tgt = {"company": target_name or (target or {}).get("company") or "The company", "metrics": {}}
    if target:
        for m, r in fin_metrics.compute(target).items():
            if r["latest"] is None or not r.get("latest_is_current_period", True):
                continue
            row = {"latest": r["latest"], "as_of": r["as_of"], "trend": r["trend"], "higher_is_better": r["higher_is_better"],
                   "uses_partial_input": r["uses_partial_input"], "definition": r["definition"]}
            if m in med and r["higher_is_better"] is not None:
                row["vs_median"] = vs(r["latest"], med[m]["value"], r["higher_is_better"])
            tgt["metrics"][m] = row
    now = datetime.now(timezone.utc).isoformat()
    comparison = {"stage": "peer_comparison", "generated_at": now, "source": "SEC company facts (annual 10-K, US-GAAP)",
                  "target": tgt, "peers": peers, "median": med,
                  "warnings": ["Peer figures are company-wide annual numbers from SEC filings; the target may be reported on a different basis.",
                               "Fiscal years can end months apart (see 'aligned'). Debt definitions can differ between companies."]}
    (outdir / "peer_comparison.json").write_text(json.dumps(comparison, indent=2), encoding="utf-8")
    (outdir / "peers.json").write_text(json.dumps({"peers": {p["name"]: p["values_used"] for p in peers},
                                                   "generated_at": now, "source": comparison["source"]}, indent=2), encoding="utf-8")
    (outdir / "peers_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    print(f"peer comparison ready for {len(peers)} of {len(tickers)} peers -> {outdir}", flush=True)
    return status


def main() -> None:
    ap = argparse.ArgumentParser(description="Peer ratios from SEC filings")
    ap.add_argument("--tickers", nargs="+", required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--cache-dir", type=Path, default=Path("sec_cache"))
    ap.add_argument("--target-name", default="")
    ap.add_argument("--target-approved", type=Path, default=None)
    a = ap.parse_args()
    if not UA:
        a.outdir.mkdir(parents=True, exist_ok=True)
        msg = 'SEC_USER_AGENT is not set. Add SEC_USER_AGENT="Your Name you@example.com" to .env'
        (a.outdir / "peers_status.json").write_text(json.dumps(
            [{"ticker": t.upper(), "name": t.upper(), "ok": False, "status": "failed", "message": msg} for t in a.tickers], indent=2), encoding="utf-8")
        print("!! " + msg, flush=True)
        return
    run(a.tickers, a.outdir, a.target_name, a.target_approved, Sec(a.cache_dir))


if __name__ == "__main__":
    main()
