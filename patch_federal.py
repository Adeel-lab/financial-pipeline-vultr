'''patch_federal.py - wires the Federal Register step into jobs.py, config.py and rca.py.

Run it AFTER patch_all.py, from the project root (the folder that contains backend/ and information_new/):
    python patch_federal.py
Each file is backed up as <name>.bak2. A file is written only if every anchor is found exactly once.
Running it twice is safe.
'''
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
    p.with_suffix(p.suffix + ".bak2").write_bytes(p.read_bytes())
    p.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def once(text, old, new, label):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{label}] expected exactly 1 match, found {n}. Run patch_all.py first, then this file. Nothing was written for this file.")
    return text.replace(old, new)


def patch_config():
    p, t, crlf = load("backend/config.py")
    if '"federal_register"' in t:
        return "config.py: already patched"
    t = once(t, '    "filing_explanations": INFO_DIR / "filing_explanations.py",\n',
             '    "filing_explanations": INFO_DIR / "filing_explanations.py",\n    "federal_register": INFO_DIR / "federal_register.py",\n', "config")
    save(p, t, crlf)
    return "config.py: patched (federal_register script entry)"


JOBS_METHOD = '''    def _federal_register(self, job_id: str) -> None:
        # Optional step: official Federal Register documents about the company and its industry. Never fails the job.
        p, jd = self.db.get_job(job_id)["params"], self.job_dir(job_id)
        out = jd / "regulatory"
        out.mkdir(parents=True, exist_ok=True)
        cmd = [config.PYTHON, str(config.SCRIPTS["federal_register"]), "--company", p["company"], "--outdir", str(out),
               "--cache-dir", str(config.JOBS_DIR / "_sec_cache")]
        for a in p.get("aliases") or []:
            cmd += ["--alias", str(a)]
        tickers = p.get("tickers") or []
        if tickers:
            cmd += ["--ticker", str(tickers[0])]
        for t in p.get("regulatory_topics") or []:
            cmd += ["--topic", str(t)]
        try:
            r = subprocess.run(cmd, cwd=config.INFO_DIR, env=build_env(), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=240)
            (jd / "logs").mkdir(parents=True, exist_ok=True)
            (jd / "logs" / "federal_register.log").write_text(r.stdout + r.stderr, encoding="utf-8")
            if r.returncode == 0:
                self._event(job_id, "info", "Federal Register documents ready.")
            else:
                self._event(job_id, "warning", "Federal Register step failed (the job continues): " + (r.stderr or r.stdout)[-300:])
        except Exception as e:
            self._event(job_id, "warning", f"Federal Register step skipped: {type(e).__name__}: {e}")

'''


def patch_jobs():
    p, t, crlf = load("backend/jobs.py")
    if "_federal_register" in t:
        return "jobs.py: already patched"
    t = once(t, '    ("filing_explanations", "filings/filing_explanations.json"),\n',
             '    ("filing_explanations", "filings/filing_explanations.json"),\n    ("federal_register", "regulatory/federal_register.json"),\n', "jobs: artifact")
    t = once(t, 'env=build_env({"FILINGS_JSON": str(jd / "filings" / "filing_explanations.json")}),',
             'env=build_env({"FILINGS_JSON": str(jd / "filings" / "filing_explanations.json"),\n'
             '                                          "FEDERAL_JSON": str(jd / "regulatory" / "federal_register.json")}),', "jobs: env")
    t = once(t, "        self._filing_explanations(job_id)\n",
             "        self._filing_explanations(job_id)\n        self._federal_register(job_id)\n", "jobs: call")
    t = once(t, "    def _run_peers(self, job_id: str, p: dict)", JOBS_METHOD + "    def _run_peers(self, job_id: str, p: dict)", "jobs: method")
    save(p, t, crlf)
    return "jobs.py: patched (Federal Register step before the SWOT build, artifact, FEDERAL_JSON for the Five Whys)"


