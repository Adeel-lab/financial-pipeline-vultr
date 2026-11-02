'''patch_all.py - applies the Groq-pacing fix and the filing-explanations wiring.

Run from the project root (the folder that contains backend/ and information_new/):
    python patch_all.py
Each file is backed up as <name>.bak. A file is only written if every anchor is found exactly once.
Running it twice is safe (already patched files are skipped).
'''
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")


def load(rel):
    p = ROOT / rel
    if not p.exists():
        sys.exit(f"missing file: {p}")
    raw = p.read_bytes().decode("utf-8")
    return p, raw.replace("\r\n", "\n"), "\r\n" in raw


def save(p, text, crlf):
    p.with_suffix(p.suffix + ".bak").write_bytes(p.read_bytes())
    p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{label}] expected exactly 1 match, found {n}. Nothing was written for this file.")
    return text.replace(old, new)


# ------------------------------------------------------------------ sentiment.py
SENT_CONST = '''
SENTIMENT_REASONING = os.environ.get("SENTIMENT_REASONING", "").strip()     # e.g. low (gpt-oss on Groq)
MAX_WAIT = float(os.environ.get("SENTIMENT_MAX_WAIT", "90"))                # longer waits = daily limit, stop
_last_call = 0.0
'''

SENT_CHAT = '''def parse_reset(s):
    tot = 0.0
    for num, unit in re.findall(r"([\\d.]+)(ms|h|m|s)", s or ""):
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
                print(f"!! Provider says wait {wait:.0f}s (daily limit?). LLM scoring stopped; keyword fallback.", flush=True)
                return None
            wait = max(wait, 2 ** attempt * 2) + random.random()
            print(f"   HTTP {r.status_code}, retry {attempt + 1}/6 in {wait:.0f}s", flush=True)
            await asyncio.sleep(wait)
            continue
        LLMState.dead = True
        log.error("Chat API fatal HTTP %s %s", r.status_code, r.text[:300])
        print(f"!! Chat API error HTTP {r.status_code} - falling back to keyword scores")
        return None
    return None
'''


def patch_sentiment():
    p, t, crlf = load("information_new/sentiment.py")
    if "SENTIMENT_REASONING" in t:
        return "sentiment.py: already patched"
    t = once(t, "import sys\n", "import sys\nimport time\n", "sentiment: import time")
    m = re.search(r"^MAX_OUT_TOKENS = .*$", t, re.M)
    if not m:
        sys.exit("[sentiment: constants] MAX_OUT_TOKENS line not found")
    t = t[:m.end()] + "\n" + SENT_CONST.rstrip("\n") + t[m.end():]
    a, b = t.find("async def chat("), t.find("def extract_json_object")
    if a == -1 or b == -1 or b < a:
        sys.exit("[sentiment: chat] chat() or extract_json_object() not found")
    t = t[:a] + SENT_CHAT + "\n\n" + t[b:]
    save(p, t, crlf)
    return "sentiment.py: patched (pacing, retry-after, daily-limit stop, reasoning_effort)"


# ------------------------------------------------------------------ backend/config.py
def patch_config():
    p, t, crlf = load("backend/config.py")
    if '"filing_explanations"' in t:
        return "config.py: already patched"
    t = once(t, '    "sentiment": INFO_DIR / "sentiment.py",\n',
             '    "sentiment": INFO_DIR / "sentiment.py",\n    "filing_explanations": INFO_DIR / "filing_explanations.py",\n',
             "config: scripts")
    t = once(t, '_int("FINANALYST_NEWS_CONCURRENCY", 2)', '_int("FINANALYST_NEWS_CONCURRENCY", 1)', "config: concurrency")
    save(p, t, crlf)
    return "config.py: patched (script entry, news concurrency 2 -> 1)"


# ------------------------------------------------------------------ backend/jobs.py
JOBS_METHOD = '''    def _filing_explanations(self, job_id: str) -> None:
        # Optional step: management explanations quoted from the latest SEC 10-K / 10-Q. Never fails the job.
        p, jd = self.db.get_job(job_id)["params"], self.job_dir(job_id)
        tickers = p.get("tickers") or []
        if not tickers:
            self._event(job_id, "info", "No ticker given: filing explanations skipped.")
            return
        out = jd / "filings"
        out.mkdir(parents=True, exist_ok=True)
        try:
            r = subprocess.run([config.PYTHON, str(config.SCRIPTS["filing_explanations"]), "--company", p["company"],
                                "--ticker", str(tickers[0]), "--outdir", str(out)],
                               cwd=config.INFO_DIR, env=build_env(), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=300)
            (jd / "logs").mkdir(parents=True, exist_ok=True)
            (jd / "logs" / "filing_explanations.log").write_text(r.stdout + r.stderr, encoding="utf-8")
            if r.returncode == 0:
                self._event(job_id, "info", "Filing explanations ready.")
            else:
                self._event(job_id, "warning", "Filing explanations failed (the job continues): " + (r.stderr or r.stdout)[-300:])
        except Exception as e:
            self._event(job_id, "warning", f"Filing explanations skipped: {type(e).__name__}: {e}")

'''


