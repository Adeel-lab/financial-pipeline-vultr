"""
Stage 5 - Sentiment: score each verified article body, roll up to events, then to companies.
Works for any company; a local-scope company (e.g. "Betson Group (Budapest)") is judged for that scope.

Input : output/verify_<run>/articles/*.json   (from verify.py, status == ok)
Output: output/sentiment_<run>/article_scores.json   one row per article/event
        output/sentiment_<run>/company_sentiment.json  per-company roll-up + top opportunities/threats
Env   : VULTR_INFERENCE_API_KEY (optional; without it a keyword scorer runs offline)
        VULTR_INFERENCE_BASE, VULTR_CHAT_MODEL (any OpenAI-compatible endpoint, e.g. Groq)
        SENTIMENT_CONCURRENCY (default 1), SENTIMENT_DELAY (seconds between calls; Groq free tier ~12),
        SENTIMENT_MAX_TOKENS (default 1500)
Run   : python sentiment.py [--input output/verify_xxx] [--no-llm] [--limit 5]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from company_profile import profile_from_dict

log = logging.getLogger("sentiment")

PROMPT_VERSION = "v3-scope"
API_BASE = os.environ.get("VULTR_INFERENCE_BASE", "https://api.vultrinference.com/v1").rstrip("/")
API_KEY = os.environ.get("VULTR_INFERENCE_API_KEY", "")
CHAT_MODEL = os.environ.get("VULTR_CHAT_MODEL", "deepseek-v4.1-flash")
LLM_CONCURRENCY = int(os.environ.get("SENTIMENT_CONCURRENCY", "1"))
LLM_DELAY = float(os.environ.get("SENTIMENT_DELAY", "0"))
MAX_OUT_TOKENS = int(os.environ.get("SENTIMENT_MAX_TOKENS", "1500"))   # reasoning models need headroom

SENTIMENT_REASONING = os.environ.get("SENTIMENT_REASONING", "").strip()     # e.g. low (gpt-oss on Groq)
MAX_WAIT = float(os.environ.get("SENTIMENT_MAX_WAIT", "90"))                # longer waits = daily limit, stop
_last_call = 0.0
CONSENT_MARKERS = ("develop and improve new services", "before you continue to google", "consent.google.com")
MAX_TEXT = 6000            # characters of article body sent to the model
OPP_THRESHOLD = 0.25       # score >= this -> opportunity, <= -this -> threat
TIER_VALUE = {"major": 1.0, "other": 0.5, "financial_aggregator": 0.3}
SCOPE_FACTOR = {"in_scope": 1.0, "group_wide": 0.7, "out_of_scope": 0.3}
BUCKETS = ("24h", "7d", "30d")
TOP_N = 5

POS = re.compile(r"\b(beat|beats|surge\w*|jump\w*|rise\w*|rose|record|growth|grow\w*|approv\w*|win\w*|"
                 r"strong\w*|upgrad\w*|rais\w*|boost\w*|profit\w*|rebound\w*|recover\w*|expand\w*|"
                 r"gain\w*|improv\w*|outperform\w*|breakthrough|demand|milestone|launch\w*|order\w*)\b", re.I)
NEG = re.compile(r"\b(miss\w*|fall\w*|fell|sink\w*|sank|slump\w*|drop\w*|cut\w*|recall\w*|lawsuit\w*|"
                 r"probe\w*|crash\w*|kill\w*|downgrad\w*|warn\w*|delay\w*|declin\w*|loss\w*|layoff\w*|"
                 r"halt\w*|cancel\w*|concern\w*|pressure\w*|plunge\w*|terminat\w*|weak\w*|shortage\w*|"
                 r"tariff\w*|fine[sd]?|penalt\w*|defect\w*|risk\w*|headwind\w*|slowdown)\b", re.I)

SYSTEM_PROMPT = (
    "You are a business news analyst. You get the full text of ONE news article (any language), the name "
    "of a company, and the Scope the analysis is about (either the whole company, or specific places). "
    "Judge the article's likely effect on that company's business outlook within the Scope - not the "
    "writer's tone. Reply with ONLY one JSON object with these fields: "
    "sentiment (number from -1.0 very negative to 1.0 very positive), "
    "confidence (0.0-1.0), "
    "about_company (true if the article is materially about this company, false if it only mentions it, "
    "e.g. sponsorship or a list of names), "
    "scope_relevance ('in_scope' if it concerns the Scope, 'group_wide' if it concerns the whole company "
    "or other places but still affects the Scope, 'out_of_scope' if it is only about other places; when the "
    "Scope is the whole company always answer 'in_scope'), "
    "summary (max 25 words, in English), "
    "evidence (ONE sentence copied EXACTLY, word for word, in the article's own language)."
)


# ---------------------------------------------------------------- helpers
def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def content_hash(text: str) -> str:
    # same formula verify.py uses: sha256 of the extracted text, first 16 hex chars
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if len(p.strip()) > 30]


def signal_from(score: float) -> str:
    return "opportunity" if score >= OPP_THRESHOLD else "threat" if score <= -OPP_THRESHOLD else "neutral"


def find_verify_dir(arg: Path | None, base: Path) -> Path:
    if arg:
        return arg
    runs = [p for p in base.iterdir() if p.is_dir() and (p / "articles").is_dir()]
    if not runs:
        sys.exit(f"No verify_* folder with articles/ under {base}. Run verify.py first.")
    return max(runs, key=lambda p: p.stat().st_mtime)


def save_json_atomic(path: Path, data) -> None:
    import tempfile
    import time

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False)

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(temp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(min(0.1 * (2 ** attempt), 1.0))
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

# ---------------------------------------------------------------- keyword scorer (offline, English only)
def keyword_score(text: str) -> dict:
    lead = text[:1000]
    p = len(POS.findall(text)) + len(POS.findall(lead))   # lead counted twice: news front-loads the point
    n = len(NEG.findall(text)) + len(NEG.findall(lead))
    score = (p - n) / (p + n + 3)
    best, best_hits = "", 0
    for s in split_sentences(text):
        hits = len(POS.findall(s)) + len(NEG.findall(s))
        if hits > best_hits:
            best, best_hits = s, hits
    return {"sentiment": round(score, 3), "confidence": round(min(1.0, (p + n) / 12) * 0.6, 3),
            "about_company": True, "scope_relevance": "in_scope", "summary": "", "evidence": best[:300],
            "evidence_verified": bool(best), "method": "keyword"}


# ---------------------------------------------------------------- LLM scorer
class LLMState:
    dead = False
    ok = 0
    failed = 0


def parse_reset(s):
    tot = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s or ""):
        tot += float(num) / 1000 if unit == "ms" else float(num) * {"h": 3600, "m": 60, "s": 1}[unit]
    return tot


async def chat(client: httpx.AsyncClient, messages: list[dict]) -> str | None:
    global _last_call
    payload = {"model": CHAT_MODEL, "messages": messages, "temperature": 0, "max_tokens": MAX_OUT_TOKENS}
    if SENTIMENT_REASONING:
        payload["reasoning_effort"] = SENTIMENT_REASONING
    for attempt in range(6):
        gap = LLM_DELAY - (time.monotonic() - _last_call)
        if gap > 0:
            await asyncio.sleep(gap)
        _last_call = time.monotonic()
        try:
            r = await client.post(f"{API_BASE}/chat/completions", json=payload)
        except (httpx.TimeoutException, httpx.TransportError):
            wait = 2 ** attempt * 2
            print(f"   network error, retry {attempt + 1}/6 in {wait}s", flush=True)
            await asyncio.sleep(wait)
            continue
        if r.status_code == 200:
            try:
                left = int(r.headers.get("x-ratelimit-remaining-tokens", "999999"))
            except ValueError:
                left = 999999
            if left < 3000:
                await asyncio.sleep(min(parse_reset(r.headers.get("x-ratelimit-reset-tokens")), MAX_WAIT))
            return r.json()["choices"][0]["message"]["content"]
        if r.status_code == 400 and "reasoning_effort" in payload:
            payload.pop("reasoning_effort")
            print("   provider rejected reasoning_effort, continuing without it", flush=True)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            try:
                wait = float(r.headers.get("retry-after", 0))
            except ValueError:
                wait = 0.0

            if r.status_code == 429 and wait > MAX_WAIT:
                LLMState.dead = True
                print(
                    f"!! Groq requested a {wait:.0f}s wait. "
                    "Stopping this stage rather than using keyword scores.",
                    flush=True,
                )
                print(f"!! Groq error: {r.text[:2000]}", flush=True)
                raise RuntimeError(
                    f"Groq rate limit requires waiting {wait:.0f}s; "
                    "see the error details above, then retry this stage."
                )
            wait = max(wait, 2 ** attempt * 2) + random.random()
            print(f"   HTTP {r.status_code}, retry {attempt + 1}/6 in {wait:.0f}s", flush=True)
            await asyncio.sleep(wait)
            continue
        LLMState.dead = True
        log.error("Chat API fatal HTTP %s %s", r.status_code, r.text[:300])
        print(f"!! Chat API error HTTP {r.status_code} - falling back to keyword scores")
        return None
    return None


def extract_json_object(text: str) -> dict | None:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b < a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def validate_llm(raw: dict, article_text: str, local_scope: bool) -> dict | None:
    try:
        score = max(-1.0, min(1.0, float(raw.get("sentiment", 0))))
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        return None
    evidence = str(raw.get("evidence", "") or "").strip()
    verified = bool(evidence) and norm_ws(evidence) in norm_ws(article_text)
    sr = raw.get("scope_relevance", "in_scope")
    sr = sr if sr in SCOPE_FACTOR else "in_scope"
    if not local_scope:
        sr = "in_scope"
    return {"sentiment": round(score, 3), "confidence": round(conf, 3),
            "about_company": bool(raw.get("about_company", True)), "scope_relevance": sr,
            "summary": str(raw.get("summary", ""))[:200], "evidence": evidence[:400],
            "evidence_verified": verified, "method": "llm"}


async def llm_score(client, sem, company: str, scope_text: str, local_scope: bool, text: str) -> dict | None:

    if LLMState.dead:
        raise RuntimeError(
            "LLM scoring stopped after a provider error; "
            "retry when quota is available."
        )
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Company: {company}\nScope: {scope_text}\n\nARTICLE:\n{text[:MAX_TEXT]}"}]

    async with sem:

        if LLMState.dead:
            raise RuntimeError(
                "LLM scoring stopped after a provider error; "
                "retry when quota is available."
            )

        out = await chat(client, msgs)
        if LLM_DELAY:
            await asyncio.sleep(LLM_DELAY)
    obj = extract_json_object(out) if out else None
    res = validate_llm(obj, text, local_scope) if obj else None
    if res is None:
        LLMState.failed += 1
        return None
    LLMState.ok += 1
    return res


# ---------------------------------------------------------------- per-article / per-event
def event_weight(rec: dict, score: dict) -> float:
    tier = TIER_VALUE.get((rec.get("article") or {}).get("source_tier", "other"), 0.5)
    n_src = max(1, int(rec.get("n_sources") or 1))
    coverage = 0.5 + 0.5 * min(1.0, math.log1p(n_src) / math.log(8))
    rel = (rec.get("label") or {}).get("business_relevance", 2)
    relevance = max(0.1, min(3, rel) / 3)
    conf = max(0.2, score["confidence"])
    about = 1.0 if score["about_company"] else 0.3
    scope = SCOPE_FACTOR.get(score.get("scope_relevance", "in_scope"), 1.0)
    return round(tier * coverage * relevance * conf * about * scope, 4)


async def score_article(client, sem, rec: dict, cache: dict, use_llm: bool) -> dict:
    text = rec.get("text", "")
    profile = profile_from_dict(rec.get("profile"), rec["company"])
    scope_text = profile.get("scope_text", "global (whole company)")
    local_scope = profile.get("scope") == "local"
    h = content_hash(text)
    ck = hashlib.sha1(f"{PROMPT_VERSION}|{CHAT_MODEL}|{rec['company']}|{scope_text}|{h}".encode()).hexdigest()
    res = cache.get(ck) if use_llm else None
    if res is None and use_llm and client is not None:
        res = await llm_score(client, sem, rec["company"], scope_text, local_scope, text)
        if res:
            cache[ck] = res
    if res is None:
        res = keyword_score(text)
    art = rec.get("article") or {}
    lab = rec.get("label") or {}
    ver = rec.get("verification") or {}
    sig = signal_from(res["sentiment"])
    return {
        "event_id": rec["event_id"], "company": rec["company"], "bucket": rec.get("bucket"),
        "title": art.get("title"), "source": art.get("source"), "source_tier": art.get("source_tier"),
        "url": ver.get("url_used"), "title_score": ver.get("title_score"),
        "location_in_text": ver.get("location_in_text"),
        "published_at": art.get("published_at"), "n_sources": rec.get("n_sources"),
        "content_hash": h, "text_chars": len(text),
        "sentiment": res["sentiment"], "signal": sig, "confidence": res["confidence"],
        "about_company": res["about_company"], "scope_relevance": res.get("scope_relevance", "in_scope"),
        "summary": res["summary"], "evidence": res["evidence"], "evidence_verified": res["evidence_verified"],
        "method": res["method"], "weight": event_weight(rec, res),
        "triage_sentiment": lab.get("sentiment"), "triage_signal": lab.get("signal"),
        "triage_topic": lab.get("topic"),
        "agrees_with_triage": (lab.get("signal") == sig) if lab.get("signal") else None,
    }


# ---------------------------------------------------------------- aggregation
def weighted_mean(rows: list[dict]) -> float | None:
    w = sum(r["weight"] for r in rows)
    return round(sum(r["weight"] * r["sentiment"] for r in rows) / w, 3) if w > 0 else None


def top_items(rows: list[dict], signal: str, n: int = TOP_N) -> list[dict]:
    pool = [r for r in rows if r["signal"] == signal and r["about_company"]
            and r.get("scope_relevance") != "out_of_scope"]
    pool.sort(key=lambda r: -(r["weight"] * abs(r["sentiment"])))
    keep = ("event_id", "title", "source", "url", "published_at", "bucket", "sentiment", "weight",
            "content_hash", "summary", "evidence", "evidence_verified", "triage_topic", "scope_relevance")
    return [{k: r[k] for k in keep} for r in pool[:n]]


def summarise(rows: list[dict]) -> dict:
    return {"n_articles": len(rows), "net_sentiment": weighted_mean(rows),
            "signal_counts": dict(Counter(r["signal"] for r in rows)),
            "top_opportunities": top_items(rows, "opportunity"),
            "top_threats": top_items(rows, "threat")}


def aggregate(rows: list[dict], report: dict | None, profiles: dict[str, dict]) -> dict:
    by_co: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_co[r["company"]].append(r)
    out = {}
    for co, rs in by_co.items():
        block = summarise(rs)
        block["profile"] = profiles.get(co)
        block["by_bucket"] = {b: summarise([r for r in rs if r["bucket"] == b]) for b in BUCKETS}
        sel = (report or {}).get("by_company", {}).get(co, {})
        total = sum(sel.values()) if sel else None
        block["coverage"] = {"events_selected": total, "events_with_full_text": len(rs),
                             "events_headline_only": (total - len(rs)) if total else None}
        block["quality"] = {
            "llm_scored": sum(r["method"] == "llm" for r in rs),
            "keyword_scored": sum(r["method"] == "keyword" for r in rs),
            "evidence_verified": sum(bool(r["evidence_verified"]) for r in rs),
            "not_about_company": sum(not r["about_company"] for r in rs),
            "out_of_scope": sum(r.get("scope_relevance") == "out_of_scope" for r in rs),
            "agree_with_triage_pct": round(100 * sum(1 for r in rs if r["agrees_with_triage"]) /
                                            max(1, sum(1 for r in rs if r["agrees_with_triage"] is not None)), 1)}
        out[co] = block
    return out


# ---------------------------------------------------------------- pipeline
async def run(args) -> None:
    vdir = find_verify_dir(args.input, args.base)
    files = sorted((vdir / "articles").glob("*.json"))
    if args.limit:
        files = files[:args.limit]
    if not files:
        sys.exit(f"No article JSON files in {vdir / 'articles'}")
    recs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    recs = [r for r in recs if r.get("verification", {}).get("status") == "ok" and r.get("text")]
    n_in = len(recs)
    recs = [r for r in recs if not any(m in r["text"].lower() for m in CONSENT_MARKERS)]
    seen, uniq = set(), []
    for r in recs:
        h = content_hash(r["text"])
        if h not in seen:
            seen.add(h)
            uniq.append(r)
    print(f"guard: {n_in} ok articles -> {len(uniq)} kept "
          f"({n_in - len(recs)} consent pages, {len(recs) - len(uniq)} duplicate texts dropped)")
    recs = uniq
    if not recs:
        sys.exit("No usable articles left after the guard. Re-run resolve.py and verify.py first.")
    profiles = {r["company"]: profile_from_dict(r.get("profile"), r["company"]) for r in recs}
    report_path = vdir / "verify_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None

    out = args.base / vdir.name.replace("verify_", "sentiment_")
    out.mkdir(parents=True, exist_ok=True)
    cache_path = out / "llm_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    use_llm = bool(API_KEY) and not args.no_llm
    print(f"{len(recs)} articles | mode: {'LLM ' + CHAT_MODEL if use_llm else 'offline keyword scorer (English only)'}")
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    done = 0
    async with httpx.AsyncClient(headers=headers, timeout=90) as client:
        async def one(r):
            nonlocal done
            row = await score_article(client, sem, r, cache, use_llm)
            done += 1
            print(f"[{done}/{len(recs)}] {row['company'][:18]:18} {row['sentiment']:+.2f} "
                  f"{row['method']:7} {(row['title'] or '')[:60]}", flush=True)
            if use_llm:
                save_json_atomic(cache_path, cache)
            return row
        rows = await asyncio.gather(*(one(r) for r in recs))
    save_json_atomic(cache_path, cache)

    rows = sorted(rows, key=lambda r: (r["company"], r["bucket"] or "", -r["weight"]))
    now = datetime.now(timezone.utc).isoformat()
    save_json_atomic(out / "article_scores.json", {"stage": "sentiment", "generated_at": now,
                     "source_dir": str(vdir), "model": CHAT_MODEL if use_llm else "keyword",
                     "prompt_version": PROMPT_VERSION, "items": rows})
    companies = aggregate(rows, report, profiles)
    save_json_atomic(out / "company_sentiment.json", {"stage": "sentiment", "generated_at": now,
                     "companies": companies})

    for co, b in companies.items():
        print(f"\n=== {co} === net sentiment {b['net_sentiment']} | {b['signal_counts']} | coverage {b['coverage']}")
        for label, key in (("Opportunities", "top_opportunities"), ("Threats", "top_threats")):
            print(f"  {label}:")
            for t in b[key][:3]:
                print(f"    {t['sentiment']:+.2f}  [{t['bucket']}] {t['title'][:90]}")
    print(f"\nLLM ok/failed calls: {LLMState.ok}/{LLMState.failed}"
          + ("  (LLM disabled after fatal error)" if LLMState.dead else ""))
    print(f"saved -> {out}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Sentiment scoring + aggregation")
    ap.add_argument("--input", type=Path, default=None, help="verify_* run folder (default newest)")
    ap.add_argument("--base", type=Path, default=Path("output"))
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(a))


if __name__ == "__main__":
    main()
