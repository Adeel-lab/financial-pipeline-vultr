"""
evidence_articles.py (v3) - full-article passage search for the RCA ("Five Whys").

Reads verified article texts written by verify.py (verify_triage_<run>/articles/<event_id>.json),
cuts them into passages, and returns the passages that best match a question. No network, no API
key, no extra packages. Every passage is an exact slice of the verified article text.

Test:
  python evidence_articles.py --verify-dir output/verify_triage_<run> --company "General Motors" \
      --alias GM --alias "General Motors" --query "why did GM EV sales fall in Q3" -k 6
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

STOP = set("""a an and are as at be been but by can could did do does for from had has have he her his how
i if in into is it its may more most not of on or our she should so than that the their them then there
these they this those to too was we were what when where which while who why will with would you your
said says also after about over under between during amid per vs via""".split())

TOKEN = re.compile(r"[a-z0-9]+(?:[.,][0-9]+)*")
BOUNDARY = re.compile(
    r'(?:(?<=[.!?])|(?<=[.!?]["\u201d\u2019)\]]))\s+(?=[A-Z0-9"\u201c\u2018(])')

PHRASES = [
    (re.compile(r"\bthird[- ]quarter\b"), " q3 "), (re.compile(r"\bsecond[- ]quarter\b"), " q2 "),
    (re.compile(r"\bfirst[- ]quarter\b"), " q1 "), (re.compile(r"\bfourth[- ]quarter\b"), " q4 "),
    (re.compile(r"\belectric[- ]vehicles?\b"), " ev "), (re.compile(r"\bgeneral motors\b"), " gm "),
]
DECLINE = ("fell fall falls falling fallen drop dropped drops decline declined declines declining "
           "plunge plunged plummet plummeted slump slumped sank sink slide slid shrank shrink").split()
SYN = {w: "decline" for w in DECLINE}
SYN.update({"deliveries": "sale", "delivery": "sale", "sold": "sale", "selling": "sale"})
CAUSE_CUES = re.compile(
    r"\b(because|due to|after|following|as a result|result of|tied to|driven by|caused by|"
    r"blamed|attributed|reflect(?:s|ed)?|expir(?:y|ed|ation)|ended|removal|withdrawal|"
    r"discontinued|amid|owing to|thanks to|explain(?:s|ed)?)\b", re.I)

K1, B = 1.5, 0.75
TARGET_CHARS, HARD_CHARS, MIN_PASSAGE = 650, 900, 80


def _stem(t: str) -> str:
    if any(c.isdigit() for c in t):
        return t
    if t.endswith("ing") and len(t) > 5:
        return t[:-3]
    if t.endswith("ed") and len(t) > 4:
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    return t


def tokenize(text: str) -> list[str]:
    low = (text or "").lower()
    for rx, rep in PHRASES:
        low = rx.sub(rep, low)
    toks = [SYN.get(t, t) for t in TOKEN.findall(low)]
    return [_stem(t) for t in toks if t not in STOP and len(t) > 1]


def similar(a: str, b: str) -> float:
    """Word overlap between two answers, 0..1 (Jaccard). Used to spot a step that repeats the last one."""
    sa, sb = set(tokenize(a)), set(tokenize(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in BOUNDARY.finditer(text):
        if text[start:m.start()].strip():
            spans.append((start, m.start()))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


def passage_spans(text: str) -> list[tuple[int, int]]:
    """Group sentences into ~650-char passages; consecutive passages share one sentence."""
    spans = sentence_spans(text)
    out, i = [], 0
    while i < len(spans):
        s, j = spans[i][0], i
        while j < len(spans) and spans[j][1] - s <= TARGET_CHARS:
            j += 1
        if j == i:
            j = i + 1
        e = min(spans[j - 1][1], s + HARD_CHARS)
        if e - s >= MIN_PASSAGE:
            out.append((s, e))
        if j >= len(spans):
            break
        i = max(i + 1, j - 1)
    return out


def _norm(s: str) -> str:
    s = (s or "").replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_quote(quote: str, source_text: str) -> bool:
    """True if the quote (ellipsis-separated parts allowed) appears in order in source_text."""
    src, pos = _norm(source_text), 0
    parts = [p for p in re.split(r"\u2026|\.\.\.", quote or "") if _norm(p)]
    if not parts:
        return False
    for part in parts:
        idx = src.find(_norm(part), pos)
        if idx < 0:
            return False
        pos = idx + len(_norm(part))
    return True


class ArticleIndex:
    def __init__(self, passages: list[dict]):
        self.passages = passages
        self.inv: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.lens: list[int] = []
        for pid, p in enumerate(passages):
            toks = tokenize(p["text"])
            self.lens.append(len(toks))
            for term, tf in Counter(toks).items():
                self.inv[term].append((pid, tf))
        self.n = max(1, len(passages))
        self.avgdl = (sum(self.lens) / len(self.lens)) if self.lens else 1.0

    @classmethod
    def from_verify_dir(cls, verify_dir: str | Path) -> "ArticleIndex":
        passages: list[dict] = []
        for f in sorted((Path(verify_dir) / "articles").glob("*.json")):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ver, text = rec.get("verification") or {}, rec.get("text") or ""
            if ver.get("status") != "ok" or len(text) < 200:
                continue
            art, res = rec.get("article") or {}, rec.get("resolved") or {}
            final_url = ver.get("final_url")
            meta = {
                "event_id": rec.get("event_id"),
                "company": rec.get("company") or "",
                "title": art.get("title") or ver.get("title") or "",
                "source": art.get("source") or "",
                "url": final_url or res.get("url") or ver.get("url_used") or "",
                "url_confirmed": bool(final_url),
                "published_at": art.get("published_at") or ver.get("published_at_page") or "",
                "content_hash": ver.get("content_hash") or hashlib.sha256(text.encode()).hexdigest()[:16],
            }
            for s, e in passage_spans(text):
                passages.append({**meta, "start": s, "end": e, "text": text[s:e]})
        return cls(passages)

    def search(self, query: str, k: int = 5, company: str | None = None,
               aliases: list[str] | None = None, max_per_article: int = 2,
               min_rel: float = 0.3, causal_boost: float = 1.25) -> list[dict]:
        """aliases: names that identify the company in text, e.g. ["GM", "General Motors"]."""
        terms = set(tokenize(query))
        if not terms:
            return []
        scores: dict[int, float] = defaultdict(float)
        matched: dict[int, set] = defaultdict(set)
        for term in terms:
            postings = self.inv.get(term)
            if not postings:
                continue
            df = len(postings)
            idf = math.log(1 + (self.n - df + 0.5) / (df + 0.5))
            if any(c.isdigit() for c in term):
                idf *= 1.3
            for pid, tf in postings:
                dl = self.lens[pid]
                scores[pid] += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * dl / self.avgdl))
                matched[pid].add(term)
        alias_rx = None
        names = [a for a in (aliases or []) if a] or ([company] if company else [])
        if names:
            alias_rx = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(a) for a in names), re.I)
        for pid in list(scores):
            text = self.passages[pid]["text"]
            coverage = len(matched[pid]) / len(terms)
            scores[pid] *= 0.4 + 0.6 * coverage
            if alias_rx is not None:
                scores[pid] *= 1.15 if alias_rx.search(text) else 0.6
            if CAUSE_CUES.search(text):
                scores[pid] *= causal_boost
        want = (company or "").lower()
        ranked = sorted(scores.items(), key=lambda x: -x[1])
        if want:
            ranked = [(pid, sc) for pid, sc in ranked if want in self.passages[pid]["company"].lower()]
        if not ranked:
            return []
        top, picked, per_article = ranked[0][1], [], Counter()
        for pid, sc in ranked:
            p = self.passages[pid]
            if sc < min_rel * top or per_article[p["event_id"]] >= max_per_article:
                continue
            per_article[p["event_id"]] += 1
            picked.append({**p, "score": round(sc, 3)})
            if len(picked) >= k:
                break
        return picked


def grade_support(support: list[dict], pool_text: dict[str, str], min_chars: int = 25) -> dict:
    """Check each {"id", "quote"} word for word. The cited id is tried first. If the quote is not
    there but is in another pool item, accept it and record the corrected id (model_cited keeps the
    id the model gave). Returns basis 'reported' only if at least one quote verifies."""
    ok, bad = [], []
    for s in support or []:
        q, cid = str(s.get("quote", "")), str(s.get("id", ""))
        if len(_norm(q)) < min_chars:
            bad.append({"id": cid, "quote": q, "reason": "quote too short"})
            continue
        if cid in pool_text and verify_quote(q, pool_text[cid]):
            ok.append({"id": cid, "quote": q})
            continue
        hit = next((i for i, t in pool_text.items() if verify_quote(q, t)), None)
        if hit:
            ok.append({"id": hit, "quote": q, "model_cited": cid})
        else:
            bad.append({"id": cid, "quote": q, "reason": "not found in any pool item"})
    return {"basis": "reported" if ok else "inference", "verified": ok, "rejected": bad}


def main() -> None:
    ap = argparse.ArgumentParser(description="Search verified article texts")
    ap.add_argument("--verify-dir", type=Path, required=True)
    ap.add_argument("--query", required=True)
    ap.add_argument("--company", default=None)
    ap.add_argument("--alias", action="append", default=[], help="company name variants, repeatable")
    ap.add_argument("-k", type=int, default=6)
    a = ap.parse_args()
    art_dir = a.verify_dir / "articles"
    if not art_dir.is_dir():
        raise SystemExit(f"No 'articles' folder in {a.verify_dir}. Use the verify_triage_<id> folder.")
    idx = ArticleIndex.from_verify_dir(a.verify_dir)
    print(f"{len(idx.passages)} passages from {len({p['event_id'] for p in idx.passages})} articles")
    for i, h in enumerate(idx.search(a.query, k=a.k, company=a.company, aliases=a.alias or None), 1):
        flag = "" if h["url_confirmed"] else "  (link not confirmed)"
        print(f"\n#{i} score={h['score']} {h['source']} {h['published_at'][:10]} {h['url']}{flag}")
        print(h["text"])


if __name__ == "__main__":
    main()