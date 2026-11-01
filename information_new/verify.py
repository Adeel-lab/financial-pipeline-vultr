"""
Stage 4 - Verification: open resolved URLs in a headless browser, extract text,
screenshot, and write a storage-ready record per article. Works for any company.


Input : output/triage_<run>/resolved.json            (from resolve.py)
Output: output/verify_triage_<run>/articles/*.json, raw/*.html, screenshots/*.png,
        verify_report.json, verify_failed.json


Checks applied to every page: never a Google page, not a cookie/consent wall, not a paywall or
thin page, headline must match the page (status "title_mismatch"), no duplicate texts.
For a local-scope company each record also says whether the page names a chosen location.
Each saved record also stores verification.final_url: the page the browser actually ended on.


Needs: pip install playwright trafilatura httpx
       python -m playwright install chromium
Run  : python verify.py                (newest resolved.json)
       python verify.py --limit 5      (smoke test; clears this run's old output first)
"""
from __future__ import annotations


import argparse
import asyncio
import hashlib
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


from playwright.async_api import async_playwright, TimeoutError as PWTimeout


from company_profile import profile_from_dict, distinctive_words, location_hits


log = logging.getLogger("verify")


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
CONCURRENCY = 4
PAGE_TIMEOUT_MS = 25000
SETTLE_MS = 1200
MAX_TEXT = 20000
MAX_HTML = 400000
MAX_ALTERNATES = 2
MIN_TITLE_SCORE = 0.34
BLOCK_PATTERNS = ("doubleclick", "googlesyndication", "googletagmanager", "google-analytics",
                  "facebook", "twitter", "scorecardresearch", "quantserve", "criteo",
                  "taboola", "outbrain", "adservice", "adnxs", "pubmatic", "rubicon")


# Phrases that mean "you must subscribe". A page counts as a paywall only when the extracted text
# is short, because full articles on open sites often contain these words in menus or scripts.
PAYWALL_HINTS = ("subscribe to read", "sign in to continue", "create an account to read",
                 "this article is for subscribers", "limited free articles",
                 "register to continue reading", "already a subscriber", "subscribers only")
PAYWALL_MAX_TEXT = 2500        # hint found in the visible text and text shorter than this
PAYWALL_HTML_MAX_TEXT = 1000   # hint only in the page source and text shorter than this
# Google consent page text (anywhere) and generic cookie-wall wording (start of text only)
CONSENT_ANYWHERE = ("develop and improve new services", "before you continue to google",
                    "consent.google.com")
CONSENT_AT_START = ("we use cookies", "we and our partners", "we and our third-party vendors",
                    "accept all cookies", "cookie settings", "your privacy choices")
THIN_TEXT = 400


STOP = {"this", "that", "with", "from", "have", "will", "after", "over", "amid", "says", "said",
        "what", "when", "your", "they", "their", "about", "more", "than", "into", "here", "there",
        "which", "while", "could", "would", "just", "also", "news", "report", "reports", "company"}



def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()



def find_input_dir(arg: Path | None, base: Path) -> Path:
    if arg:
        return arg
    runs = [p for p in base.iterdir() if p.is_dir() and (p / "resolved.json").exists()]
    if not runs:
        sys.exit(f"No resolved.json found under {base}. Run resolve.py first.")
    return max(runs, key=lambda p: p.stat().st_mtime)



