"""Search saved management explanations for use in the Five Whys.

Reads filing_explanations.json produced by filing_explanations.py.
Uses only excerpts marked verified by the collector.

These excerpts describe management's explanations, not independently
established causes. This module makes no network or model calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "was", "were",
    "are", "why", "how", "has", "have", "had", "its", "into", "than",
    "not", "but", "our", "their",
}


def _tokens(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z][a-z0-9-]{2,}", text.lower())
        if word not in STOP_WORDS
    }


class FilingIndex:
    def __init__(self, path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()

        data = json.loads(raw.decode("utf-8-sig"))
        self.company = data.get("company") or "The company"
        self.items = []

        for number, item in enumerate(data.get("items") or []):
            quote = str(item.get("quote") or "").strip()
            url = str(item.get("url") or "").strip()

            if item.get("verified") is not True or not quote or not url:
                continue

            topic = str(item.get("topic") or "")
            form = str(item.get("form") or "")
            period = str(item.get("period") or "")

            self.items.append({
                "id": (
                    f"filing:{form}:{period}:"
                    f"{topic.replace(' ', '_')}:{number}"
                ),
                "topic": topic,
                "quote": quote,
                "url": url,
                "form": form,
                "period": period,
                "filing_date": str(item.get("filing_date") or ""),
                "_tokens": _tokens(f"{topic} {quote}"),
                "_topic_tokens": _tokens(topic),
            })

    def search(self, query: str, k: int = 2, exclude=()) -> list[dict]:
        query_tokens = _tokens(query)
        excluded = set(exclude or ())
        matches = []

        for item in self.items:
            if item["id"] in excluded:
                continue

            score = (
                len(query_tokens & item["_tokens"])
                + 2 * len(query_tokens & item["_topic_tokens"])
            )

            if score > 0:
                matches.append((score, item))

        matches.sort(key=lambda match: -match[0])

        return [
            {
                key: value
                for key, value in item.items()
                if not key.startswith("_")
            }
            for _, item in matches[:max(0, k)]
        ]