"""
evidence_regulatory.py - turns federal_register.json (written by federal_register.py) into quotes for the Five Whys.

Each item is one sentence copied from the title or abstract of an official Federal Register document (a rule, proposed
rule, notice or presidential document). It shows what the government published or proposed. It never shows, by itself,
that this affected the company.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

STOP = {"the", "a", "an", "of", "to", "in", "and", "or", "for", "is", "was", "are", "why", "this", "that", "with",
        "by", "on", "at", "as", "it", "its", "be", "from", "than", "has", "have", "had", "not"}


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z&\-]{2,}", (s or "").lower()) if w not in STOP}


class RegulatoryIndex:
    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw.decode("utf-8-sig"))
        self.company = data.get("company") or "The company"
        self.items = []
        for it in data.get("items") or []:
            if not it.get("verified") or not it.get("quote") or not it.get("url"):
                continue
            self.items.append({
                "id": f"fr:{it.get('document_number')}", "quote": it["quote"], "title": it.get("title", ""), "url": it["url"],
                "type": it.get("type") or "Document", "agencies": it.get("agencies") or [], "published": it.get("publication_date") or "",
                "effective_on": it.get("effective_on"), "comments_close_on": it.get("comments_close_on"),
                "scope": it.get("scope", ""), "topic": it.get("topic", ""),
                "tokens": _tokens(f"{it.get('title', '')} {it['quote']} {it.get('topic', '')}"), "topic_tokens": _tokens(it.get("topic", ""))})

    def search(self, query: str, k: int = 2, exclude=()):
        q = _tokens(query)
        scored = []
        for h in self.items:
            if h["id"] in exclude:
                continue
            score = len(q & h["tokens"]) + 2 * len(q & h["topic_tokens"])
            if score > 0:
                scored.append((score, h))
        scored.sort(key=lambda x: -x[0])
        return [{k_: v for k_, v in h.items() if k_ not in ("tokens", "topic_tokens")} for _, h in scored[:k]]
