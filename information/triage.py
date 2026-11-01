"""
Stage 2 - Triage: discovery output -> event clusters -> LLM labels -> scored, budgeted selection.

Input : output/<run_id>/discovery_*.json   (from discovery.py)
Output: output/triage_<run_id>/triage_<company>.json + selected_for_resolution.json

Needs: pip install httpx numpy
Env  : VULTR_INFERENCE_API_KEY  (optional - without it the stage runs fully offline with
       TF-IDF clustering and a keyword labeler)
       VULTR_CHAT_MODEL, VULTR_EMBED_MODEL, VULTR_INFERENCE_BASE  (optional overrides;
       any OpenAI-compatible endpoint works, e.g. Groq while Vultr is not set up)
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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

log = logging.getLogger("triage")

API_BASE = os.environ.get("VULTR_INFERENCE_BASE", "https://api.vultrinference.com/v1").rstrip("/")
API_KEY = os.environ.get("VULTR_INFERENCE_API_KEY", "")
CHAT_MODEL = os.environ.get("VULTR_CHAT_MODEL", "deepseek-v4.1-flash")
EMBED_MODEL_ENV = os.environ.get("VULTR_EMBED_MODEL", "")

BUDGETS = {"24h": 10, "7d": 15, "30d": 15}
TOPIC_CAP_FRACTION = 0.4           # max share of one topic inside a single time bucket
GLOBAL_TOPIC_CAP = 8               # max events of one topic in a company's final selection
MIN_PER_SIGNAL = 4                 # minimum opportunities and threats in the final selection
BATCH_SIZE = 20
LLM_CONCURRENCY = 4
EMBED_BATCH = 64
MAX_GAP_DAYS = 4.0                 # an event cannot span coverage gaps longer than this
DEFAULT_SIM = {"embeddings": 0.78, "tfidf": 0.35}

TOPICS = (
    "earnings_financials", "products_launch", "autonomy_ai", "regulation_legal",
    "supply_chain_ops", "labor_org", "mna_partnerships", "recalls_safety",
    "macro_competition", "analyst_stock_chatter", "other",
)
SIGNALS = ("opportunity", "threat", "neutral")
TOPIC_WEIGHT = {
    "earnings_financials": 1.0, "regulation_legal": 0.95, "mna_partnerships": 0.95,
    "autonomy_ai": 0.9, "supply_chain_ops": 0.9, "recalls_safety": 0.9,
    "products_launch": 0.85, "macro_competition": 0.85, "labor_org": 0.8,
    "other": 0.35, "analyst_stock_chatter": 0.25,
}
TIER_VALUE = {"major": 1.0, "other": 0.5, "financial_aggregator": 0.3}
TIER_RANK = {"major": 2, "other": 1, "financial_aggregator": 0}

STOP = set("""a an the of to in on for and or is are was were be been by with as at from its it
that this these those after over amid says say said new vs up down out into than more most
will would could can may might has have had not no but about their his her our your you we
they what why how who when where which while just now also still""".split())


# ---------------------------------------------------------------- loading
def find_input_dir(arg: Path | None, base: Path) -> Path:
    if arg:
        return arg
    runs = [p for p in base.iterdir() if p.is_dir() and any(p.glob("discovery_*.json"))]
    if not runs:
        sys.exit(f"No discovery output found under {base}. Run discovery.py first.")
    return max(runs, key=lambda p: p.stat().st_mtime)


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


# ---------------------------------------------------------------- vectors
def tfidf_matrix(texts: list[str], max_features: int = 8000) -> np.ndarray:
    docs = []
    for t in texts:
        toks = [w for w in re.findall(r"[a-z0-9]+", t.lower()) if w not in STOP and len(w) > 1]
        docs.append(toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])])
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    terms = [t for t, c in df.most_common() if 2 <= c <= 0.4 * n][:max_features]
    index = {t: i for i, t in enumerate(terms)}
    m = np.zeros((n, max(1, len(terms))), dtype=np.float32)
    for r, d in enumerate(docs):
        for t, c in Counter(d).items():
            j = index.get(t)
            if j is not None:
                m[r, j] = (1 + math.log(c)) * math.log((1 + n) / (1 + df[t]))
    return l2norm(m)


def l2norm(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


async def list_models(client: httpx.AsyncClient) -> list[str]:
    r = await client.get(f"{API_BASE}/models")
    r.raise_for_status()
    data = r.json()
    rows = data.get("data", data) if isinstance(data, dict) else data
    return [m.get("id", "") for m in rows if isinstance(m, dict)]


async def resolve_embed_model(client: httpx.AsyncClient) -> str | None:
    if EMBED_MODEL_ENV:
        return EMBED_MODEL_ENV
    try:
        ids = await list_models(client)
    except Exception as e:
        log.warning("Could not list models (%s)", e)
        return None
    for hint in ("embed", "bge", "e5", "gte", "minilm"):
        for mid in ids:
            if hint in mid.lower():
                return mid
    return None


async def embed_texts(client: httpx.AsyncClient, model: str, texts: list[str]) -> np.ndarray | None:
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        for attempt in range(4):
            try:
                r = await client.post(f"{API_BASE}/embeddings", json={"model": model, "input": batch})
            except (httpx.TimeoutException, httpx.TransportError):
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                rows = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
                vecs = [d["embedding"] for d in rows]
                if len(vecs) != len(batch):
                    log.warning("Embedding count mismatch")
                    return None
                out.extend(vecs)
                break
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(2 ** attempt + random.random())
                continue
            log.warning("Embeddings HTTP %s: %s", r.status_code, r.text[:200])
            return None
        else:
            return None
    return l2norm(np.array(out, dtype=np.float32))


def leader_cluster(vecs: np.ndarray, times: np.ndarray, thr: float, gap_days: float) -> np.ndarray:
    n, d = vecs.shape
    order = np.argsort(times, kind="stable")
    cap = 256
    cents = np.zeros((cap, d), np.float32)
    norms = np.ones(cap, np.float32)
    last = np.zeros(cap, np.float64)
    labels = np.full(n, -1, dtype=int)
    k = 0
    gap = gap_days * 86400
    for i in order:
        v, t = vecs[i], times[i]
        if k:
            sims = (cents[:k] @ v) / norms[:k]
            sims[(t - last[:k]) > gap] = -1.0
            j = int(np.argmax(sims))
            if sims[j] >= thr:
                cents[j] += v
                norms[j] = float(np.linalg.norm(cents[j])) or 1.0
                last[j] = t
                labels[i] = j
                continue
        if k == cap:
            cap *= 2
            cents = np.vstack([cents, np.zeros((k, d), np.float32)])
            norms = np.concatenate([norms, np.ones(k, np.float32)])
            last = np.concatenate([last, np.zeros(k)])
        cents[k] = v
        norms[k] = float(np.linalg.norm(v)) or 1.0
        last[k] = t
        labels[i] = k
        k += 1
    return labels


# ---------------------------------------------------------------- labeling
TOPIC_RULES = [
    ("earnings_financials", r"earnings|revenue|profit|guidance|outlook|quarter|q[1-4]\b|buyback|repurchase|dividend|cash flow|capex|margin|deliveries|sales (fell|rose|rise|drop|decline)"),
    ("recalls_safety", r"\brecall|\bnhtsa\b|crash|safety|investigation|probe|defect|killed|fatal"),
    ("regulation_legal", r"lawsuit|sues?\b|court|judge|settle|class action|regulator|tariff|\beu\b|ban\b|antitrust|verdict|fine\b"),
    ("mna_partnerships", r"merger|acquisition|acquire|partnership|joint venture|stake in|deal with|invest(s|ment)? in|spin-?off"),
    ("autonomy_ai", r"self-driving|autonomous|\bfsd\b|robotaxi|cybercab|\bai\b|optimus|robot|autopilot|eyes[- ]off"),
    ("supply_chain_ops", r"supplier|supply|plant|factory|production|battery|rare-earth|magnet|layoff|lay off|jobs|chip|shortage"),
    ("labor_org", r"union|strike|\bceo\b|executive|appoint|names? .* (ceo|chief|president)|leaves|resign|chief of staff"),
    ("products_launch", r"launch|unveil|reveal|debut|new model|roadster|mustang|f-150|cybertruck|model [3ysx]\b|trim|range|package"),
    ("macro_competition", r"china|byd|toyota|hyundai|rivian|competit|market share|ev demand|tariffs?|rates"),
    ("analyst_stock_chatter", r"price target|analyst|rating|options|holdings"),
]
POS_WORDS = r"beat|beats|surge|jump|rise|rises|record|growth|approve|approval|wins?|launch|strong|upgrade|raises|boost|profit surges|rebound|recovery|expands?|invest"
NEG_WORDS = r"miss|falls?|fell|sink|sinks|slump|drop|cuts?|recall|lawsuit|probe|crash|killed|downgrade|warns?|delay|decline|loss|layoff|halt|cancel|concern|pressure|plunge|terminate"

_CHATTER_PATTERNS = [re.compile(p, re.I) for p in (
    r"\b(shares|stake|position|holdings)\b.*\b(sold|purchased|acquired|bought|boosted|trimmed|lifted|cut|raised)\b.*\bby\b",
    r"\bstock\b.*\b(sold|sale|buy|sell|hold|worth|undervalued|overvalued|fair value|outperform\w*|underperform\w*|moves?|prediction|forecast|slides?|jumps?|falls?|rises?|sinks?|surges?|lifts?)\b",
    r"\b(fair value|undervalued|overvalued|price targets?|price prediction|options? (signal|traders?|activity)|stock movers|trading (day|debut)|premarket|pre-market|should you|buy,? hold|better dividend stock|dividend stock|a buy\b|stocks? to buy)\b",
    r"\b(NYSE|NASDAQ)\s*:",
    r"\b(outperforms?|underperforms?) (competitors|broader market|market)\b",
    r"\b(analysts?|wall street)\b.*\b(trim|raise|cut|lower|upgrade|downgrade|adjust)\w*\b.*\btargets?\b",
)]


class _Chatter:
    def search(self, s: str) -> bool:
        return any(p.search(s) for p in _CHATTER_PATTERNS)


FUND_CHATTER = _Chatter()


def heuristic_label(title: str) -> dict:
    t = title.lower()
    topic = "other"
    for name, pat in TOPIC_RULES:
        if re.search(pat, t):
            topic = name
            break
    pos = len(re.findall(POS_WORDS, t))
    neg = len(re.findall(NEG_WORDS, t))
    sent = 1 if pos > neg else -1 if neg > pos else 0
    signal = "opportunity" if sent > 0 else "threat" if sent < 0 else "neutral"
    chatter = topic == "analyst_stock_chatter" or bool(FUND_CHATTER.search(title))
    if chatter:
        topic = "analyst_stock_chatter"
    rel = 1 if chatter else 2 if topic != "other" else 1
    return {"topic": topic, "sentiment": sent, "signal": signal,
            "business_relevance": rel, "chatter": chatter, "method": "heuristic"}


def validate_label(raw: dict) -> dict | None:
    try:
        topic = raw.get("topic", "other")
        topic = topic if topic in TOPICS else "other"
        sent = int(max(-1, min(1, round(float(raw.get("sentiment", 0))))))
        signal = raw.get("signal", "neutral")
        signal = signal if signal in SIGNALS else "neutral"
        rel = int(max(0, min(3, round(float(raw.get("business_relevance", 1))))))
        chatter = bool(raw.get("chatter", topic == "analyst_stock_chatter"))
        return {"topic": topic, "sentiment": sent, "signal": signal,
                "business_relevance": rel, "chatter": chatter, "method": "llm"}
    except (TypeError, ValueError):
        return None


SYSTEM_PROMPT = (
    "You are a financial news analyst. For each numbered headline about the named company, "
    "return one JSON object. Reply with ONLY a JSON array, no prose.\n"
    "Fields: id (int, same as input), "
    f"topic (one of {list(TOPICS)}), "
    "sentiment (-1, 0 or 1: effect on the company's business outlook), "
    "signal ('opportunity', 'threat' or 'neutral' for the company), "
    "business_relevance (0-3: 0 = irrelevant to the company's business, 3 = material to strategy or financials), "
    "chatter (true if it is stock-price commentary, fund holdings, analyst price targets or trading noise)."
)


class LLMState:
    dead = False
    ok_batches = 0
    failed_batches = 0


async def chat(client: httpx.AsyncClient, messages: list[dict]) -> str | None:
    for attempt in range(4):
        try:
            r = await client.post(f"{API_BASE}/chat/completions", json={
                "model": CHAT_MODEL, "messages": messages, "temperature": 0, "max_tokens": 3000})
        except (httpx.TimeoutException, httpx.TransportError):
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        if r.status_code in (429, 500, 502, 503, 504):
            await asyncio.sleep(2 ** attempt + random.random())
            continue
        LLMState.dead = True
        log.error("Chat API fatal HTTP %s: %s  (check VULTR_CHAT_MODEL / key; use --list-models)",
                  r.status_code, r.text[:300])
        print(f"!! Chat API error HTTP {r.status_code}: {r.text[:200]}  -> falling back to keyword labels")
        return None
    return None


def extract_json_array(text: str):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("["), text.rfind("]")
    if a == -1 or b <= a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None


async def label_batch(client, sem, company: str, batch: list[tuple[str, str]]) -> dict[str, dict]:
    """batch: [(event_id, headline)] -> {event_id: label}"""
    result: dict[str, dict] = {}
    if LLMState.dead:
        return result
    lines = "\n".join(f"{i}. {h}" for i, (_, h) in enumerate(batch))
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Company: {company}\nHeadlines:\n{lines}"}]
    async with sem:
        text = await chat(client, msgs)
    arr = extract_json_array(text) if text else None
    if not isinstance(arr, list):
        LLMState.failed_batches += 1
        return result
    LLMState.ok_batches += 1
    for row in arr:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        lab = validate_label(row)
        if lab and 0 <= idx < len(batch):
            result[batch[idx]] = lab[0]
    return result


# ---------------------------------------------------------------- events & scoring
def build_events(company: str, items: list[dict], labels: np.ndarray, now: float) -> list[dict]:
    groups: dict[int, list[dict]] = defaultdict(list)
    for it, lab in zip(items, labels):
        groups[int(lab)].append(it)
    events = []
    for gi, members in groups.items():
        members.sort(key=lambda m: (-TIER_RANK.get(m.get("source_tier", "other"), 1), m["published_at"]))
        rep = members[0]
        times = [parse_ts(m["published_at"]) for m in members]
        sources = set()
        for m in members:
            sources.add(m["source"])
            sources.update(m.get("also_reported_by", []))
        size = sum(1 + m.get("duplicates_merged", 0) for m in members)
        last = max(times)
        age_days = max(0.0, (now - last) / 86400)
        bucket = "24h" if age_days <= 1 else "7d" if age_days <= 7 else "30d"
        eid = hashlib.sha1(f"{company}|{rep['item_id']}".encode()).hexdigest()[:12]
        events.append({
            "event_id": eid, "company": company, "size": size, "n_articles": len(members),
            "n_sources": len(sources), "sources": sorted(sources)[:12],
            "best_tier": rep.get("source_tier", "other"),
            "first_published": datetime.fromtimestamp(min(times), timezone.utc).isoformat(),
            "last_published": datetime.fromtimestamp(last, timezone.utc).isoformat(),
            "age_days": round(age_days, 2), "bucket": bucket,
            "representative": slim(rep), "alternates": [slim(m) for m in members[1:3]],
            "member_item_ids": [m["item_id"] for m in members],
            "label": None, "score": 0.0, "selected": False, "selection_reason": "",
        })
    return events


def slim(it: dict) -> dict:
    return {k: it.get(k) for k in ("item_id", "title", "source", "source_tier",
                                   "google_link", "published_at")}


def score_event(e: dict) -> float:
    lab = e["label"]
    rel = lab["business_relevance"] / 3
    topic_factor = TOPIC_WEIGHT.get(lab["topic"], 0.35) * (0.5 + 0.5 * rel)
    if lab["chatter"]:
        topic_factor *= 0.3
    if lab["business_relevance"] == 0:
        topic_factor *= 0.2
    coverage = min(1.0, math.log1p(e["n_sources"]) / math.log(8))
    recency = 0.5 ** (e["age_days"] / 7)
    tier = TIER_VALUE.get(e["best_tier"], 0.5)
    signal_mult = 1.15 if lab["signal"] in ("opportunity", "threat") else 0.9
    return round(topic_factor * (0.4 * coverage + 0.3 * recency + 0.3 * tier) * signal_mult, 4)


def select_events(events: list[dict], budgets: dict[str, int]) -> list[dict]:
    chosen: list[dict] = []
    ids: set[str] = set()
    topic_count: dict[str, Counter] = defaultdict(Counter)
    global_topic: Counter = Counter()

    def add(e: dict, key: str, cap: int | None, reason: str, use_global: bool = True) -> bool:
        topic = e["label"]["topic"]
        if e["event_id"] in ids:
            return False
        if cap is not None and topic_count[key][topic] >= cap:
            return False
        if use_global and global_topic[topic] >= GLOBAL_TOPIC_CAP:
            return False
        chosen.append(e)
        ids.add(e["event_id"])
        topic_count[key][topic] += 1
        global_topic[topic] += 1
        e["selected"], e["selection_reason"] = True, reason
        return True

    usable = [e for e in events if not e["label"]["chatter"]]
    for b, budget in budgets.items():
        pool = sorted((e for e in usable if e["bucket"] == b), key=lambda e: -e["score"])
        cap = max(1, math.ceil(TOPIC_CAP_FRACTION * budget))
        n = 0
        for e in pool:
            if n >= budget:
                break
            n += add(e, b, cap, f"top of {b} bucket")
        for e in pool:
            if n >= budget:
                break
            n += add(e, b, None, f"{b} bucket (topic cap relaxed)")

    total = sum(budgets.values())
    if len(chosen) < total:
        for e in sorted((e for e in usable if e["event_id"] not in ids), key=lambda e: -e["score"]):
            if len(chosen) >= total:
                break
            add(e, e["bucket"], None, "spill-over from underfilled bucket", use_global=False)

    for sig in ("opportunity", "threat"):
        need = MIN_PER_SIGNAL - sum(1 for e in chosen if e["label"]["signal"] == sig)
        if need <= 0:
            continue
        avail = sorted((e for e in usable if e["event_id"] not in ids and e["label"]["signal"] == sig),
                       key=lambda e: -e["score"])
        for cand in avail[:need]:
            victims = sorted((e for e in chosen if e["label"]["signal"] == "neutral"), key=lambda e: e["score"])
            if not victims:
                break
            v = victims[0]
            chosen.remove(v)
            ids.discard(v["event_id"])
            global_topic[v["label"]["topic"]] -= 1
            v["selected"], v["selection_reason"] = False, ""
            add(cand, cand["bucket"], None, f"swapped in to balance {sig}s", use_global=False)
    return sorted(chosen, key=lambda e: -e["score"])


# ---------------------------------------------------------------- pipeline
async def triage_company(client, sem, key: str, record: dict, now: float, args, embed_model) -> dict:
    company = record["company"]
    items = record["items"]
    if not items:
        return {"company": company, "events": [], "selected": [], "stats": {"items": 0}}
    texts = [i["title"] for i in items]
    times = np.array([parse_ts(i["published_at"]) for i in items])

    vecs, mode = None, "tfidf"
    if embed_model and args.mode in ("auto", "embeddings"):
        vecs = await embed_texts(client, embed_model, texts) if client else None
        if vecs is not None:
            mode = "embeddings"
        elif args.mode == "embeddings":
            sys.exit("Embeddings requested but unavailable.")
    if vecs is None:
        vecs = tfidf_matrix(texts)
    thr = args.sim if args.sim is not None else DEFAULT_SIM[mode]
    labels = leader_cluster(vecs, times, thr, MAX_GAP_DAYS)
    events = build_events(company, items, labels, now)

    cache_path = args.cache
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    todo: list[tuple[str, str]] = []
    for e in events:
        title = e["representative"]["title"]
        ck = hashlib.sha1(f"{CHAT_MODEL}|{company}|{title}".encode()).hexdigest()
        e["_ck"] = ck
        if ck in cache:
            e["label"] = cache[ck]
        elif FUND_CHATTER.search(title):
            e["label"] = heuristic_label(title)
        else:
            todo.append((e["event_id"], title))

    by_id = {e["event_id"]: e for e in events}
    if todo and API_KEY and not args.no_llm:
        batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
        results = await asyncio.gather(*(label_batch(client, sem, company, b) for b in batches))
        for res in results:
            for eid, lab in res.items():
                by_id[eid]["label"] = lab
                cache[by_id[eid]["_ck"]] = lab
    heur = 0
    for e in events:
        if e["label"] is None:
            e["label"] = heuristic_label(e["representative"]["title"])
            heur += 1
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    for e in events:
        e["score"] = score_event(e)
        e.pop("_ck", None)
    selected = select_events(events, BUDGETS)
    events.sort(key=lambda e: -e["score"])
    stats = {
        "items": len(items), "events": len(events), "cluster_mode": mode, "similarity_threshold": thr,
        "labeled_by_heuristic": heur, "singleton_events": sum(1 for e in events if e["n_articles"] == 1),
        "chatter_events": sum(1 for e in events if e["label"]["chatter"]),
        "largest_events": [{"size": e["size"], "title": e["representative"]["title"]}
                           for e in sorted(events, key=lambda e: -e["size"])[:args.show]],
    }
    return {"company": company, "stats": stats, "events": events, "selected": selected}


def print_report(res: dict) -> None:
    s = res["stats"]
    if s.get("items", 0) == 0:
        print(f"\n=== {res['company']} === (no items)")
        return
    sel = res["selected"]
    print(f"\n=== {res['company']} ===")
    print(f"{s['items']} articles -> {s['events']} events  [{s['cluster_mode']}, sim>={s['similarity_threshold']}]"
          f"  singletons: {s['singleton_events']}  chatter: {s['chatter_events']}"
          f"  heuristic-labelled: {s['labeled_by_heuristic']}")
    print("largest events:")
    for x in s["largest_events"]:
        print(f"   {x['size']:>4}  {x['title'][:95]}")
    print(f"selected {len(sel)}: by bucket {dict(Counter(e['bucket'] for e in sel))}")
    print(f"   by signal {dict(Counter(e['label']['signal'] for e in sel))}")
    print(f"   by topic  {dict(Counter(e['label']['topic'] for e in sel))}")
    print(f"   by tier   {dict(Counter(e['best_tier'] for e in sel))}")


async def run(args) -> None:
    in_dir = find_input_dir(args.input, args.base)
    out_dir = args.base / f"triage_{in_dir.name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    args.cache = out_dir / "label_cache.json"
    files = sorted(in_dir.glob("discovery_*.json"))
    if not files:
        sys.exit(f"No discovery_*.json in {in_dir}")
    now = datetime.now(timezone.utc).timestamp()
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
    sem = asyncio.Semaphore(LLM_CONCURRENCY)
    async with httpx.AsyncClient(headers=headers, timeout=90) as client:
        if args.list_models:
            for m in await list_models(client):
                print(m)
            return
        embed_model = None
        if API_KEY and args.mode != "tfidf":
            embed_model = await resolve_embed_model(client)
            print(f"embedding model: {embed_model or 'none found -> TF-IDF fallback'}")
        if not API_KEY:
            print("VULTR_INFERENCE_API_KEY not set -> offline mode (TF-IDF + keyword labels)")
        else:
            print(f"chat model: {CHAT_MODEL}  base: {API_BASE}")
        selected_all = []
        for f in files:
            record = json.loads(f.read_text(encoding="utf-8"))
            res = await triage_company(client, sem, f.stem.replace("discovery_", ""), record, now, args, embed_model)
            print_report(res)
            (out_dir / f"triage_{f.stem.replace('discovery_', '')}.json").write_text(
                json.dumps({"engagement_id": record.get("engagement_id"), "run_id": record.get("run_id"),
                            "stage": "triage", "company": res["company"], "stats": res["stats"],
                            "events": res["events"]}, indent=2), encoding="utf-8")
            for e in res["selected"]:
                selected_all.append({
                    "company": e["company"], "event_id": e["event_id"], "bucket": e["bucket"],
                    "score": e["score"], "label": e["label"], "n_sources": e["n_sources"],
                    "selection_reason": e["selection_reason"],
                    "article": e["representative"], "alternates": e["alternates"],
                })
        (out_dir / "selected_for_resolution.json").write_text(json.dumps({
            "stage": "triage", "generated_at": datetime.now(timezone.utc).isoformat(),
            "llm_batches_ok": LLMState.ok_batches, "llm_batches_failed": LLMState.failed_batches,
            "items": selected_all}, indent=2), encoding="utf-8")
        print(f"\nLLM batches ok/failed: {LLMState.ok_batches}/{LLMState.failed_batches}"
              f"{'  (LLM disabled after fatal error)' if LLMState.dead else ''}")
        print(f"{len(selected_all)} events selected -> {out_dir / 'selected_for_resolution.json'}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Triage stage")
    ap.add_argument("--input", type=Path, default=None, help="discovery run folder (default: newest)")
    ap.add_argument("--base", type=Path, default=Path("output"))
    ap.add_argument("--mode", choices=["auto", "embeddings", "tfidf"], default="auto")
    ap.add_argument("--sim", type=float, default=None, help="cluster similarity threshold")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--show", type=int, default=6, help="how many largest events to print")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(a))


if __name__ == "__main__":
    main()