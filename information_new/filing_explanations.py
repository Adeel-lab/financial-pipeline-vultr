"""filing_explanations.py - management's own explanations of results, quoted from the latest 10-K / 10-Q.

Run : python filing_explanations.py --company Ford --ticker F --outdir jobs/<id>/filings
Env : SEC_USER_AGENT="Your Name you@example.com" (required by the SEC), SEC_CACHE_DIR (optional)
No LLM is used, so there are no rate limits. Every quote is a sentence copied from the filing text.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

UA = os.environ.get("SEC_USER_AGENT", "").strip()
CACHE = Path(os.environ.get("SEC_CACHE_DIR", "sec_cache"))
MAX_MDA = 150_000
LABEL = "management explanation"

TOPICS = {
    "revenue": r"\b(revenues?|net sales|total sales|wholesales?|volumes?|pricing)\b",
    "cost of sales": r"\b(cost of sales|cost of goods sold|material costs?|commodit\w+|warranty|raw materials?)\b",
    "operating income": r"\b(operating (?:income|profit|loss)|EBIT|segment (?:profit|income))\b",
    "net income": r"\bnet (?:income|loss|earnings)\b",
    "operating expenses": r"\b(selling, general|SG&A|administrative expenses?|research and development|advertising)\b",
    "debt and interest": r"\b(interest expense|debt|borrowings|credit facilit\w+|notes payable)\b",
    "cash and liquidity": r"\b(liquidity|cash flows?|cash and cash equivalents|operating activities|capital expenditures?)\b",
    "external factors": r"\b(tariffs?|regulat\w+|emissions?|recalls?|inflation|foreign currency|exchange rates?)\b",
}
TOPIC_RE = {k: re.compile(v, re.I) for k, v in TOPICS.items()}
CUES = re.compile(r"\b(due to|primarily|driven by|resulted from|result of|reflect\w*|attributable|compared with|"
                  r"compared to|higher|lower|increase[sd]?|decrease[sd]?|partially offset|unfavorable|favorable|"
                  r"as a result|mainly|largely)\b", re.I)
START = {"10-K": re.compile(r"item\s*7\.?\s*[-\u2013\u2014:.]?\s*management's discussion and analysis", re.I),
         "10-Q": re.compile(r"item\s*2\.?\s*[-\u2013\u2014:.]?\s*management's discussion and analysis", re.I)}
END = {"10-K": re.compile(r"\n\s*item\s*(?:7a|8)\b", re.I),
       "10-Q": re.compile(r"\n\s*item\s*(?:3|4)\b", re.I)}

_last = 0.0


def fetch(client: httpx.Client, url: str, name: str, max_age_days: float | None = None) -> str:
    global _last
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / name
    if path.exists() and (max_age_days is None or time.time() - path.stat().st_mtime < max_age_days * 86400):
        return path.read_text(encoding="utf-8", errors="replace")
    for attempt in range(4):
        gap = 0.2 - (time.monotonic() - _last)
        if gap > 0:
            time.sleep(gap)
        _last = time.monotonic()
        r = client.get(url)
        if r.status_code == 200:
            path.write_text(r.text, encoding="utf-8")
            return r.text
        if r.status_code in (403, 429, 503):
            time.sleep(2 ** attempt * 2)
            continue
        r.raise_for_status()
    raise RuntimeError(f"SEC request kept failing: {url}")


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<ix:header>.*?</ix:header>|<script.*?</script>|<style.*?</style>", " ", raw)
    raw = re.sub(r"(?i)</(?:p|div|tr|li|h\d)>|<br\s*/?>", "\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(raw).replace("\xa0", " ").replace("\u2019", "'")
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    return "\n".join(l for l in lines if l)


def mda_section(text: str, form: str) -> str:
    best = ""
    for m in START[form].finditer(text):
        e = END[form].search(text, m.end())
        seg = text[m.start(): e.start() if e else m.start() + MAX_MDA]
        if len(seg) > len(best):
            best = seg[:MAX_MDA]
    return best if len(best) >= 3000 else text[:MAX_MDA]


def sentences(section: str) -> list[str]:
    out = []
    for line in section.split("\n"):
        if len(line) < 80 or sum(c.isalpha() for c in line) / len(line) < 0.6:
            continue
        out += [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line)]
    return [s for s in out if 60 <= len(s) <= 450]


def pick(sents: list[str], topic_re: re.Pattern, n: int = 2) -> list[str]:
    scored = []
    for s in sents:
        k, c = len(topic_re.findall(s)), len(CUES.findall(s))
        if k and c:
            scored.append((k * 2 + min(c, 3), s))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, s in scored:
        key = s[:80].lower()
        if key not in seen:
            seen.add(key)
            out.append(s)
        if len(out) == n:
            break
    return out


def explain_filing(text: str, form: str, meta: dict) -> list[dict]:
    sents = sentences(mda_section(text, form))
    norm = re.sub(r"\s+", " ", text).lower()
    items = []
    for topic, rx in TOPIC_RE.items():
        for s in pick(sents, rx):
            items.append({"topic": topic, "quote": s, "label": LABEL,
                          "verified": re.sub(r"\s+", " ", s).lower() in norm, **meta})
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description="Management explanations from the latest 10-K / 10-Q")
    ap.add_argument("--company", required=True)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--n-annual", type=int, default=2, help="how many recent 10-Ks (default 2)")
    a = ap.parse_args()
    if not UA:
        sys.exit('Set SEC_USER_AGENT in .env, e.g. SEC_USER_AGENT="Your Name you@example.com"')
    a.outdir.mkdir(parents=True, exist_ok=True)
    errors, items, filings = [], [], []
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as c:
        tick = json.loads(fetch(c, "https://www.sec.gov/files/company_tickers.json", "company_tickers.json", 7))
        row = next((v for v in tick.values() if v["ticker"].upper() == a.ticker.upper()), None)
        if not row:
            sys.exit(f"Ticker {a.ticker} not found in the SEC list (US-listed companies only).")
        cik = int(row["cik_str"])
        sub = json.loads(fetch(c, f"https://data.sec.gov/submissions/CIK{cik:010d}.json", f"sub_{cik}.json", 1))
        rec = sub["filings"]["recent"]
        rows = list(zip(rec["form"], rec["accessionNumber"], rec["primaryDocument"], rec["filingDate"], rec["reportDate"]))
        picks = [r for r in rows if r[0] == "10-K"][:a.n_annual] + [r for r in rows if r[0] == "10-Q"][:1]
        for form, acc, doc, filed, period in picks:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
            meta = {"form": form, "period": period, "filing_date": filed, "url": url}
            try:
                text = html_to_text(fetch(c, url, f"{cik}_{acc}.htm"))
                got = explain_filing(text, form, meta)
                items += got
                filings.append({**meta, "explanations": len(got)})
                print(f"{form} {period}: {len(got)} explanations", flush=True)
            except Exception as e:
                errors.append(f"{form} {period}: {type(e).__name__}: {e}")
                print(f"!! {form} {period}: {e}", flush=True)
    result = {"company": a.company, "ticker": a.ticker.upper(), "cik": cik,
              "generated_at": datetime.now(timezone.utc).isoformat(), "label": LABEL,
              "filings": filings, "items": items, "errors": errors}
    (a.outdir / "filing_explanations.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    md = [f"# {a.company}: what management says explains the results", ""]
    for it in items:
        md += [f"**{it['topic']}** ({it['form']}, period {it['period']})", f"> {it['quote']}", f"[source]({it['url']})", ""]
    (a.outdir / "filing_explanations.md").write_text("\n".join(md), encoding="utf-8")
    print(f"saved {len(items)} explanations -> {a.outdir}")


if __name__ == "__main__":
    main()
