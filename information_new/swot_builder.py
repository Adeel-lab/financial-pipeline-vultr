"""
swot_builder.py (v1.1) - evidence-based SWOT for ANY company, with consultant review.

Two steps
  build   Collects evidence, asks the model to PROPOSE a quadrant and a qualified statement for each item,
          checks every proposal with code, and writes swot_draft.json / swot_draft.md. Nothing is final yet.
  review  The consultant approves, edits or rejects each proposal -> swot_final.json / swot_final.md.

Evidence used
  Financial   metrics from the approved dataset (financial_gate.py), judged by fin_metrics.py (needed only when
              you pass --financial): absolute (generic threshold), historical (trend), relative (needs peer values).
              Financial items are internal, so they can only be strengths or weaknesses.
  News        verified article scores (sentiment.py). Articles about the same development are grouped into one
              "story" so one recall or one earnings report is not counted three times.
  Objective   optional text (--objective) used to judge strategic significance (materiality).

v1.1: model calls are sent in small batches, rate limits are waited out, an unusable reply is retried in smaller
batches, and every failure is printed and saved in swot_draft.json under llm.errors (v1.0 failed silently).

Examples
  python swot_builder.py build --sentiment output/sentiment_RUN --objective "assess expansion readiness"
  python swot_builder.py build --sentiment output/sentiment_RUN --financial output/financial_x/approved_financials.json
  python swot_builder.py review --draft output/swot_TIME/swot_draft.json

Env (optional, same as sentiment.py): VULTR_INFERENCE_BASE, VULTR_INFERENCE_API_KEY, VULTR_CHAT_MODEL
  SWOT_CHUNK (items per model call, default 8)   SWOT_MAX_TOKENS (default 3500)   SWOT_DELAY (seconds between calls)
  SWOT_REASONING_EFFORT (low|medium|high, sent only if set; for reasoning models that accept it)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

API_BASE = os.environ.get("VULTR_INFERENCE_BASE", "https://api.vultrinference.com/v1").rstrip("/")
API_KEY = os.environ.get("VULTR_INFERENCE_API_KEY", "")
CHAT_MODEL = os.environ.get("VULTR_CHAT_MODEL", "deepseek-v4.1-flash")
DELAY = float(os.environ.get("SWOT_DELAY", "0"))
CHUNK = max(1, int(os.environ.get("SWOT_CHUNK", "8")))
MAX_TOKENS = int(os.environ.get("SWOT_MAX_TOKENS", "3500"))
REASONING_EFFORT = os.environ.get("SWOT_REASONING_EFFORT", "")
PROMPT_VERSION = "swot-v1.1"
QUADRANTS = ("strength", "weakness", "opportunity", "threat")
BASES = ("fact", "allegation", "forecast", "inference", "opinion")
PCT_METRICS = {"operating_margin", "net_margin", "gross_margin", "revenue_growth", "net_income_growth",
               "ppe_share_of_assets", "inventory_share_of_assets", "debt_ratio", "equity_ratio", "retained_earnings_to_equity"}
ERRORS: list[str] = []          # every model-call problem of this run
STATE = {"kind": "ok", "abort": False}   # kind of the last failure; abort = provider is down, stop calling


# ------------------------------------------------------------------ small helpers
def norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def split_arg(text: str):
    if "::" in text:
        p, name = text.split("::", 1)
        return Path(p), name.strip()
    return Path(text), None


def company_keys(label: str, profile: dict | None) -> set:
    keys = {norm(label), norm(str(label).split(" (")[0])}
    if profile:
        keys.add(norm(profile.get("name")))
        keys.update(norm(a) for a in (profile.get("all_aliases") or []) + (profile.get("aliases") or []) + (profile.get("tickers") or []))
    keys.discard("")
    return keys


def fmt(metric: str, v: float) -> str:
    return f"{v * 100:.1f}%" if metric in PCT_METRICS else f"{v:.2f}x"


def note_error(msg: str) -> None:
    ERRORS.append(msg)
    print(f"  (model call problem: {msg})", flush=True)


def call_llm(messages: list[dict]):
    """Model text, or None. Every failure is printed and kept in ERRORS. Replaced in tests."""
    STATE["kind"] = "ok"
    if not API_KEY:
        return None
    try:
        import httpx
    except ImportError:
        note_error("httpx is not installed")
        STATE.update(kind="hard", abort=True)
        return None
    payload = {"model": CHAT_MODEL, "messages": messages, "temperature": 0, "max_tokens": MAX_TOKENS}
    if REASONING_EFFORT:
        payload["reasoning_effort"] = REASONING_EFFORT
    for attempt in range(6):
        try:
            r = httpx.post(f"{API_BASE}/chat/completions", headers={"Authorization": f"Bearer {API_KEY}"}, timeout=180, json=payload)
        except httpx.HTTPError as e:
            if attempt == 5:
                note_error(f"network error: {type(e).__name__}")
                STATE.update(kind="hard", abort=True)
                return None
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            try:
                choice = r.json()["choices"][0]
                text = (choice.get("message") or {}).get("content") or ""
                finish = choice.get("finish_reason")
            except (ValueError, KeyError, IndexError):
                note_error("reply was not in the expected format")
                STATE["kind"] = "empty"
                return None
            if not text.strip():
                note_error(f"empty reply (finish_reason={finish}); a reasoning model may have used all {MAX_TOKENS} output tokens - raise SWOT_MAX_TOKENS or set SWOT_REASONING_EFFORT=low")
                STATE["kind"] = "empty"
                return None
            if finish == "length":
                note_error(f"reply cut off at {MAX_TOKENS} tokens")
            if DELAY:
                time.sleep(DELAY)
            return text
        if r.status_code == 413:
            note_error("request too large for the model's limit; lower SWOT_CHUNK")
            STATE["kind"] = "too_large"
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            try:
                wait = float(r.headers.get("retry-after", 0))
            except ValueError:
                wait = 0.0
            wait = max(wait, 15.0 * (attempt + 1)) if r.status_code == 429 else 2 ** attempt * 3
            if attempt == 5:
                note_error(f"HTTP {r.status_code} after 6 attempts (rate limit or provider problem)")
                STATE.update(kind="hard", abort=True)
                return None
            print(f"  (HTTP {r.status_code}, waiting {wait:.0f}s)", flush=True)
            time.sleep(wait)
            continue
        note_error(f"HTTP {r.status_code}: {r.text[:200]}")
        STATE.update(kind="hard", abort=True)
        return None
    return None


def extract_json(text: str):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b < a:
        return None
    try:
        return json.loads(text[a:b + 1])
    except json.JSONDecodeError:
        return None


# ------------------------------------------------------------------ news evidence
def eligible_news(rows: list[dict]):
    ok, excluded = [], []
    for r in rows:
        why = []
        if r.get("method") not in (None, "llm"):
            why.append("keyword score is not accepted as SWOT evidence")
        if r.get("evidence_verified") is not True:
            why.append("evidence quote did not match the article text")
        if r.get("about_company") is False:
            why.append("not materially about the company")
        if r.get("scope_relevance") == "out_of_scope":
            why.append("about places outside the chosen scope")
        if not r.get("url") or not r.get("content_hash") or not r.get("evidence"):
            why.append("incomplete provenance")
        s = r.get("sentiment")
        if not isinstance(s, (int, float)) or abs(s) < 0.25:
            why.append("neutral or missing sentiment")
        if why:
            excluded.append({"event_id": r.get("event_id"), "title": r.get("title"), "reasons": why})
        else:
            ok.append(r)
    return ok, excluded


def tokens(r: dict) -> set:
    return set(re.findall(r"[a-z0-9]{4,}", f"{r.get('title', '')} {r.get('summary', '')}".lower()))


def number_anchors(r: dict) -> set:
    """Leading digits of larger numbers (102,000 / 102K / 102,159 -> '102'); years are ignored."""
    out = set()
    for n in re.findall(r"\d[\d,]*\.?\d*", f"{r.get('title', '')} {r.get('summary', '')}"):
        d = re.sub(r"\D", "", n.split(".")[0])
        if len(d) >= 3 and not re.fullmatch(r"(19|20)\d\d", d):
            out.add(d[:3])
    return out


def group_deterministic(rows: list[dict]) -> list[list[str]]:
    parent = {r["event_id"]: r["event_id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    tk = {r["event_id"]: tokens(r) for r in rows}
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            ta, tb = tk[a["event_id"]], tk[b["event_id"]]
            jac = len(ta & tb) / len(ta | tb) if ta and tb else 0
            if jac >= 0.45 or (jac >= 0.12 and number_anchors(a) & number_anchors(b)):
                parent[find(a["event_id"])] = find(b["event_id"])
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(find(r["event_id"]), []).append(r["event_id"])
    return list(groups.values())


def group_stories(label: str, rows: list[dict], use_llm: bool):
    if len(rows) < 2:
        return [[r["event_id"]] for r in rows], "single"
    if use_llm and API_KEY and not STATE["abort"]:
        lines = "\n".join(f"{r['event_id']} | {r.get('title')} | {r.get('summary')}" for r in rows)
        out = call_llm([{"role": "system", "content": "Group news items that describe the SAME underlying development (same announcement, recall, lawsuit, earnings report or deal). Different developments stay separate even if the topic is similar. Reply with ONLY JSON: {\"groups\":[[\"id\",...],...]} using every id exactly once."},
                        {"role": "user", "content": f"Company: {label}\n{lines}"}])
        obj = extract_json(out) if out else None
        if obj and isinstance(obj.get("groups"), list):
            ids, seen, groups = {r["event_id"] for r in rows}, set(), []
            for g in obj["groups"]:
                g = [x for x in g if x in ids and x not in seen] if isinstance(g, list) else []
                seen.update(g)
                if g:
                    groups.append(g)
            groups += [[x] for x in sorted(ids - seen)]
            return groups, "llm"
        if not STATE["abort"]:
            note_error("story grouping reply unusable; used word overlap instead")
    return group_deterministic(rows), "word_overlap"


def story_items(label: str, rows: list[dict], use_llm: bool):
    by_id = {r["event_id"]: r for r in rows}
    groups, method = group_stories(label, rows, use_llm)
    items = []
    for g in groups:
        mem = sorted((by_id[x] for x in g), key=lambda r: -(r.get("weight") or 0))
        rep = mem[0]
        w = sum((r.get("weight") or 0.01) for r in mem)
        sent = sum((r.get("weight") or 0.01) * r["sentiment"] for r in mem) / w
        domains = {urlparse(r["url"]).hostname for r in mem if r.get("url")}
        items.append({
            "id": "story:" + hashlib.sha1("|".join(sorted(g)).encode()).hexdigest()[:10], "kind": "news",
            "title": rep.get("title"), "summary": rep.get("summary"), "sentiment": round(sent, 2), "bucket": rep.get("bucket"),
            "weight": round(max(r.get("weight") or 0 for r in mem), 4), "n_articles": len(mem), "n_domains": len(domains),
            "member_event_ids": g,
            "evidence": [{"type": "article", "url": r["url"], "content_hash": r["content_hash"], "quote": r["evidence"],
                          "source": r.get("source"), "published_at": r.get("published_at")} for r in mem[:3]]})
    return items, method


# ------------------------------------------------------------------ financial evidence
def fin_items(metrics: dict, approved: dict, path: Path):
    items = []
    src = {"type": "financial", "file": str(path), "file_sha256": sha256(path), "approved_at": approved.get("approved_at"),
           "source_files": approved.get("source_files")}
    for name, m in metrics.items():
        if not m["available"] or not m["latest_is_current_period"]:
            continue
        rel = m["relative"]
        notable = m["absolute_band"] in ("strength", "weakness") or (rel and rel["favourable"] is not None) or m["trend"] in ("improving", "deteriorating")
        if not notable:
            continue
        label = name.replace("_", " ").capitalize()
        parts = [f"{label} is {fmt(name, m['latest'])} as of {m['as_of']}"]
        if m["absolute_band"]:
            th = m["threshold"]
            parts.append(f"{m['absolute_band']} against a generic threshold ({fmt(name, th['weak'])} weak / {fmt(name, th['strong'])} strong)")
        parts.append(f"trend {m['trend'].replace('_', ' ')}")
        if rel:
            parts.append(f"{rel['position'].replace('_', ' ')} (peer value {fmt(name, rel['peer_value'])}, {rel['source']})")
        if m["uses_partial_input"]:
            parts.append("uses a partial input, treat as a lower bound")
        quad = None
        if m["absolute_band"] in ("strength", "weakness"):
            quad = m["absolute_band"]
        elif rel and rel["favourable"] is not None:
            quad = "strength" if rel["favourable"] else "weakness"
        items.append({"id": "fin:" + name, "kind": "financial", "metric": name, "title": label, "statement_rule": "; ".join(parts),
                      "latest": m["latest"], "as_of": m["as_of"], "absolute_band": m["absolute_band"], "trend": m["trend"],
                      "relative": rel, "partial": bool(m["uses_partial_input"]), "history": m["history"], "rule_quadrant": quad,
                      "evidence": [src | {"metric": name, "definition": m["definition"]}]})
    return items


# ------------------------------------------------------------------ classification
SYSTEM_PROMPT = (
    "You are a strategy analyst preparing a SWOT draft that a consultant will review. For each evidence item decide: "
    "quadrant = strength | weakness | opportunity | threat | not_swot. Strengths and weaknesses are INTERNAL to the company "
    "(capabilities, resources, financial position); opportunities and threats are EXTERNAL (market, regulation, competitors, "
    "economy). Positive news is not automatically an opportunity and negative news is not automatically a threat. Announced or "
    "planned capabilities are forecasts, not achieved strengths. Lawsuit claims are allegations, not facts. Financial items are "
    "internal: use only strength, weakness or not_swot for them. Use not_swot for trivia, sponsorships or immaterial items. "
    "basis = fact | allegation | forecast | inference | opinion. materiality = 1 (minor) to 3 (major) for the stated objective. "
    "For news items write ONE qualified sentence 'statement' (max 250 characters) using ONLY facts present in the item - no new "
    "numbers, names or causes. For financial items leave statement empty. 'why' = short reasoning for internal vs external. "
    "Copy each item's id EXACTLY as given. Keep every field short. "
    'Reply with ONLY JSON: {"items":[{"id":"...","quadrant":"...","basis":"...","materiality":1,"confidence":0.0,"statement":"...","why":"..."}]}'
)


def describe(it: dict) -> str:
    if it["kind"] == "financial":
        return f"{it['id']} | FINANCIAL | {it['statement_rule']}"
    q = it["evidence"][0]["quote"]
    return (f"{it['id']} | NEWS | {it['title']} | summary: {it['summary']} | sentiment {it['sentiment']:+.2f} | "
            f"{it['n_articles']} article(s) from {it['n_domains']} site(s) | quote: {q}")


def numbers_in(text: str) -> set:
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text or "")}


def validate(it: dict, c: dict):
    """-> (proposal, notes). Code rules that the model cannot override."""
    notes = []
    q = c.get("quadrant")
    if q not in QUADRANTS + ("not_swot",):
        return None, ["invalid quadrant"]
    basis = c.get("basis") if c.get("basis") in BASES else "inference"
    try:
        mat = int(max(1, min(3, round(float(c.get("materiality", 2))))))
        conf = max(0.0, min(1.0, float(c.get("confidence", 0.5))))
    except (TypeError, ValueError):
        mat, conf = 2, 0.5
    why = str(c.get("why", ""))[:200]
    if it["kind"] == "financial":
        if q in ("opportunity", "threat"):
            return None, ["financial items are internal; opportunity/threat rejected"]
        rq = it.get("rule_quadrant")
        if rq and q not in (rq, "not_swot"):
            notes.append(f"model said {q} but the threshold/peer rule says {rq}; rule kept")
            q = rq
        elif rq and q == "not_swot":
            notes.append("model judged this immaterial; kept as proposal for the reviewer")
        statement = it["statement_rule"]
    else:
        statement = re.sub(r"\s+", " ", str(c.get("statement", ""))).strip()[:250]
        source_text = " ".join([it["title"] or "", it["summary"] or ""] + [e["quote"] for e in it["evidence"]]).replace(",", "")
        if not statement or not numbers_in(statement) <= numbers_in(source_text):
            notes.append("statement had missing or unsupported numbers; replaced by the evidence summary")
            statement = it["summary"] or it["title"]
        blob = f"{it['title']} {it['summary']}".lower()
        if basis == "fact" and re.search(r"alleg|class action|lawsuit|accus|sues\b|claims? that", blob):
            basis = "allegation"
            notes.append("legal claim: basis set to allegation")
        if basis == "allegation" and re.search(r"\bproves?\b|\bproven\b|\bguilty\b|\bknowingly\b|\badmitted\b|\bcaused\b", statement, re.I):
            statement = it["summary"] or it["title"]
            notes.append("statement presented an allegation as established; replaced by the evidence summary")
    return {"quadrant": None if q == "not_swot" else q, "not_swot": q == "not_swot", "basis": basis, "materiality": mat,
            "confidence": conf, "statement": statement, "why": why, "origin": "llm"}, notes


def classify_chunk(label: str, scope: str, objective: str, chunk: list[dict], results: dict, depth: int = 0) -> None:
    """One model call; items that get no valid proposal are retried in two smaller calls."""
    if STATE["abort"]:
        return
    user = (f"Company: {label}\nScope: {scope}\nObjective: {objective or 'not stated (judge general materiality)'}\n\nITEMS:\n"
            + "\n".join(describe(x) for x in chunk))
    text = call_llm([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}])
    if STATE["abort"]:
        return
    obj = extract_json(text) if text else None
    if text and obj is None:
        note_error("the model replied, but not with the expected JSON")
    by_id = {x["id"]: x for x in chunk}
    for c in (obj or {}).get("items", []) if isinstance(obj, dict) else []:
        if not isinstance(c, dict):
            continue
        key = str(c.get("id", "")).strip()
        if key not in by_id:                      # tolerate a dropped prefix: "abc123" instead of "story:abc123"
            key = next((k for k in by_id if k.split(":", 1)[-1] == key.split(":", 1)[-1]), key)
        if key in by_id and key not in results:
            results[key] = validate(by_id[key], c)
    missing = [x for x in chunk if x["id"] not in results]
    can_split = obj is not None or (text is None and STATE["kind"] in ("empty", "too_large"))   # splitting cannot fix a wrong format
    if not can_split:
        return
    if missing and depth < 2 and len(missing) > 1:
        half = (len(missing) + 1) // 2
        classify_chunk(label, scope, objective, missing[:half], results, depth + 1)
        classify_chunk(label, scope, objective, missing[half:], results, depth + 1)
    elif missing and depth < 2:
        classify_chunk(label, scope, objective, missing, results, depth + 1)


def classify(label: str, scope: str, objective: str, items: list[dict], use_llm: bool):
    results: dict = {}
    if use_llm and API_KEY and items:
        for i in range(0, len(items), CHUNK):
            classify_chunk(label, scope, objective, items[i:i + CHUNK], results)
            print(f"  {label}: {len(results)}/{len(items)} items classified by the model", flush=True)
    for it in items:
        prop, notes = results.get(it["id"], (None, ["no valid model proposal"]))
        if prop is None and it["kind"] == "financial" and it.get("rule_quadrant"):
            prop = {"quadrant": it["rule_quadrant"], "not_swot": False, "basis": "fact", "materiality": 2, "confidence": 0.5,
                    "statement": it["statement_rule"], "why": "threshold/peer rule", "origin": "rule"}
        if prop is None:
            prop = {"quadrant": None, "not_swot": False, "basis": "inference", "materiality": None, "confidence": 0.0,
                    "statement": it.get("statement_rule") or it.get("summary") or it.get("title"), "why": "", "origin": "none"}
        it["proposal"], it["notes"] = prop, notes
        it["status"] = "proposed" if prop["quadrant"] or prop["not_swot"] else "needs_classification"
    return items


# ------------------------------------------------------------------ build
def build_draft(sentiment_dir: Path | None, financial: list[tuple[Path, str | None]], peers: tuple[Path, str | None] | None,
                objective: str, use_llm: bool) -> dict:
    ERRORS.clear()
    STATE.update(kind="ok", abort=False)
    fin_metrics = None
    if financial:
        import fin_metrics as fin_metrics          # only needed when financial files are given
    companies, rows_all, sent_src = {}, [], None
    if sentiment_dir:
        sf = sentiment_dir / "company_sentiment.json" if sentiment_dir.is_dir() else sentiment_dir
        companies = load(sf).get("companies", {})
        af = sf.parent / "article_scores.json"
        rows_all = load(af).get("items", []) if af.exists() else []
        sent_src = {"file": str(sf), "sha256": sha256(sf)}
    fin_for, extra = {}, []
    for path, forced in financial:
        ap = load(path)
        want = norm(forced or ap.get("company"))
        hit = [l for l, b in companies.items() if want in company_keys(l, b.get("profile"))]
        if len(hit) == 1 and hit[0] not in fin_for:
            fin_for[hit[0]] = (path, ap)
        elif len(hit) > 1:
            sys.exit(f"{path} matches several companies ({', '.join(hit)}); add ::Exact company label")
        else:
            extra.append((path, ap))
    for path, ap in extra:
        fin_for[ap.get("company") or path.stem] = (path, ap)
        companies.setdefault(ap.get("company") or path.stem, {})
    peer_vals = {}
    if peers:
        import fin_metrics as fin_metrics
        pv = fin_metrics.load_peers(peers[0])
        peer_vals = {None: pv} if not peers[1] else {norm(peers[1]): pv}
    out = {"stage": "swot_draft", "generated_at": datetime.now(timezone.utc).isoformat(), "status": "proposals_need_review",
           "objective": objective or None, "prompt_version": PROMPT_VERSION, "sentiment_source": sent_src,
           "llm": {"enabled": bool(use_llm and API_KEY), "model": CHAT_MODEL if use_llm and API_KEY else None, "chunk": CHUNK, "errors": []},
           "companies": {}}
    for label, block in companies.items():
        profile = block.get("profile") or {}
        scope = profile.get("scope_text", "global (whole company)")
        warnings = ["Opportunities and threats rest on a selected news sample, not a complete market review.",
                    "Quote matching checks text presence, not truth or support for an inference.",
                    "Every item is a proposal until the consultant approves it."]
        items, excluded, grouping, fin_cov = [], [], None, None
        if label in fin_for:
            path, ap = fin_for[label]
            pv = peer_vals.get(norm(label)) or peer_vals.get(norm(label.split(" (")[0])) or peer_vals.get(None) or {}
            metrics = fin_metrics.compute(ap, pv)
            items += fin_items(metrics, ap, path)
            fin_cov = {"file": str(path), "company_in_file": ap.get("company"), "frequency": ap.get("frequency"), "periods": len(ap["periods"]),
                       "latest_period": ap["periods"][0] if ap["periods"] else None,
                       "absolute": sum(m["absolute_band"] is not None for m in metrics.values()),
                       "historical": sum(m["trend"] in ("improving", "deteriorating", "stable") for m in metrics.values()),
                       "relative": sum(m["relative"] is not None for m in metrics.values()),
                       "unavailable": [k for k, m in metrics.items() if not m["available"]],
                       "partial": sorted({f for m in metrics.values() for f in m["uses_partial_input"]})}
            if fin_cov["partial"]:
                warnings.append("Partial inputs (" + ", ".join(fin_cov["partial"]) + ") make related metrics lower bounds.")
            if not fin_cov["relative"]:
                warnings.append("No peer benchmark supplied: relative (vs competitors) assessment is not available.")
            if profile.get("scope") == "local":
                warnings.append("Financial statements may describe the whole company, not the chosen location(s): " + scope)
            if ap.get("frequency") == "quarterly":
                warnings.append("Quarterly statements: check income-statement figures are single-quarter, not cumulative.")
        else:
            warnings.append("No approved financial dataset for this company: strengths and weaknesses can come from news only.")
        rows = [r for r in rows_all if r.get("company") == label]
        if rows:
            ok, excluded = eligible_news(rows)
            stories, grouping = story_items(label, ok, use_llm) if ok else ([], None)
            items += stories
        elif sentiment_dir:
            warnings.append("No scored articles for this company.")
        items = classify(label, scope, objective, items, use_llm)
        items.sort(key=lambda x: (-(x["proposal"]["materiality"] or 0), -(x.get("weight") or 0)))
        out["companies"][label] = {
            "profile": profile or None, "scope": scope, "coverage": block.get("coverage"), "financial": fin_cov,
            "assessments": {"absolute": bool(fin_cov and fin_cov["absolute"]), "historical": bool(fin_cov and fin_cov["historical"]),
                            "relative": bool(fin_cov and fin_cov["relative"]), "strategic_significance": bool(objective)},
            "story_grouping": grouping, "items": items, "excluded_news": excluded, "warnings": warnings}
    out["llm"]["errors"] = list(ERRORS)
    return out


# ------------------------------------------------------------------ review
def apply_review(draft: dict, decisions: list[dict], reviewer: str = "consultant") -> dict:
    """decisions: [{"company":..., "id":..., "action":"approve|edit|reject", "quadrant":..., "statement":..., "note":...}]"""
    dmap = {(d["company"], d["id"]): d for d in decisions}
    final = {"stage": "swot_final", "finalised_at": datetime.now(timezone.utc).isoformat(), "reviewer": reviewer,
             "objective": draft.get("objective"), "draft_generated_at": draft["generated_at"], "companies": {}}
    for label, c in draft["companies"].items():
        quad = {q: [] for q in QUADRANTS}
        rejected, pending = [], []
        for it in c["items"]:
            d = dmap.get((label, it["id"]))
            p = it["proposal"]
            if not d or d.get("action") not in ("approve", "edit", "reject"):
                pending.append({"id": it["id"], "title": it.get("title"), "proposed_quadrant": p["quadrant"]})
                continue
            if d["action"] == "reject":
                rejected.append({"id": it["id"], "title": it.get("title"), "note": d.get("note", "")})
                continue
            q = d.get("quadrant") if d["action"] == "edit" and d.get("quadrant") else p["quadrant"]
            if q not in QUADRANTS:
                raise ValueError(f"{label} / {it['id']}: choose a quadrant before approving")
            if it["kind"] == "financial" and q in ("opportunity", "threat"):
                raise ValueError(f"{it['id']}: financial items are internal (strength or weakness only)")
            quad[q].append({"id": it["id"], "kind": it["kind"], "statement": (d.get("statement") or p["statement"]),
                            "edited": d["action"] == "edit", "basis": p["basis"], "materiality": p["materiality"],
                            "why": p["why"], "reviewer_note": d.get("note", ""), "evidence": it["evidence"]})
        final["companies"][label] = {"scope": c["scope"], "swot": quad, "rejected": rejected, "pending": pending,
                                     "warnings": c["warnings"], "assessments": c["assessments"]}
    return final


def markdown(doc: dict, final: bool) -> str:
    L = [f"# SWOT {'(final, reviewed)' if final else '(DRAFT - proposals awaiting review)'}", ""]
    if doc.get("objective"):
        L += [f"Objective: {doc['objective']}", ""]
    if not final and doc.get("llm", {}).get("errors"):
        L += ["Model problems during this build: " + "; ".join(sorted(set(doc["llm"]["errors"]))[:5]), ""]
    for label, c in doc["companies"].items():
        L += [f"## {label}", f"Scope: {c['scope']}", ""]
        if final:
            for q in QUADRANTS:
                L += [f"### {q.capitalize()}", ""]
                for x in c["swot"][q]:
                    L.append(f"- {x['statement']} [{x['basis']}, materiality {x['materiality']}]{' (edited)' if x['edited'] else ''}")
                    for e in x["evidence"][:3]:
                        L.append("  - evidence: " + (f"{e['url']} (hash {e['content_hash']})" if e["type"] == "article" else f"financial file {e['file']} / {e.get('metric')}"))
                if not c["swot"][q]:
                    L.append("No approved items.")
                L.append("")
            L += [f"Pending: {len(c['pending'])} | Rejected: {len(c['rejected'])}", ""]
        else:
            for q in QUADRANTS + (None,):
                its = [i for i in c["items"] if i["proposal"]["quadrant"] == q and not i["proposal"]["not_swot"]]
                L += [f"### {q.capitalize() if q else 'Needs classification'}", ""]
                for i in its:
                    p = i["proposal"]
                    L.append(f"- {p['statement']} [{p['basis']}, materiality {p['materiality']}, {p['origin']}] `{i['id']}`")
                    for e in i["evidence"][:2]:
                        L.append("  - " + (f"{e['url']} (hash {e['content_hash']})" if e["type"] == "article" else f"financial: {e.get('metric')}"))
                    for n in i.get("notes", []):
                        L.append(f"  - note: {n}")
                if not its:
                    L.append("None.")
                L.append("")
            ns = [i for i in c["items"] if i["proposal"]["not_swot"]]
            if ns:
                L += [f"Judged not relevant to the SWOT: {len(ns)} item(s)", ""]
        L += ["### Warnings", ""] + [f"- {w}" for w in c["warnings"]] + [""]
    return "\n".join(L)


def interactive_review(draft: dict) -> list[dict]:
    decisions = []
    for label, c in draft["companies"].items():
        print(f"\n=== {label} ({c['scope']}) - {len(c['items'])} proposals ===")
        for it in c["items"]:
            p = it["proposal"]
            print(f"\n[{it['id']}] proposed: {p['quadrant'] or ('not relevant' if p['not_swot'] else 'UNCLASSIFIED')} | {p['basis']} | materiality {p['materiality']}")
            print("  " + p["statement"])
            if p["why"]:
                print("  why: " + p["why"])
            for n in it.get("notes", []):
                print("  note: " + n)
            for e in it["evidence"][:2]:
                print("  evidence:", e.get("url") or f"{e.get('metric')} ({e.get('file')})")
            ans = input("  [Enter]=approve  e=edit  r=reject  s=skip  q=quit: ").strip().lower()
            if ans == "q":
                return decisions
            if ans == "r":
                decisions.append({"company": label, "id": it["id"], "action": "reject", "note": input("  reason: ").strip()})
            elif ans == "e" or (ans == "" and not p["quadrant"]):
                q = input("  quadrant [s/w/o/t]: ").strip().lower()
                qm = {"s": "strength", "w": "weakness", "o": "opportunity", "t": "threat"}
                st = input("  statement (Enter = keep): ").strip()
                decisions.append({"company": label, "id": it["id"], "action": "edit", "quadrant": qm.get(q, p["quadrant"]), "statement": st or None})
            elif ans == "":
                decisions.append({"company": label, "id": it["id"], "action": "approve"})
    return decisions


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="create the draft")
    b.add_argument("--sentiment", type=Path, help="sentiment run folder (or company_sentiment.json)")
    b.add_argument("--financial", action="append", default=[], metavar="FILE[::Company]", help="approved_financials.json")
    b.add_argument("--peers", metavar="FILE[::Company]", help="optional peer benchmark JSON")
    b.add_argument("--objective", default="", help="what decision this SWOT supports")
    b.add_argument("--no-llm", action="store_true")
    b.add_argument("--outdir", type=Path, default=Path("output"))
    r = sub.add_parser("review", help="approve / edit / reject proposals")
    r.add_argument("--draft", type=Path, required=True)
    r.add_argument("--decisions", type=Path, help="JSON list of decisions instead of the interactive prompts")
    r.add_argument("--reviewer", default="consultant")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if a.cmd == "build":
        if not a.sentiment and not a.financial:
            sys.exit("Give at least --sentiment or --financial.")
        if not API_KEY and not a.no_llm:
            print("(no VULTR_INFERENCE_API_KEY set: rule-based proposals only; news items will need classification)")
        draft = build_draft(a.sentiment, [split_arg(f) for f in a.financial], split_arg(a.peers) if a.peers else None, a.objective, not a.no_llm)
        out = a.outdir / f"swot_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "swot_draft.json").write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "swot_draft.md").write_text(markdown(draft, False), encoding="utf-8")
        total_unclassified = 0
        for label, c in draft["companies"].items():
            cnt = {q: sum(1 for i in c["items"] if i["proposal"]["quadrant"] == q) for q in QUADRANTS}
            unc = sum(1 for i in c["items"] if i["status"] == "needs_classification")
            total_unclassified += unc
            print(f"{label}: {cnt}, unclassified {unc}, not relevant {sum(1 for i in c['items'] if i['proposal']['not_swot'])}")
        if draft["llm"]["enabled"] and total_unclassified:
            print(f"\nWARNING: {total_unclassified} item(s) got no usable model proposal."
                  + (" Reasons: " + "; ".join(sorted(set(draft['llm']['errors']))[:3]) if draft["llm"]["errors"] else " The model replied, but not in the expected format."))
        print("Saved ->", out / "swot_draft.json", "\nNext: python swot_builder.py review --draft", out / "swot_draft.json")
    else:
        draft = load(a.draft)
        decisions = load(a.decisions) if a.decisions else interactive_review(draft)
        final = apply_review(draft, decisions, a.reviewer)
        (a.draft.parent / "swot_final.json").write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
        (a.draft.parent / "swot_final.md").write_text(markdown(final, True), encoding="utf-8")
        (a.draft.parent / "review_decisions.json").write_text(json.dumps(decisions, indent=2), encoding="utf-8")
        print("Saved ->", a.draft.parent / "swot_final.json")


if __name__ == "__main__":
    main()