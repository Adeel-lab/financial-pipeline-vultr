"""
Stage 3 - Resolve: Google News redirect links -> real publisher URLs (only for triage-selected events).

Input : output/triage_<run>/selected_for_resolution.json   (from triage.py)
Output: output/triage_<run>/resolved.json (+ resolved_failures.json)

How it works
1. Old-style ids embed the URL; decode them offline (no request).
2. Newer ids are opaque: fetch the Google News article page to read a signature + timestamp,
   then call Google's batchexecute RPC to get the publisher URL.
   FIX: one RPC per article (RPC_CHUNK = 1). With 10 per call the replies can come back in a
   different order than the requests, which attached URLs to the WRONG events.
3. If an event's main article fails, its alternates are tried.
4. NEW: after the main URL is found, up to MAX_ALT other articles of the same event are resolved
   too ("resolved_alternates") so verify.py has real publisher fall-backs, never Google links.
5. Every URL is safety-checked (http/https only, no private/loopback/metadata addresses).

Needs: pip install httpx
Run  : python resolve.py            (uses the newest triage run)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import logging
import os
import random
import re
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

log = logging.getLogger("resolve")

GN_BASE = os.environ.get("GN_BASE", "https://news.google.com").rstrip("/")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PAGE_CONCURRENCY = 3
PAGE_DELAY = 0.3
RPC_CHUNK = 1          # MUST stay 1: batched replies are not guaranteed to be in request order
RPC_DELAY = 0.25
RETRIES = 3
MAX_ALT = 2
COOKIES = {"CONSENT": "YES+cb.20240101-00-p0.en+FX+000",
           "SOCS": "CAESEwgDEgk0ODE3Nzk3MjQaAmVuIAEaBgiA_LyaBg"}

_SIG = re.compile(r'data-n-a-sg="([^"]+)"')
_TS = re.compile(r'data-n-a-ts="([^"]+)"')
_URL_IN_BYTES = re.compile(rb"https?://[\x21-\x7e]+")


# ---------------------------------------------------------------- url safety
def host_is_blocked(host: str) -> bool:
    h = host.lower().strip("[]")
    if h in ("localhost", "metadata.google.internal") or h.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


def basic_url_check(url: str) -> str | None:
    """Return a failure reason, or None if the URL passes the static checks."""
    try:
        p = urlparse(url)
    except ValueError:
        return "malformed_url"
    if p.scheme not in ("http", "https") or not p.hostname:
        return "bad_scheme"
    if p.hostname.endswith("google.com"):
        return "still_google"
    if host_is_blocked(p.hostname):
        return "blocked_host"
    return None


async def dns_check(host: str) -> str | None:
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return "dns_failure"
    for info in infos:
        if host_is_blocked(info[4][0]):
            return "resolves_to_private_address"
    return None


# ---------------------------------------------------------------- decoding
def gn_article_id(link: str) -> str | None:
    m = re.search(r"/articles/([^/?#]+)", link or "")
    return m.group(1) if m else None


def decode_offline(art_id: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(art_id + "=" * (-len(art_id) % 4))
    except Exception:
        return None
    if raw.startswith(b"\x08\x13\x22") or b"AU_yqL" not in raw[:12]:
        m = _URL_IN_BYTES.search(raw)
        if m:
            return m.group(0).decode("ascii", "ignore")
    return None


async def get_with_retry(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    for attempt in range(RETRIES):
        try:
            r = await client.get(url)
        except (httpx.TimeoutException, httpx.TransportError):
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            await asyncio.sleep(2 ** attempt + random.random())
            continue
        return r
    return None


async def fetch_params(client, sem, art_id: str) -> tuple[dict | None, str]:
    """Fetch the article page and read the signature/timestamp attributes."""
    reason = "no_signature"
    for path in (f"/articles/{art_id}", f"/rss/articles/{art_id}"):
        async with sem:
            r = await get_with_retry(client, f"{GN_BASE}{path}?hl=en-US&gl=US&ceid=US:en")
            await asyncio.sleep(PAGE_DELAY)
        if r is None:
            reason = "page_unreachable"
            continue
        if "consent.google.com" in str(r.url):
            reason = "consent_wall"
            continue
        if r.status_code != 200:
            reason = f"page_http_{r.status_code}"
            continue
        sg, ts = _SIG.search(r.text), _TS.search(r.text)
        if sg and ts:
            return {"id": art_id, "sig": sg.group(1), "ts": ts.group(1)}, ""
    return None, reason


def build_payload(params: list[dict]) -> str:
    reqs = []
    for p in params:
        arg = (f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
               f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{p["id"]}",{p["ts"]},"{p["sig"]}"]')
        reqs.append(["Fbv4je", arg])
    return "f.req=" + quote(json.dumps([reqs]))


def parse_rpc(text: str) -> list[str | None]:
    """Return the publisher URL (or None) for every Fbv4je entry, in response order."""
    start = text.find("[")
    if start == -1:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return []
    out: list[str | None] = []
    for row in data:
        if not (isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr" and row[1] == "Fbv4je"):
            continue
        url = None
        if isinstance(row[2], str):
            try:
                inner = json.loads(row[2])
                if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], str):
                    url = inner[1]
            except json.JSONDecodeError:
                pass
        out.append(url)
    return out


async def rpc_chunk(client, params: list[dict]) -> list[str | None] | None:
    for attempt in range(RETRIES):
        try:
            r = await client.post(
                f"{GN_BASE}/_/DotsSplashUi/data/batchexecute",
                content=build_payload(params),
                headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"})
        except (httpx.TimeoutException, httpx.TransportError):
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return parse_rpc(r.text)
        if r.status_code in (429, 500, 502, 503, 504):
            await asyncio.sleep(2 ** attempt + random.random())
            continue
        log.warning("batchexecute HTTP %s", r.status_code)
        return None
    return None


async def resolve_ids(client, sem, ids: list[str], do_dns: bool) -> dict[str, dict]:
    """art_id -> {url, method, reason}"""
    results: dict[str, dict] = {}
    need_rpc: list[str] = []
    for i in dict.fromkeys(ids):
        url = decode_offline(i)
        if url:
            results[i] = {"url": url, "method": "offline", "reason": ""}
        else:
            need_rpc.append(i)

    fetched = await asyncio.gather(*(fetch_params(client, sem, i) for i in need_rpc))
    ready: list[dict] = []
    for i, (p, reason) in zip(need_rpc, fetched):
        if p:
            ready.append(p)
        else:
            results[i] = {"url": None, "method": "rpc", "reason": reason}

    for k in range(0, len(ready), RPC_CHUNK):
        chunk = ready[k:k + RPC_CHUNK]
        urls = await rpc_chunk(client, chunk)
        if urls is None or len(urls) != len(chunk):
            urls = [None] * len(chunk)        # never guess when the reply does not line up
        for p, u in zip(chunk, urls):
            results[p["id"]] = {"url": u, "method": "rpc", "reason": "" if u else "rpc_returned_nothing"}
        await asyncio.sleep(RPC_DELAY)

    for i, res in results.items():
        if not res["url"]:
            continue
        bad = basic_url_check(res["url"])
        if not bad and do_dns:
            bad = await dns_check(urlparse(res["url"]).hostname)
        if bad:
            res["reason"], res["url"] = bad, None
    return results


# ---------------------------------------------------------------- pipeline
def find_input(arg: Path | None, base: Path) -> Path:
    if arg:
        return arg
    files = list(base.glob("triage_*/selected_for_resolution.json"))
    if not files:
        sys.exit(f"No selected_for_resolution.json under {base}. Run triage.py first.")
    return max(files, key=lambda p: p.stat().st_mtime)


async def run(args) -> None:
    src = find_input(args.input, args.base)
    data = json.loads(src.read_text(encoding="utf-8"))
    items = data["items"]
    print(f"input: {src} ({len(items)} events)")
    sem = asyncio.Semaphore(PAGE_CONCURRENCY)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                                 cookies=COOKIES, timeout=25, follow_redirects=True) as client:
        pending = list(range(len(items)))
        for round_no in range(3):             # main article, then alternates 1 and 2
            if not pending:
                break
            cands = {}
            for idx in pending:
                ev = items[idx]
                pool = [ev["article"]] + ev.get("alternates", [])
                if round_no < len(pool):
                    cands[idx] = pool[round_no]
            ids = {idx: gn_article_id(a.get("google_link", "")) for idx, a in cands.items()}
            res = await resolve_ids(client, sem, [i for i in ids.values() if i], not args.no_dns_check)
            still = []
            for idx in pending:
                if idx not in cands:
                    still.append(idx)         # no more alternates to try
                    continue
                art, aid = cands[idx], ids[idx]
                r = res.get(aid) if aid else {"url": None, "method": "none", "reason": "no_article_id"}
                ev = items[idx]
                if r and r["url"]:
                    ev["resolved"] = {
                        "status": "ok", "url": r["url"], "domain": urlparse(r["url"]).hostname,
                        "method": r["method"], "used_alternate": round_no, "source_title": art["title"],
                        "source_item_id": art["item_id"],
                        "resolved_at": datetime.now(timezone.utc).isoformat()}
                else:
                    ev.setdefault("resolve_attempts", []).append(
                        {"alternate": round_no, "reason": (r or {}).get("reason", "unknown")})
                    still.append(idx)
            pending = still
            print(f"round {round_no}: resolved so far {sum('resolved' in e for e in items)}/{len(items)}")
        for idx in pending:
            items[idx]["resolved"] = {"status": "failed", "url": None}

        # extra real publisher URLs per event, so verify.py never has to fall back to a Google link
        for ev in items:
            ev["resolved_alternates"] = []
        if not args.no_alternates:
            cand_ids: dict[int, list] = {}
            for idx, ev in enumerate(items):
                if ev["resolved"]["status"] != "ok":
                    continue
                pool = [ev["article"]] + ev.get("alternates", [])
                used = ev["resolved"]["used_alternate"]
                extras = [a for j, a in enumerate(pool) if j != used][:MAX_ALT]
                cand_ids[idx] = [(a, gn_article_id(a.get("google_link", ""))) for a in extras]
            all_ids = sorted({i for lst in cand_ids.values() for _, i in lst if i})
            if all_ids:
                print(f"resolving {len(all_ids)} alternate articles ...")
                res = await resolve_ids(client, sem, all_ids, not args.no_dns_check)
                for idx, lst in cand_ids.items():
                    seen = {items[idx]["resolved"]["url"]}
                    for a, aid in lst:
                        r = res.get(aid) if aid else None
                        if r and r["url"] and r["url"] not in seen:
                            seen.add(r["url"])
                            items[idx]["resolved_alternates"].append(
                                {"url": r["url"], "source_title": a["title"], "source_item_id": a["item_id"]})

    ok = [e for e in items if e["resolved"]["status"] == "ok"]
    fails = [e for e in items if e["resolved"]["status"] != "ok"]
    url_counts = Counter(e["resolved"]["url"] for e in ok)
    dupes = {u: c for u, c in url_counts.items() if c > 1}
    out_dir = src.parent
    (out_dir / "resolved.json").write_text(json.dumps({
        "stage": "resolve", "generated_at": datetime.now(timezone.utc).isoformat(),
        "rpc_chunk": RPC_CHUNK, "n_ok": len(ok), "n_failed": len(fails), "items": items}, indent=2),
        encoding="utf-8")
    (out_dir / "resolved_failures.json").write_text(json.dumps(
        [{"company": e["company"], "event_id": e["event_id"], "attempts": e.get("resolve_attempts", [])}
         for e in fails], indent=2), encoding="utf-8")
    print(f"\nresolved {len(ok)}/{len(items)} (failed {len(fails)})")
    print("by method   :", dict(Counter(e["resolved"]["method"] for e in ok)))
    print("used alt    :", dict(Counter(e["resolved"]["used_alternate"] for e in ok)))
    print("by company  :", dict(Counter(e["company"] for e in ok)))
    print("fail reasons:", dict(Counter(a["reason"] for e in fails for a in e.get("resolve_attempts", [])[:1])))
    print("top domains :", Counter(e["resolved"]["domain"] for e in ok).most_common(8))
    print("events with extra publisher URLs:", sum(bool(e["resolved_alternates"]) for e in items))
    if dupes:
        print(f"WARNING: {len(dupes)} URLs were assigned to more than one event: {list(dupes)[:3]}")
    print(f"saved -> {out_dir / 'resolved.json'}")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Resolve Google News links to publisher URLs")
    ap.add_argument("--input", type=Path, default=None, help="path to selected_for_resolution.json")
    ap.add_argument("--base", type=Path, default=Path("output"))
    ap.add_argument("--no-dns-check", action="store_true")
    ap.add_argument("--no-alternates", action="store_true", help="skip resolving alternate articles")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(a))


if __name__ == "__main__":
    main()