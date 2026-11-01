"""
Stage 1 - Discovery: Google News RSS -> filtered, deduplicated candidate list.

Strategy: one feed per calendar day (after:/before: operators) so no single feed hits
the ~100-item cap. 24h / 7d / 30d windows are then assigned from each article's own
publish time. Capped days are re-queried with extra query variants.
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

log = logging.getLogger("discovery")

USER_AGENT = "FinAnalystBot/0.2 (research prototype; contact: you@example.com)"
FEED_BASE = "https://news.google.com/rss/search"
EDITION = "hl=en-US&gl=US&ceid=US:en"
MAX_CONCURRENT_FEEDS = 3
POLITE_DELAY = 0.4
RETRIES = 3
CAP_THRESHOLD = 95            # a feed with >= this many entries is treated as truncated
NEAR_DUP_RATIO = 0.90
NEAR_DUP_JACCARD = 0.80
DEFAULT_DAYS = 30

WINDOWS = {                   # label -> max article age
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# ---------- source tiers (extend freely) ----------
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


@dataclass(frozen=True)
class Company:
    name: str
    query: str
    aliases: tuple[str, ...]
    tickers: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    extra_queries: tuple[str, ...] = ()   # used only for days where the main query is capped


COMPANIES: dict[str, Company] = {
    "tesla": Company(
        name="Tesla", query="Tesla",
        aliases=("Tesla",), tickers=("TSLA",),
        exclude=(r"Nikola Tesla", r"Tesla coil", r"Tesla Laboratories", r"Tesla-logo pills"),
        extra_queries=("Tesla stock", "Tesla Musk", "Tesla earnings", "Tesla FSD robotaxi",
                       "Tesla sales deliveries", "Tesla recall"),
    ),
    "gm": Company(
        name="General Motors", query='"General Motors"',
        aliases=("General Motors",), tickers=("GM",),
        exclude=(r"genetically modified",),
        extra_queries=("GM earnings", "GM stock", "GM Chevrolet", "GM recall"),
    ),
    "ford": Company(
        name="Ford Motor", query='"Ford Motor"',
        aliases=("Ford Motor", "Ford"), tickers=(),
        exclude=(r"Harrison Ford", r"Gerald Ford", r"Gerald R\. Ford", r"Ford County",
                 r"Tom Ford", r"Betty Ford", r"Glenn Ford", r"Ford Foundation",
                 r"\bTamiami Ford\b", r"Ford dealership", r"Ford dealer\b"),
        extra_queries=("Ford F-150", "Ford stock", "Ford earnings", "Ford recall", "Ford EV"),
    ),
}


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
def match_entity(company: Company, title: str, snippet: str) -> tuple[str | None, bool]:
    text = f"{title} {snippet}"
    for pat in company.exclude:
        if re.search(pat, text, re.IGNORECASE):
            return None, False
    for alias in company.aliases:
        if re.search(rf"\b{re.escape(alias)}\b", text, re.IGNORECASE):
            in_title = bool(re.search(rf"\b{re.escape(alias)}\b", title, re.IGNORECASE))
            return alias, in_title
    for tk in company.tickers:
        if re.search(rf"\b{re.escape(tk)}\b", text):
            return tk, bool(re.search(rf"\b{re.escape(tk)}\b", title))
    return None, False


# ---------- fetching ----------
def build_feed_url(query: str, after: date, before: date) -> str:
    q = quote_plus(f"{query} after:{after:%Y-%m-%d} before:{before:%Y-%m-%d}")
    return f"{FEED_BASE}?q={q}&{EDITION}"


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


def parse_entries(raw: bytes, company: Company, feed_url: str, now: datetime,
                  days: int, stats: Counter) -> tuple[list[Item], int]:
    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        log.error("Unparseable feed: %s", feed_url)
        stats["slices_unparseable"] += 1
        return [], 0
    horizon = now - timedelta(days=days, hours=2)
    items: list[Item] = []
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
        item_id = hashlib.sha256(f"{company.name}|{normalize(title)}|{source}".encode()).hexdigest()[:16]
        items.append(Item(
            item_id=item_id, company=company.name, title=title, source=source,
            source_tier=classify_source(source), google_link=link,
            published_at=pub.isoformat(), snippet=snippet,
            windows=windows_for(now - pub), matched_on=matched, entity_in_title=in_title,
            found_via=[feed_url], retrieved_at=now.isoformat(),
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
    return kept


# ---------- orchestration ----------
async def discover_company(client, sem, company: Company, now: datetime, days: int):
    stats: Counter = Counter()
    capped_days: list[str] = []
    failed: list[str] = []
    today = now.date()

    async def fetch_one(query: str, day: date):
        url = build_feed_url(query, day, day + timedelta(days=1))
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
            for q in company.extra_queries:
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


def print_report(company: Company, items: list[Item], stats: Counter, capped, failed) -> None:
    print(f"\n=== {company.name} ===")
    print(f"feeds ok/failed/capped: {stats['slices_ok']}/{stats['slices_failed']}/{stats['slices_capped']}"
          f"  (extra-query feeds still capped: {stats['extra_slices_still_capped']})")
    print(f"raw {stats['raw']} | undated {stats['dropped_undated']} | old {stats['dropped_out_of_window']}"
          f" | off-topic {stats['dropped_entity']} | passed {stats['passed_filters']}"
          f" | unique {stats['unique_total']}")
    by_win = {w: sum(1 for i in items if w in i.windows) for w in WINDOWS}
    print("unique per window:", by_win)
    print("by source tier:", dict(Counter(i.source_tier for i in items)))
    print("entity in title:", sum(i.entity_in_title for i in items), "of", len(items))
    if capped:
        print("days that hit the feed cap (expanded with extra queries):", ", ".join(sorted(capped)))
    if failed:
        print("FAILED feeds:", len(failed), "(re-run to retry)")


async def run(keys: list[str], engagement_id: str, outdir: Path, days: int) -> None:
    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc)
    out = outdir / run_id
    out.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        for key in keys:
            company = COMPANIES[key]
            items, stats, capped, failed = await discover_company(client, sem, company, now, days)
            print_report(company, items, stats, capped, failed)
            record = {
                "engagement_id": engagement_id, "run_id": run_id, "company": company.name,
                "generated_at": now.isoformat(), "stage": "discovery", "days_covered": days,
                "stats": dict(stats), "capped_days": capped, "failed_feeds": failed,
                "items": [asdict(i) for i in items],
            }
            (out / f"discovery_{key}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"\nSaved to {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Google News RSS discovery")
    ap.add_argument("--companies", nargs="+", default=list(COMPANIES), choices=list(COMPANIES))
    ap.add_argument("--engagement-id", default="demo")
    ap.add_argument("--outdir", type=Path, default=Path("output"))
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(a.companies, a.engagement_id, a.outdir, a.days))


if __name__ == "__main__":
    main()