RCA_LOAD = '''        print(f"  (filing explanations not loaded: {_e})")

try:
    from evidence_regulatory import RegulatoryIndex
    HAVE_REG = True
except ImportError:
    HAVE_REG = False

REGS = None
if HAVE_REG and os.getenv("FEDERAL_JSON") and Path(os.getenv("FEDERAL_JSON")).exists():
    try:
        REGS = RegulatoryIndex(os.getenv("FEDERAL_JSON"))
    except Exception as _e:
        print(f"  (Federal Register documents not loaded: {_e})")
'''

RCA_FUNC = '''def add_regulations(pool, regs, query, k, seen, counter):
    # Official Federal Register documents, added as R1, R2... A document shows what the government published or proposed.
    added = []
    if regs is None:
        return added
    for h in regs.search(query, k=k, exclude=seen):
        seen.add(h["id"])
        counter[0] += 1
        eid = f"R{counter[0]}"
        pseudo = {"id": h["id"], "kind": "regulatory", "content_hash": regs.sha256[:16],
                  "evidence": [{"url": h["url"], "source": "Federal Register", "published_at": h["published"], "quote": h["quote"]}]}
        extra = ""
        if h.get("effective_on"):
            extra += f", effective {h['effective_on']}"
        if h.get("comments_close_on"):
            extra += f", comments close {h['comments_close_on']}"
        text = (f"[{eid}] FEDERAL REGISTER {h['type'].upper()} ({', '.join(h['agencies'][:2]) or 'agency not stated'}, "
                f"published {h['published']}{extra}; an official document, not proof of any effect on the company) | "
                f"title: {h['title']} | quote: " + chr(34) + h["quote"] + chr(34))
        pool[eid] = (pseudo, text)
        added.append(eid)
    return added


'''


def patch_rca():
    p, t, crlf = load("information_new/rca.py")
    if "add_regulations" in t:
        return "rca.py: already patched"
    t = once(t, '        print(f"  (filing explanations not loaded: {_e})")\n', RCA_LOAD, "rca: loader")
    t = once(t, "# ---------- LLM ----------", RCA_FUNC + "# ---------- LLM ----------", "rca: function")
    t = once(t, "    cseen, ccounter = set(), [0]\n", "    cseen, ccounter = set(), [0]\n    rseen, rcounter = set(), [0]\n", "rca: counters")
    t = once(t, "        cadded = add_filings(pool, FILINGS, premise, 2, cseen, ccounter)\n",
             "        cadded = add_filings(pool, FILINGS, premise, 2, cseen, ccounter)\n"
             "        radded = add_regulations(pool, REGS, premise, 2, rseen, rcounter)\n", "rca: call")
    t = once(t, "+{len(cadded)} filing quotes)", "+{len(cadded)} filing quotes, +{len(radded)} regulatory documents)", "rca: print")
    t = once(t, "Items F* are facts computed by code",
             "Items R* are official U.S. Federal Register documents (rules, proposed rules, notices): they show what the government published or proposed, not that it affected the company. Items F* are facts computed by code", "rca: pool text")
    t = once(t, "They count as a separate source type.\n",
             "They count as a separate source type.\n"
             "13. R* items are government documents. Cite them as what the document says or proposes (the proposed rule would ...). A document existing is not evidence that it affected the company unless a cited news item or filing says so.\n", "rca: rule")
    t = once(t, '    if kind == "filing":\n',
             '    if kind == "regulatory":\n        evs = ev_list(it)\n        return [("regulatory", _domain((evs[0] if evs else {}).get("url")) or "federalregister.gov")]\n'
             '    if kind == "filing":\n', "rca: sources")
    save(p, t, crlf)
    return "rca.py: patched (R* Federal Register documents in the Five Whys pool)"


if __name__ == "__main__":
    for fn in (patch_config, patch_jobs, patch_rca):
        print(fn())