def patch_jobs():
    p, t, crlf = load("backend/jobs.py")
    if "_filing_explanations" in t:
        return "jobs.py: already patched"
    t = once(t, "import shutil\n", "import shutil\nimport subprocess\n", "jobs: import")
    t = once(t, '    ("rca", "rca/*.json"),\n',
             '    ("filing_explanations", "filings/filing_explanations.json"),\n    ("rca", "rca/*.json"),\n', "jobs: artifact")
    t = once(t, "env=build_env(), timeout=config.timeout_for(stage), job_dir=jd)",
             'env=build_env({"FILINGS_JSON": str(jd / "filings" / "filing_explanations.json")}),\n'
             "                           timeout=config.timeout_for(stage), job_dir=jd)", "jobs: stage env")
    t = once(t, '        root = jd / "swot"\n        before = {d.name for d in root.iterdir()}\n        args = ["build"',
             '        self._filing_explanations(job_id)\n'
             '        root = jd / "swot"\n        before = {d.name for d in root.iterdir()}\n        args = ["build"', "jobs: call")
    t = once(t, "    def _run_peers(self, job_id: str, p: dict)", JOBS_METHOD + "    def _run_peers(self, job_id: str, p: dict)", "jobs: method")
    save(p, t, crlf)
    return "jobs.py: patched (filing step before the SWOT build, artifact, FILINGS_JSON for the Five Whys)"


# ------------------------------------------------------------------ information_new/rca.py
RCA_IMPORT = '''    HAVE_FACTS = False

try:
    from evidence_filings import FilingIndex
    HAVE_FILINGS = True
except ImportError:
    HAVE_FILINGS = False

FILINGS = None
if HAVE_FILINGS and os.getenv("FILINGS_JSON") and Path(os.getenv("FILINGS_JSON")).exists():
    try:
        FILINGS = FilingIndex(os.getenv("FILINGS_JSON"))
    except Exception as _e:
        print(f"  (filing explanations not loaded: {_e})")
'''

RCA_FUNC = '''def add_filings(pool, filings, query, k, seen, counter):
    # Management explanations quoted from the company's own SEC filings, added as C1, C2...
    added = []
    if filings is None:
        return added
    for h in filings.search(query, k=k, exclude=seen):
        seen.add(h["id"])
        counter[0] += 1
        eid = f"C{counter[0]}"
        pseudo = {"id": h["id"], "kind": "filing", "content_hash": filings.sha256[:16],
                  "evidence": [{"url": h["url"], "source": f"SEC {h['form']}", "published_at": h["filing_date"],
                                "quote": h["quote"]}]}
        text = (f"[{eid}] MANAGEMENT EXPLANATION (company's own SEC {h['form']}, period {h['period']}; "
                f"a claim by management, not a proven fact) | topic: {h['topic']} | quote: " + chr(34) + h["quote"] + chr(34))
        pool[eid] = (pseudo, text)
        added.append(eid)
    return added


'''


def patch_rca():
    p, t, crlf = load("information_new/rca.py")
    if "add_filings" in t:
        return "rca.py: already patched"
    t = once(t, "    HAVE_FACTS = False\n", RCA_IMPORT, "rca: import")
    t = once(t, "# ---------- LLM ----------", RCA_FUNC + "# ---------- LLM ----------", "rca: function")
    t = once(t, "    fseen, fcounter = set(), [0]\n", "    fseen, fcounter = set(), [0]\n    cseen, ccounter = set(), [0]\n", "rca: counters")
    t = once(t, "        fadded = add_facts(pool, facts, premise, 3, fseen, fcounter)\n",
             "        fadded = add_facts(pool, facts, premise, 3, fseen, fcounter)\n"
             "        cadded = add_filings(pool, FILINGS, premise, 2, cseen, ccounter)\n", "rca: call")
    t = once(t, "+{len(fadded)} financial facts)", "+{len(fadded)} financial facts, +{len(cadded)} filing quotes)", "rca: print")
    t = once(t, "Items F* are facts computed by code",
             "Items C* are quotes from the company's own SEC filings: management's explanations, which are claims by management and not proven facts. Items F* are facts computed by code", "rca: pool text")
    t = once(t, "If an F* item carries a data caution, repeat the caution.\n",
             "If an F* item carries a data caution, repeat the caution.\n"
             "12. C* items show what management says. Cite them as management's explanation (management attributes this to ...), never as established fact. They count as a separate source type.\n",
             "rca: rule")
    save(p, t, crlf)
    return "rca.py: patched (C* filing quotes in the Five Whys pool)"


if __name__ == "__main__":
    for fn in (patch_sentiment, patch_config, patch_jobs, patch_rca):
        print(fn())
