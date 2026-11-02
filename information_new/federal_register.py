"""federal_register.py - official U.S. government documents about the company and its industry.

Searches the Federal Register (rules, proposed rules, notices, presidential documents) through its free JSON API:
  1. documents that NAME the company (for example a recall petition, an exemption, a trade action), and
  2. documents on the regulatory topics of the company's industry (topics come from its SEC industry code, or --topic).
Every quote is a sentence copied from the document's own title or abstract. A document shows what the government
published or proposed. It never shows, by itself, that this affected the company.

Run : python federal_register.py --company "Ford" --ticker F --outdir jobs/<id>/regulatory
Env : SEC_USER_AGENT (only used to look up the company's SEC name and industry code from the ticker; optional)
Out : federal_register.json, federal_register.md
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx


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
API = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = ["document_number", "title", "type", "action", "abstract", "agencies", "publication_date",
          "effective_on", "comments_close_on", "html_url"]
COMPANY_TYPES = ["RULE", "PRORULE", "NOTICE", "PRESDOCU"]
TOPIC_TYPES = ["RULE", "PRORULE", "PRESDOCU"]
MAX_COMPANY, MAX_PER_TOPIC, MAX_TOTAL = 10, 5, 40
LABEL = "government document"

# SEC industry code (SIC) -> regulatory topics. A short starter list: add or override with --topic.
SIC_TOPICS = [
    ({3711, 3713, 3714, 3715, 3716}, ["motor vehicle emissions", "fuel economy standards", "motor vehicle safety standard",
                                      "automobile tariffs", "electric vehicle"]),
    ({2834, 2835, 2836}, ["drug approval", "drug pricing", "biologics license", "Medicare drug"]),
    ({6021, 6022, 6035, 6036}, ["bank capital requirements", "bank holding company", "deposit insurance", "consumer financial protection"]),
    ({4512, 4513, 4522}, ["aviation safety", "air carrier", "airline passenger", "aircraft emissions"]),
    ({1311, 1381, 2911}, ["oil and gas leasing", "methane emissions", "renewable fuel standard", "refinery"]),
    ({3674, 3672, 3571}, ["semiconductor export controls", "export administration regulations", "CHIPS"]),
    ({7370, 7371, 7372, 7374}, ["artificial intelligence", "data privacy", "cybersecurity"]),
    ({4911, 4931}, ["electric reliability", "power plant emissions", "transmission"]),
]
_last = 0.0


def clean(s) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))).strip()


def pace():
    global _last
    gap = 0.3 - (time.monotonic() - _last)
    if gap > 0:
        time.sleep(gap)
    _last = time.monotonic()


def search_docs(client, term, types, since, per_page=20) -> list:
    params = [("conditions[term]", f'"{term}"'), ("conditions[publication_date][gte]", since),
              ("order", "newest"), ("per_page", str(per_page))]
    params += [("conditions[type][]", t) for t in types] + [("fields[]", f) for f in FIELDS]
    for attempt in range(4):
        pace()
        r = client.get(API, params=params)
        if r.status_code == 200:
            return r.json().get("results") or []
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt * 2)
            continue
        raise RuntimeError(f"Federal Register API HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("Federal Register API kept failing")


def sentences(text: str) -> list:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text) if len(s.strip()) > 20]


def needle_hit(text: str, needles: list) -> bool:
    low = text.lower()
    for n in needles:
        if " " in n or len(n) > 8:
            if n in low:
                return True
        elif re.search(rf"\b{re.escape(n)}\b", low):
            return True
    return False


def make_item(doc, scope, matched, needles, topic_words=None):
    title, abstract = clean(doc.get("title")), clean(doc.get("abstract"))
    blob = f"{title} {abstract}"
    if scope == "company":
        if not needle_hit(blob, needles):
            return None
    else:
        low = blob.lower()
        if not topic_words or not all(w in low for w in topic_words):
            return None
    quote, src = None, None
    keys = needles if scope == "company" else (topic_words or [])
    for text, name in ((abstract, "abstract"), (title, "title")):
        if not text:
            continue
        ss = sentences(text) or [text]
        hit = next((s for s in ss if any(k in s.lower() for k in keys)), None)
        if hit:
            quote, src = hit[:450], name
            break
    if not quote:
        quote, src = (abstract[:450], "abstract") if abstract else (title[:450], "title")
    verified = re.sub(r"\s+", " ", quote).lower() in re.sub(r"\s+", " ", blob).lower()
    ag = [a.get("name") or a.get("raw_name") or "" for a in (doc.get("agencies") or []) if isinstance(a, dict)]
    return {"document_number": doc.get("document_number"), "title": title, "type": doc.get("type"), "action": clean(doc.get("action")),
            "agencies": [a for a in ag if a], "publication_date": doc.get("publication_date"),
            "effective_on": doc.get("effective_on"), "comments_close_on": doc.get("comments_close_on"),
            "url": doc.get("html_url"), "scope": scope, "topic": matched, "quote": quote, "quote_from": src,
            "verified": verified, "label": LABEL}


def sec_profile(ticker, cache):
    """(official company name, SIC code, SIC description) from the SEC, or (None, None, None)."""
    if not (ticker and UA):
        return None, None, None
    cache.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as c:
        tp = cache / "company_tickers.json"
        if not tp.exists() or time.time() - tp.stat().st_mtime > 7 * 86400:
            pace()
            tp.write_text(c.get("https://www.sec.gov/files/company_tickers.json").text, encoding="utf-8")
        tick = json.loads(tp.read_text(encoding="utf-8"))
        row = next((v for v in tick.values() if str(v["ticker"]).upper() == ticker.upper()), None)
        if not row:
            return None, None, None
        cik = int(row["cik_str"])
        sp = cache / f"sub_{cik}.json"
        if not sp.exists() or time.time() - sp.stat().st_mtime > 86400:
            pace()
            sp.write_text(c.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json").text, encoding="utf-8")
        sub = json.loads(sp.read_text(encoding="utf-8"))
    sic = sub.get("sic")
    return (sub.get("name") or row.get("title")), (int(sic) if str(sic).isdigit() else None), sub.get("sicDescription")


def official_variants(name: str) -> list:
    n = " ".join(w.capitalize() if w.isupper() and len(w) > 3 else w for w in name.replace(",", "").split())
    n = n.title() if name.isupper() else n
    out = [n]
    for short, full in ((" Co", " Company"), (" Corp", " Corporation"), (" Inc", " Incorporated")):
        if n.endswith(short):
            out.append(n[: -len(short)] + full)
    return list(dict.fromkeys(out))


def run(company, aliases, ticker, topics, outdir, days, cache, client=None):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    notes, errors, items, seen = [], [], [], set()
    since = (date.today() - timedelta(days=days)).isoformat()
    sec_name = sic = sic_desc = None
    try:
        sec_name, sic, sic_desc = sec_profile(ticker, Path(cache))
    except Exception as e:
        notes.append(f"SEC lookup skipped: {type(e).__name__}: {e}")
    names = list(dict.fromkeys([company] + list(aliases or []) + (official_variants(sec_name) if sec_name else [])))
    names = [n for n in names if n and len(n) >= 3]
    multi = [n for n in names if " " in n.strip()]
    if not multi:
        notes.append("The company name is a single common word and no longer official name was found: results may include unrelated documents. Give the ticker or an alias.")
    pool = multi or names           # a one-word name like "Ford" is used only if nothing longer is known
    needles = list(dict.fromkeys(n.lower() for n in pool))
    needles = [n for n in needles if not any(o != n and n.startswith(o) for o in needles)]   # "ford motor co" also matches "...company"
    if not topics:
        for codes, tl in SIC_TOPICS:
            if sic in codes:
                topics = tl
                break
        if not topics:
            notes.append(f"No topic list for industry code {sic} ({sic_desc}); only documents naming the company were searched. Add topics with --topic.")
    own = client or httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": UA or "FinAnalyst"})
    try:
        for term in (multi or names)[:3]:
            try:
                got = 0
                for d in search_docs(own, term, COMPANY_TYPES, since):
                    if d.get("document_number") in seen:
                        continue
                    it = make_item(d, "company", term, needles)
                    if it:
                        seen.add(d["document_number"]); items.append(it); got += 1
                        if got >= MAX_COMPANY:
                            break
                print(f"company '{term}': {got} documents", flush=True)
            except Exception as e:
                errors.append(f"company '{term}': {type(e).__name__}: {e}")
        for topic in topics:
            words = [w for w in re.findall(r"[a-z0-9]{3,}", topic.lower())]
            try:
                got = 0
                for d in search_docs(own, topic, TOPIC_TYPES, since):
                    if d.get("document_number") in seen:
                        continue
                    it = make_item(d, "industry", topic, [], words)
                    if it:
                        seen.add(d["document_number"]); items.append(it); got += 1
                        if got >= MAX_PER_TOPIC:
                            break
                print(f"topic '{topic}': {got} documents", flush=True)
            except Exception as e:
                errors.append(f"topic '{topic}': {type(e).__name__}: {e}")
    finally:
        if client is None:
            own.close()
    items = items[:MAX_TOTAL]
    result = {"company": company, "ticker": (ticker or "").upper() or None, "sec_name": sec_name, "sic": sic, "sic_description": sic_desc,
              "generated_at": datetime.now(timezone.utc).isoformat(), "since": since, "label": LABEL,
              "topics_searched": list(topics), "items": items, "notes": notes, "errors": errors}
    (outdir / "federal_register.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    md = [f"# {company}: Federal Register documents", ""]
    for it in items:
        md += [f"**{it['type']}** {it['publication_date']} | {', '.join(it['agencies'])} | {it['scope']}: {it['topic']}",
               it["title"], f"> {it['quote']}", f"[source]({it['url']})", ""]
    (outdir / "federal_register.md").write_text("\n".join(md), encoding="utf-8")
    print(f"saved {len(items)} documents -> {outdir}", flush=True)
    return result


def main():
    ap = argparse.ArgumentParser(description="Federal Register documents about a company and its industry")
    ap.add_argument("--company", required=True)
    ap.add_argument("--alias", action="append", default=[])
    ap.add_argument("--ticker", default="")
    ap.add_argument("--topic", action="append", default=[])
    ap.add_argument("--outdir", type=Path, required=True)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cache-dir", type=Path, default=Path("sec_cache"))
    a = ap.parse_args()
    run(a.company, a.alias, a.ticker, a.topic, a.outdir, a.days, a.cache_dir)


if __name__ == "__main__":
    main()
