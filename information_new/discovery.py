"""
Stage 1 - Discovery: Google News RSS -> filtered, deduplicated candidate list, for ANY company.

Examples
  python discovery.py --company "Microsoft" --ticker MSFT --location global
  python discovery.py --company "Betson Group" --location Budapest
  python discovery.py --company "XYZ Ltd" --location Washington California "New York"
  python discovery.py --company "Betson Group" --location Budapest Cyprus --alias Betson
  python discovery.py --preset tesla gm ford          (the original three test companies)
  python discovery.py --companies-file my_companies.json

Ticker, alias, exclude and location are all OPTIONAL. Omit --location (or say "global") for a
worldwide view; give one or more places for a local view.

Strategy: one feed per calendar day (after:/before: operators) so no single feed hits the
~100-item cap. 24h / 7d / 30d windows are then assigned from each article's own publish time.
Capped days are re-queried with extra query variants.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import random
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone, date
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import httpx

from company_profile import CompanyProfile, add_profile_args, profiles_from_args, location_hits, strip_accents

log = logging.getLogger("discovery")

USER_AGENT = "FinAnalystBot/0.3 (research prototype; contact: you@example.com)"
FEED_BASE = "https://news.google.com/rss/search"
MAX_CONCURRENT_FEEDS = 3
POLITE_DELAY = 0.4
RETRIES = 3
CAP_THRESHOLD = 95            # a feed with >= this many entries is treated as truncated
NEAR_DUP_RATIO = 0.90
NEAR_DUP_JACCARD = 0.80
DEFAULT_DAYS = 30
THIN_COVERAGE = 15            # fewer unique articles than this -> warn the user

WINDOWS = {                   # label -> max article age
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# ---------- source tiers (extend freely; mostly English-language outlets) ----------
MAJOR_SOURCES = (
    "reuters", "bloomberg", "wsj", "wall street journal", "cnbc", "new york times",
    "financial times", "associated press", "ap news", "the guardian", "washington post",
    "barron", "marketwatch", "forbes", "fortune", "axios", "cnn", "nbc news", "cbs news",
    "abc news", "bbc", "wired", "the verge", "techcrunch", "ars technica", "electrek",
    "detroit free press", "the detroit news", "automotive news", "los angeles times",
    "the globe and mail", "business insider", "time magazine", "al jazeera", "npr", "pbs",
)
AGGREGATOR_SOURCES = (
    "marketbeat", "moomoo", "tradingview", "stock traders daily", "marketscreener",
    "gurufocus", "cryptonomist", "tokenpost", "simply wall street", "tradingkey",
    "ad hoc news", "investing.com", "facebook", "biggo", "trefis", "benzinga",
    "yahoo finance", "247 wall st", "seeking alpha", "alphastreet", "kalkine",
)


def classify_source(source: str) -> str:
    s = source.lower()
    if any(k in s for k in MAJOR_SOURCES):
        return "major"
    if any(k in s for k in AGGREGATOR_SOURCES):
        return "financial_aggregator"
    return "other"


@dataclass
class Item:
    item_id: str
    company: str
    title: str
    source: str
    source_tier: str
    google_link: str
    published_at: str
    snippet: str
    windows: list[str]
    matched_on: str
    entity_in_title: bool
    location_hits: list[str] = field(default_factory=list)
    found_via: list[str] = field(default_factory=list)
    also_reported_by: list[str] = field(default_factory=list)
    duplicates_merged: int = 0
    retrieved_at: str = ""
    status: str = "discovered"


# ---------- text helpers ----------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", s))).strip()


def strip_source_suffix(title: str, source: str) -> str:
    if source and title.lower().endswith(" - " + source.lower()):
        return title[: -(len(source) + 3)].strip()
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if len(tail) <= 40:
            return head.strip()
    return title


def normalize(title: str) -> str:
    t = unicodedata.normalize("NFKD", title).lower()
    t = "".join(c for c in t if not unicodedata.combining(c))     # keep accented words whole
    t = re.sub(r"[^\w\s]", " ", t)
    return _WS.sub(" ", t).strip()


def near_duplicate(a: str, b: str, ta: set, tb: set) -> bool:
    if a == b:
        return True
    if not ta or not tb:
        return False
    jac = len(ta & tb) / len(ta | tb)
    if jac >= NEAR_DUP_JACCARD:
        return True
    if jac < 0.5:
        return False
    return SequenceMatcher(None, a, b).ratio() >= NEAR_DUP_RATIO


# ---------- entity filter ----------
def match_entity(company: CompanyProfile, title: str, snippet: str) -> tuple[str | None, bool]:
    # accent-insensitive: "Bétson" matches "Betson"
    text = strip_accents(f"{title} {snippet}")
    title_s = strip_accents(title)
    for pat in company.exclude:
        if re.search(pat, text, re.IGNORECASE):
            return None, False
    for alias in company.all_aliases:
        a = strip_accents(alias)
        if re.search(rf"\b{re.escape(a)}\b", text, re.IGNORECASE):
            in_title = bool(re.search(rf"\b{re.escape(a)}\b", title_s, re.IGNORECASE))
            return alias, in_title
    for tk in company.tickers:
        if re.search(rf"\b{re.escape(tk)}\b", text):
            return tk, bool(re.search(rf"\b{re.escape(tk)}\b", title_s))
    return None, False


# ---------- fetching ----------
def build_feed_url(query: str, after: date, before: date, edition: str) -> str:
    q = quote_plus(f"{query} after:{after:%Y-%m-%d} before:{before:%Y-%m-%d}")
    return f"{FEED_BASE}?q={q}&{edition}"


async def fetch_feed(client: httpx.AsyncClient, url: str) -> bytes | None:
    for attempt in range(RETRIES):
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.content
            if r.status_code in (429, 500, 502, 503, 504):
                log.warning("HTTP %s attempt %d: %s", r.status_code, attempt + 1, url)
            else:
                log.error("HTTP %s (no retry): %s", r.status_code, url)
                return None
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("Network error attempt %d: %s (%s)", attempt + 1, url, e)
        await asyncio.sleep(2 ** attempt + random.random())
    log.error("Giving up on feed: %s", url)
    return None


def windows_for(age: timedelta) -> list[str]:
    return [w for w, limit in WINDOWS.items() if age <= limit]


def parse_entries(raw: bytes, company: CompanyProfile, feed_url: str, now: datetime,
                  days: int, stats: Counter) -> tuple[list[Item], int]:
    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        log.error("Unparseable feed: %s", feed_url)
        stats["slices_unparseable"] += 1
        return [], 0
    horizon = now - timedelta(days=days, hours=2)
    items: list[Item] = []
    prof = company.to_dict()
    for e in parsed.entries:
        stats["raw"] += 1
        title_full = clean_text(e.get("title"))
        link = e.get("link", "")
        source = clean_text((e.get("source") or {}).get("title", "")) or "unknown"
        if not title_full or not link:
            stats["dropped_malformed"] += 1
            continue
        try:
            pub = parsedate_to_datetime(e.get("published", ""))
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            pub = pub.astimezone(timezone.utc)
        except (TypeError, ValueError):
            stats["dropped_undated"] += 1
            continue
        if pub < horizon or pub > now + timedelta(hours=1):
            stats["dropped_out_of_window"] += 1
            continue
        title = strip_source_suffix(title_full, source)
        snippet = clean_text(e.get("summary"))
        if normalize(snippet) in (normalize(title), normalize(title_full)):
            snippet = ""
        matched, in_title = match_entity(company, title, snippet)
        if not matched:
            stats["dropped_entity"] += 1
            continue
        hits = location_hits(prof, f"{title} {snippet}")
        if company.strict_location and company.scope == "local" and not hits:
            stats["dropped_location"] += 1
            continue
        item_id = hashlib.sha256(f"{company.label}|{normalize(title)}|{source}".encode()).hexdigest()[:16]
        items.append(Item(
            item_id=item_id, company=company.label, title=title, source=source,
            source_tier=classify_source(source), google_link=link,
            published_at=pub.isoformat(), snippet=snippet,
            windows=windows_for(now - pub), matched_on=matched, entity_in_title=in_title,
            location_hits=hits, found_via=[feed_url], retrieved_at=now.isoformat(),
        ))
    return items, len(parsed.entries)


# ---------- dedup ----------
def deduplicate(items: list[Item]) -> list[Item]:
    items = sorted(items, key=lambda i: i.published_at)       # earliest report is canonical
    kept: list[Item] = []
    norms: list[str] = []
    toks: list[set] = []
    exact: dict[str, Item] = {}
    for it in items:
        n = normalize(it.title)
        hit = exact.get(n)
        if hit is None:
            t = set(n.split())
            hit = next((k for k, kn, kt in zip(kept, norms, toks) if near_duplicate(n, kn, t, kt)), None)
        if hit is None:
            kept.append(it)
            norms.append(n)
            toks.append(set(n.split()))
            exact[n] = it
            continue
        hit.duplicates_merged += 1
        for f in it.found_via:
            if f not in hit.found_via:
                hit.found_via.append(f)
        if it.source != hit.source and it.source not in hit.also_reported_by:
            hit.also_reported_by.append(it.source)
        for h in it.location_hits:
            if h not in hit.location_hits:
                hit.location_hits.append(h)
    return kept


# ---------- orchestration ----------
async def discover_company(client, sem, company: CompanyProfile, now: datetime, days: int):
    stats: Counter = Counter()
    capped_days: list[str] = []
    failed: list[str] = []
    today = now.date()

    async def fetch_one(query: str, day: date):
        url = build_feed_url(query, day, day + timedelta(days=1), company.edition)
        async with sem:
            raw = await fetch_feed(client, url)
            await asyncio.sleep(POLITE_DELAY)
        if raw is None:
            stats["slices_failed"] += 1
            failed.append(f"{day}|{query}")
            return [], 0
        stats["slices_ok"] += 1
        return parse_entries(raw, company, url, now, days, stats)

    async def do_day(offset: int) -> list[Item]:
        day = today - timedelta(days=offset)
        got, n = await fetch_one(company.query, day)
        if n >= CAP_THRESHOLD:
            capped_days.append(str(day))
            stats["slices_capped"] += 1
            for q in company.all_extra_queries:
                extra, n2 = await fetch_one(q, day)
                got.extend(extra)
                if n2 >= CAP_THRESHOLD:
                    stats["extra_slices_still_capped"] += 1
        return got

    per_day = await asyncio.gather(*(do_day(o) for o in range(days)))
    all_items = [i for lst in per_day for i in lst]
    stats["passed_filters"] = len(all_items)
    unique = deduplicate(all_items)
    stats["unique_total"] = len(unique)
    return unique, stats, capped_days, failed


def print_report(company: CompanyProfile, items: list[Item], stats: Counter, capped, failed) -> None:
    print(f"\n=== {company.label} ===")
    print(f"scope: {company.scope_text()} | search: {company.query}")
    print(f"feeds ok/failed/capped: {stats['slices_ok']}/{stats['slices_failed']}/{stats['slices_capped']}"
          f"  (extra-query feeds still capped: {stats['extra_slices_still_capped']})")
    print(f"raw {stats['raw']} | undated {stats['dropped_undated']} | old {stats['dropped_out_of_window']}"
          f" | off-topic {stats['dropped_entity']} | location-filtered {stats['dropped_location']}"
          f" | passed {stats['passed_filters']} | unique {stats['unique_total']}")
    by_win = {w: sum(1 for i in items if w in i.windows) for w in WINDOWS}
    print("unique per window:", by_win)
    print("by source tier:", dict(Counter(i.source_tier for i in items)))
    print("entity in title:", sum(i.entity_in_title for i in items), "of", len(items))
    if company.scope == "local":
        print("location named in headline/summary:", sum(1 for i in items if i.location_hits), "of", len(items))
    if capped:
        print("days that hit the feed cap (expanded with extra queries):", ", ".join(sorted(capped)))
    if failed:
        print("FAILED feeds:", len(failed), "(re-run to retry)")
    if len(items) < THIN_COVERAGE:
        print(f"WARNING: only {len(items)} unique articles found. Little public news exists for this search - "
              "try an --alias, a wider --location (or global), a longer --days, or a local-language --edition.")


async def run(profiles: list[CompanyProfile], engagement_id: str, outdir: Path, days: int) -> None:
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    out = outdir / run_id
    out.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        for company in profiles:
            items, stats, capped, failed = await discover_company(client, sem, company, now, days)
            print_report(company, items, stats, capped, failed)
            record = {
                "engagement_id": engagement_id, "run_id": run_id, "company": company.label,
                "profile": company.to_dict(),
                "generated_at": now.isoformat(), "stage": "discovery", "days_covered": days,
                "stats": dict(stats), "capped_days": capped, "failed_feeds": failed,
                "items": [asdict(i) for i in items],
            }
            (out / f"discovery_{company.slug}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"\nSaved to {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Google News RSS discovery for any company",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    add_profile_args(ap)
    ap.add_argument("--engagement-id", default="demo")
    ap.add_argument("--outdir", type=Path, default=Path("output"))
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(profiles_from_args(a), a.engagement_id, a.outdir, a.days))


if __name__ == "__main__":
    main()