def url_allowed(url: str) -> tuple[bool, str]:
    """SSRF guard: only public http(s), no private/metadata hosts, never Google."""
    try:
        p = urlparse(url)
    except ValueError:
        return False, "bad_url"
    if p.scheme not in ("http", "https") or not p.hostname:
        return False, "bad_url"
    host = p.hostname.lower()
    if host.endswith("google.com"):
        return False, "google_page"
    if host in ("localhost", "metadata.google.internal") or host.endswith(".internal"):
        return False, "private_host"
    if re.match(r"^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host):
        return False, "private_host"
    return True, ""



def is_consent_text(text: str) -> bool:
    low = (text or "").lower()
    if any(h in low for h in CONSENT_ANYWHERE):
        return True
    head = low[:600]
    return any(h in head for h in CONSENT_AT_START)



def classify_failure(status: int | None, body: str, text: str) -> str:
    low = (body or "").lower()
    low_text = (text or "").lower()
    if status == 403:
        return "blocked"
    if status == 429:
        return "rate_limited"
    if status == 401:
        return "paywall"
    if is_consent_text(text):
        return "consent_wall"
    if len(text) < PAYWALL_MAX_TEXT and any(h in low_text for h in PAYWALL_HINTS):
        return "paywall"
    if len(text) < PAYWALL_HTML_MAX_TEXT and any(h in low for h in PAYWALL_HINTS):
        return "paywall"
    if status and status >= 500:
        return "server_error"
    if len(text) < THIN_TEXT:
        return "thin_content"
    return "ok"



def _toks(s: str, ignore: set[str] | None = None) -> set[str]:
    """Unicode-aware words (works for accented / non-English text); skips pure numbers."""
    out = set()
    for w in re.findall(r"\w{4,}", (s or "").lower()):
        if w.isdigit():
            continue
        if len(w) > 4 and w.endswith("s"):
            w = w[:-1]
        if w not in STOP and not (ignore and w in ignore):
            out.add(w)
    return out



def title_score(event_title: str, page_title: str, text: str, company_words: set[str]) -> float:
    """Share of the headline's meaningful words found in the fetched page (0..1)."""
    et = _toks(event_title, company_words)
    if len(et) < 2:
        return 1.0                      # headline too short to judge
    hay = _toks(page_title) | _toks((text or "")[:4000])
    return round(len(et & hay) / len(et), 3)



def extract_date(html: str) -> str | None:
    for pat in (r'"datePublished"\s*:\s*"([^"]+)"',
                r'property="article:published_time"\s+content="([^"]+)"',
                r'name="(?:date|publish-date|parsely-pub-date)"\s+content="([^"]+)"',
                r'<time[^>]+datetime="([^"]+)"'):
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None



async def fetch_page(ctx, sem, url: str, event_title: str, company_words: set[str],
                     shot_path: Path | None, min_title: float) -> dict:


    result = {"status": "error", "http_status": None, "text": "", "title": "", "date": None,
              "final_url": None}
    ok, why = url_allowed(url)
    if not ok:
        result["status"] = why
        return result
    async with sem:
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(SETTLE_MS)
            result["final_url"] = page.url
            result["http_status"] = resp.status if resp else None
            result["title"] = (await page.title() or "").strip()
            html = await page.content()
            result["date"] = extract_date(html)
            try:
                import trafilatura
                text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
            except Exception:
                text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S | re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
            result["text"] = text[:MAX_TEXT]
            host = (urlparse(result["final_url"]).hostname or "").lower()
            if host.endswith("google.com"):
                result["status"] = "consent_wall" if "consent" in host else "google_page"
            else:
                result["status"] = classify_failure(result["http_status"], html, result["text"])
            if result["status"] == "ok":
                sc = title_score(event_title, result["title"], result["text"], company_words)
                result["title_score"] = sc
                if min_title > 0 and sc < min_title:
                    result["status"] = "title_mismatch"
            if result["status"] == "ok":
                result["html"] = html[:MAX_HTML]
                if shot_path is not None:
                    try:
                        await page.screenshot(path=str(shot_path), full_page=False)
                        result["screenshot"] = shot_path.name
                    except Exception as e:
                        log.warning("screenshot failed %s: %s", url, e)
        except PWTimeout:
            result["status"] = "timeout"
        except Exception as e:
            log.warning("page error %s: %s", url, e)
            result["status"] = "error"
        finally:
            await page.close()
    return result



async def verify_event(ctx, sem, item: dict, out: Path, args) -> dict:
    """Try the resolved publisher URL, then resolved alternates (never Google links)."""
    res = item.get("resolved") or {}
    profile = profile_from_dict(item.get("profile"), item.get("company", ""))
    company_words = distinctive_words(profile)
    urls = [res["url"]] if res.get("url") else []
    urls += [a["url"] for a in item.get("resolved_alternates", [])][:MAX_ALTERNATES]
    ev_title = (item.get("article") or {}).get("title", "")
    attempts = []
    for i, u in enumerate(urls):
        shot = None if args.no_screenshots else out / "screenshots" / f"{item['event_id']}.png"
        r = await fetch_page(ctx, sem, u, ev_title, company_words, shot, args.min_title_score)
        r["url"], r["attempt"] = u, i
        attempts.append(r)
        if r["status"] == "ok":
            break
        if r["status"] in ("timeout", "rate_limited", "server_error"):
            await asyncio.sleep(2 ** i)
    if not attempts:
        attempts.append({"status": "unresolved", "http_status": None, "text": "", "title": "",
                         "date": None, "html": "", "url": None, "attempt": 0, "title_score": None,
                         "screenshot": None, "final_url": None})
    final = attempts[-1]


    hits = location_hits(profile, final["text"] + " " + final["title"])
    local = profile.get("scope") == "local"
    if final["status"] == "ok" and local and not hits and args.require_location:
        final["status"] = "out_of_scope"
    text_hash = hashlib.sha256(final["text"].encode()).hexdigest()[:16] if final["status"] == "ok" else None
    rec = {
        "company": item["company"], "profile": profile, "event_id": item["event_id"],
        "bucket": item["bucket"], "score": item["score"], "label": item["label"],
        "n_sources": item["n_sources"], "selection_reason": item["selection_reason"],
        "article": item["article"], "resolved": item["resolved"],
        "verification": {
            "status": final["status"], "http_status": final["http_status"],
            "url_used": final["url"], "attempts": len(attempts),
            "final_url": final.get("final_url"),
            "attempt_details": [{"url": a["url"], "status": a["status"],
                                 "http_status": a["http_status"], "title_score": a.get("title_score"),
                                 "final_url": a.get("final_url")}
                                for a in attempts],
            "title": final["title"], "published_at_page": final["date"],
            "title_score": final.get("title_score"), "text_chars": len(final["text"]),
            "content_hash": text_hash, "screenshot": final.get("screenshot"),
            "location_in_text": (bool(hits) if local else None), "location_terms_found": hits,
            "retrieved_at": now_iso(),
        },
        "text": final["text"],
    }
    if final["status"] == "ok":
        (out / "articles" / f"{item['event_id']}.json").write_text(
            json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "raw" / f"{item['event_id']}.html").write_text(
            final.get("html", ""), encoding="utf-8")
    return rec



def drop_duplicates(records: list[dict], out: Path) -> int:
    """If several events fetched identical text, keep the best title match, fail the rest."""
    groups = defaultdict(list)
    for r in records:
        v = r["verification"]
        if v["status"] == "ok" and v["content_hash"]:
            groups[v["content_hash"]].append(r)
    n = 0
    for h, rs in groups.items():
        if len(rs) < 2:
            continue
        rs.sort(key=lambda r: -(r["verification"]["title_score"] or 0))
        for r in rs[1:]:
            r["verification"]["status"] = "duplicate_content"
            r["text"] = ""
            eid = r["event_id"]
            for p in (out / "articles" / f"{eid}.json", out / "raw" / f"{eid}.html",
                      out / "screenshots" / f"{eid}.png"):
                p.unlink(missing_ok=True)
            n += 1
    return n



async def run(args) -> None:
    in_dir = find_input_dir(args.input, args.base)
    resolved = json.loads((in_dir / "resolved.json").read_text(encoding="utf-8"))
    items = resolved["items"]
    if args.limit:
        items = items[:args.limit]
    out = args.base / f"verify_{in_dir.name}"
    for sub in ("articles", "raw", "screenshots"):
        d = out / sub
        d.mkdir(parents=True, exist_ok=True)
        for f in d.glob("*"):             # clear stale files from earlier runs
            f.unlink()
    sem = asyncio.Semaphore(CONCURRENCY)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        await ctx.route("**/*", lambda route: route.abort()
                        if any(b in route.request.url for b in BLOCK_PATTERNS) else route.continue_())
        tasks = [verify_event(ctx, sem, it, out, args) for it in items]
        records = []
        done = 0
        for coro in asyncio.as_completed(tasks):
            records.append(await coro)
            done += 1
            if done % 10 == 0 or done == len(items):
                print(f"  {done}/{len(items)} done")
        await browser.close()


    n_dup = drop_duplicates(records, out)
    statuses = Counter(r["verification"]["status"] for r in records)
    ok = [r for r in records if r["verification"]["status"] == "ok"]
    report = {
        "stage": "verification", "generated_at": now_iso(), "input": str(in_dir),
        "n_events": len(items), "statuses": dict(statuses), "duplicates_removed": n_dup,
        "by_company": {c: dict(Counter(r["verification"]["status"] for r in records if r["company"] == c))
                       for c in sorted({r["company"] for r in records})},
        "avg_text_chars": round(sum(r["verification"]["text_chars"] for r in ok) / max(1, len(ok))),
        "screenshots": len(list((out / "screenshots").glob("*.png"))),
        "min_title_score": args.min_title_score, "require_location": args.require_location,
    }
    (out / "verify_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    failed = [{"company": r["company"], "event_id": r["event_id"], "title": r["article"].get("title"),
               "status": r["verification"]["status"], "attempts": r["verification"]["attempt_details"]}
              for r in records if r["verification"]["status"] != "ok"]
    (out / "verify_failed.json").write_text(json.dumps(failed, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"saved -> {out}")



def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Playwright verification worker")
    ap.add_argument("--input", type=Path, default=None, help="triage run folder with resolved.json")
    ap.add_argument("--base", type=Path, default=Path("output"))
    ap.add_argument("--limit", type=int, default=None, help="smoke test on N events")
    ap.add_argument("--no-screenshots", action="store_true")
    ap.add_argument("--min-title-score", type=float, default=MIN_TITLE_SCORE,
                    help="0 disables the headline-vs-page match check")
    ap.add_argument("--require-location", action="store_true",
                    help="local-scope companies: reject pages that never name a chosen location")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(a))




if __name__ == "__main__":
    main()
