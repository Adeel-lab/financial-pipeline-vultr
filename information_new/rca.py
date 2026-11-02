#!/usr/bin/env python
"""rca.py - evidence-bound 5 Whys on a SWOT item.  (v1.2 + evidence strength + financial facts)

  python rca.py list --draft output\\swot_X\\swot_draft.json
  python rca.py run  --draft output\\swot_X\\swot_draft.json --item fin_operating_margin
  python rca.py run  --draft ... --quadrant weakness --max-whys 5 --note "GM statements include GM Financial"
  python rca.py run  --draft ... --item story:abc --verify-dir output\\verify_triage_44d23bebd27e

v1.2: quotes found in another pool item are re-attributed instead of rejected; repeated answers stop the
chain; passage links are checked against the draft by content hash; fix evidence is searched before the
summary; corrective actions need a verified quote to count as supported.
"""
import argparse, hashlib, json, os, re, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
llm_client = None


HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent / "planning"):
    if (_p / "llm_client.py").exists():
        sys.path.insert(0, str(_p))
        break

try:
    from evidence_articles import ArticleIndex, grade_support, similar
    HAVE_ARTICLES = True
except ImportError:
    HAVE_ARTICLES = False


try:
    from evidence_financials import FactIndex
    HAVE_FACTS = True
except ImportError:
    HAVE_FACTS = False

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

PROMPT_VERSION = "rca-v1.2"
DELAY = float(os.getenv("SWOT_DELAY", "0"))
NON_PCT = {"quick_ratio", "current_ratio", "cash_ratio"}
SYSTEM = "You are a careful root-cause analyst. You return only valid JSON."
REPEAT_AT = 0.6          # a new answer this similar to the last one counts as a repeat
FIX_FACT_CUES = "what if would have been lever sensitivity cost of sales share of revenue operating income"



# ---------- loading ----------
def load(path):
    raw = Path(path).read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()



def _walk_items(node):
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node) and all("id" in x and "kind" in x for x in node):
            yield node
        else:
            for x in node:
                yield from _walk_items(x)
    elif isinstance(node, dict):
        for v in node.values():
            yield from _walk_items(v)



def company_blocks(draft):
    blocks = []
    comps = draft.get("companies")
    pairs = []
    if isinstance(comps, dict):
        pairs = list(comps.items())
    elif isinstance(comps, list):
        pairs = [((c.get("profile") or {}).get("name") or c.get("company") or c.get("name") or "company", c)
                 for c in comps if isinstance(c, dict)]
    for name, c in pairs:
        if isinstance(c, dict) and isinstance(c.get("items"), list):
            blocks.append((name, c["items"]))
    if not blocks:
        for i, lst in enumerate(_walk_items(draft)):
            blocks.append((draft.get("company") or f"company{i}", lst))
    if not blocks:
        sys.exit("Could not find SWOT items in this file.")
    return blocks



def company_aliases(draft, company):
    """Names that identify the company in article text (name, aliases, tickers from the draft profile)."""
    out = {str(company)}
    comps = draft.get("companies")
    seq = list(comps.values()) if isinstance(comps, dict) else (comps or [])
    for c in seq:
        if not isinstance(c, dict):
            continue
        prof = c.get("profile") or {}
        name = prof.get("name") or c.get("company") or c.get("name") or ""
        if name and name.lower() == str(company).lower():
            for key in ("aliases", "all_aliases", "tickers"):
                out.update(x for x in (prof.get(key) or c.get(key) or []) if isinstance(x, str))
    return sorted(a for a in out if a)



def find_verify_dir(draft, draft_path, given=None):
    """Locate verify_triage_<id> (the folder with articles/*.json) for this draft."""
    if given:
        return Path(given) if (Path(given) / "articles").is_dir() else None
    src = draft.get("sentiment_source")
    f = (src.get("file") if isinstance(src, dict) else src) or ""
    m = re.search(r"(triage_[0-9a-f]+)", str(f))
    if not m:
        return None
    name = f"verify_{m.group(1)}"
    dp = Path(draft_path).resolve()
    bases = [Path(str(f).replace("\\", "/")).parent.parent, Path.cwd() / "output", HERE / "output",
             dp.parent.parent, dp.parent.parent.parent]
    for b in bases:
        if (b / name / "articles").is_dir():
            return b / name
    return None



def prop(it):
    return it.get("proposal") or {}



def statement(it):
    p = prop(it)
    return p.get("statement") or it.get("statement") or it.get("title") or ""



def status(it):
    return (prop(it).get("status") or it.get("status") or "").lower()



def quadrant(it):
    p = prop(it)
    return p.get("quadrant") or it.get("quadrant") or ("not_swot" if p.get("not_swot") else "")



# ---------- evidence rendering ----------
def fmt_val(metric, v):
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{v:.2f}x" if metric in NON_PCT else f"{v * 100:.1f}%"



def hist_pairs(h):
    if isinstance(h, dict):
        return list(h.items())
    out = []
    for r in h or []:
        if isinstance(r, (list, tuple)) and len(r) >= 2:
            out.append((r[0], r[1]))
        elif isinstance(r, dict):
            out.append((r.get("period") or r.get("date") or r.get("as_of"), r.get("value")))
    return out



def ev_list(it):
    ev = it.get("evidence")
    if isinstance(ev, dict):
        return [ev]
    return [e for e in (ev or []) if isinstance(e, dict)]



def render(eid, it, label=""):
    metric = it.get("metric") or ""
    if it.get("kind") == "financial":
        h = "; ".join(f"{d} {fmt_val(metric, v)}" for d, v in hist_pairs(it.get("history")))
        rule = it.get("statement_rule") or statement(it)
        src = next((e.get("file") for e in ev_list(it) if e.get("file")), "approved_financials.json")
        return (f"[{eid}]{label} FINANCIAL {metric or it.get('title','')} (consolidated annual statements, "
                f"file {src}): {rule}. History: {h}")
    parts = [f"[{eid}]{label} NEWS | {it.get('title','')} | summary: {it.get('summary') or statement(it)}"]
    for e in ev_list(it)[:3]:
        q = (e.get("quote") or "")[:400]
        parts.append(f"source {e.get('source','?')} {str(e.get('published_at',''))[:10]} quote: \"{q}\"")
    return " | ".join(parts)



def build_pool(items, target):
    pool = {}
    pool["E0"] = (target, render("E0", target, " (PROBLEM EVIDENCE)"))
    for it in items:
        if it is target or status(it) == "rejected":
            continue
        eid = f"E{len(pool)}"
        pool[eid] = (it, render(eid, it))
    return pool



def hash_links(items):
    """content_hash -> {url, source, published_at} from the draft's own evidence (the links a reviewer already sees)."""
    out = {}
    for it in items:
        for e in ev_list(it):
            h = e.get("content_hash")
            if h and e.get("url"):
                out.setdefault(h, {"url": e["url"], "source": e.get("source"), "published_at": e.get("published_at")})
    return out



def support_text(pool):
    """The text a quote is checked against: real source wording only (not LLM summaries)."""
    out = {}
    for eid, (it, text) in pool.items():
        if it.get("kind") == "passage":
            out[eid] = it["evidence"][0]["quote"]
        elif it.get("kind") == "financial":
            out[eid] = text
        else:
            out[eid] = " ".join((e.get("quote") or "")[:400] for e in ev_list(it)[:3])
    return out



def add_passages(pool, index, query, aliases, company, k, seen, counter, links):
    """Search the full verified article texts and add the best new passages to the pool as P1, P2..."""
    added = []
    for h in index.search(query, k=k, company=company, aliases=aliases):
        key = (h["event_id"], h["start"])
        if key in seen:
            continue
        seen.add(key)
        counter[0] += 1
        eid = f"P{counter[0]}"
        known = links.get(h["content_hash"])
        url = known["url"] if known else h["url"]
        source = (known or {}).get("source") or h["source"]
        confirmed = bool(known) or bool(h.get("url_confirmed"))
        pseudo = {"id": f"passage:{h['event_id']}:{h['start']}", "kind": "passage",
                  "event_id": h["event_id"], "content_hash": h["content_hash"], "span": [h["start"], h["end"]],
                  "link_confirmed": confirmed,
                  "evidence": [{"url": url, "source": source, "published_at": h["published_at"], "quote": h["text"]}]}
        text = (f"[{eid}] ARTICLE PASSAGE | {source or '?'} {str(h['published_at'])[:10]} | "
                f"{h['title']} | quote: \"{h['text']}\"")
        pool[eid] = (pseudo, text)
        added.append(eid)
    return added



def add_facts(pool, facts, query, k, seen, counter):
    """Computed financial facts (from the approved statements) that match the question, added as F1, F2..."""
    added = []
    if facts is None:
        return added
    for h in facts.search(query, k=k, exclude={(i, 0) for i in seen}):
        if h["id"] in seen:
            continue
        seen.add(h["id"])
        counter[0] += 1
        eid = f"F{counter[0]}"
        pseudo = {"id": h["id"], "kind": "financial", "fact": True, "computed": True, "data_caution": h.get("caution"),
                  "evidence": [{"type": "financial", "file": str(facts.path), "file_sha256": facts.sha256, "quote": h["text"]}]}
        pool[eid] = (pseudo, f"[{eid}] FINANCIAL FACT (computed from the approved statements: numbers only, not causes) | {h['text']}")
        added.append(eid)
    return added



def add_filings(pool, filings, query, k, seen, counter):
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


def add_regulations(pool, regs, query, k, seen, counter):
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


# ---------- LLM ----------
def call_llm(prompt, tries=5):


    llm_json = llm_client.llm_json
    for a in range(tries):
        try:
            out = llm_json(prompt, system=SYSTEM)
            if DELAY:
                time.sleep(DELAY)
            return out
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate" in msg.lower():
                wait = 15 * (a + 1)
                print(f"  (rate limit, waiting {wait}s)")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("LLM rate-limited too many times")



def step_prompt(problem, chain, pool, notes, n, max_whys):
    pool_text = "\n".join(t for _, t in pool.values())
    hist = "\n".join(f"Why {s['step']}: {s['question']}\n  Answer: {s['answer']} "
                     f"[{', '.join(s['evidence_ids'])}]" for s in chain) or "(none yet)"
    premise = chain[-1]["answer"] if chain else problem
    note_txt = "\n".join(f"- {x}" for x in notes) or "(none)"
    return f"""PROBLEM STATEMENT: {problem}


ANALYST NOTES (context from the human reviewer):
{note_txt}


EVIDENCE POOL (the only facts you may use). Items E* are SWOT draft items. Items P* are word-for-word passages from the full text of verified articles. Items C* are quotes from the company's own SEC filings: management's explanations, which are claims by management and not proven facts. Items R* are official U.S. Federal Register documents (rules, proposed rules, notices): they show what the government published or proposed, not that it affected the company. Items F* are facts computed by code from the approved financial statements: numbers only. They show THAT a number changed, never WHY:
{pool_text}


CHAIN SO FAR:
{hist}


TASK: This is Why #{n} of at most {max_whys}. Ask: "Why is this true: {premise}?"
Give the single most direct cause that the evidence pool supports.


RULES:
1. Use only facts in the pool. No outside knowledge. Never invent numbers.
2. Cite every claim with evidence_ids from the pool, e.g. ["E3", "P2"].
3. If the pool does not explain the cause, set basis to "unknown", evidence_ids to [], is_root_cause to false, and answer to "Not determinable from the supplied evidence: <what is missing>".
4. basis "reported" = a cited item states this cause AND you can copy, word for word, a sentence from one cited item that itself states the cause-and-effect link; put that sentence in "support". If you cannot, use basis "inference" (word the answer with "likely" or "consistent with") and leave support empty.
5. Two numbers moving together is not a cause unless a source says so.
6. Set is_root_cause true only if the answer is a decision, policy, structure or external event that an actor could change or must plan around, and a further "why" would need facts outside the pool.
7. Each answer must go one level deeper than the previous one. If you would only restate the previous answer, set is_root_cause to true and say so in why_stop.
8. "support" quotes must be copied exactly from the cited item (max 300 characters) with that item's id. A quote that states only the effect, not the cause, does not count.
9. "competing_explanations": list any other explanation the pool points to (id plus one line). If a competing explanation is better supported than your answer, use it as your answer instead.
10. The item marked (PROBLEM EVIDENCE) restates the problem. Do not use it as the cause.
11. F* items never prove a cause. Join a number to a cause only with a news item or passage that states the cause, and word the link "consistent with". If an F* item carries a data caution, repeat the caution.
12. C* items show what management says. Cite them as management's explanation (management attributes this to ...), never as established fact. They count as a separate source type.
13. R* items are government documents. Cite them as what the document says or proposes (the proposed rule would ...). A document existing is not evidence that it affected the company unless a cited news item or filing says so.


Return JSON: {{"answer": str, "evidence_ids": [str], "basis": "reported|inference|unknown", "support": [{{"id": str, "quote": str}}], "competing_explanations": [str], "is_root_cause": bool, "why_stop": str}}"""



def final_prompt(problem, chain, pool, notes):
    steps = "\n".join(f"Why {s['step']}: {s['answer']} [{', '.join(s['evidence_ids'])}] ({s['basis']})"
                      for s in chain if s["status"] == "supported")
    pool_text = "\n".join(t for _, t in pool.values())
    return f"""PROBLEM: {problem}
ANALYST NOTES: {'; '.join(notes) or '(none)'}
SUPPORTED CHAIN:
{steps}
EVIDENCE POOL:
{pool_text}


Summarise the analysis. Use only the chain and pool. Corrective actions are recommendations, not facts.
Every corrective action must include "support": [{{"id": str, "quote": str}}] copying, word for word, a sentence from a pool item that shows the action addresses the cause, that the company or a peer has already done it, or how large the lever is. If the pool has no such sentence, leave support empty and start the rationale with "Hypothesis (no supporting evidence in pool):". Do not cite an item that does not say this.
Return JSON: {{"root_cause": str, "root_cause_type": "management_controllable|external|mixed|unknown",
"validation_test": str (if this root cause were fixed, would the problem go away? answer honestly, including doubts),
"corrective_actions": [{{"action": str, "rationale": str, "evidence_ids": [str], "support": [{{"id": str, "quote": str}}]}}],
"evidence_gaps": [str], "caveats": [str]}}"""



# ---------- validation ----------
NUM = re.compile(r"\d[\d,]*\.?\d*")



def unverified_numbers(answer, ids, pool):
    blob = " ".join(pool[i][1] for i in ids if i in pool).replace(",", "")
    bad = []
    for tok in NUM.findall(answer):
        t = tok.replace(",", "").rstrip(".")
        if not t or (t.isdigit() and int(t) < 10):
            continue
        if t not in blob:
            bad.append(t)
    return bad



def _eid_key(x):
    return (x[0] != "E", int(x[1:]) if x[1:].isdigit() else 0)



def _clean_support(raw):
    out = []
    for s in raw if isinstance(raw, list) else []:
        if isinstance(s, dict):
            out.append({"id": str(s.get("id", "")).strip().upper(), "quote": str(s.get("quote", ""))})
    return out




# ---------- evidence strength (a label, never a gate) ----------
STRENGTH_ORDER = ["None", "Weak", "Limited", "Good", "Strong"]



def _domain(url):
    try:
        return urlparse(url or "").netloc.lower().removeprefix("www.")
    except Exception:
        return ""



def sources_of(it):
    """(type, key) pairs for the independent sources behind one pool item. Same site = one source."""
    kind = it.get("kind")
    if kind == "financial":
        return [("financial", "financial statements")]
    if kind == "regulatory":
        evs = ev_list(it)
        return [("regulatory", _domain((evs[0] if evs else {}).get("url")) or "federalregister.gov")]
    if kind == "filing":
        evs = ev_list(it)
        return [("filing", _domain((evs[0] if evs else {}).get("url")) or "filing")]
    evs = ev_list(it) if kind == "passage" else ev_list(it)[:3]
    out = []
    for e in evs:
        key = _domain(e.get("url")) or str(e.get("source") or "").strip().lower() or str(it.get("id"))
        out.append(("news", key))
    return out or [("news", str(it.get("id")))]



def rate_evidence(ids, pool, verified):
    """Label how well a claim is backed. Counts independent sources (different sites; financial statements and
    filings count as their own source types) and verified quotes. The problem's own item (E0) is not counted."""
    srcs = {}
    for i in ids:
        if i == "E0" or i not in pool:
            continue
        for typ, key in sources_of(pool[i][0]):
            srcs.setdefault((typ, key), i)
    n, types, q = len(srcs), sorted({t for t, _ in srcs}), len(verified or [])
    if n == 0:
        label = "None"
    elif q >= 1 and (n >= 3 or (n >= 2 and len(types) >= 2)):
        label = "Strong"
    elif n >= 2:
        label = "Good"
    elif q >= 1:
        label = "Limited"
    else:
        label = "Weak"
    score = 0 if n == 0 else {1: 30, 2: 55}.get(n, 70) + 10 * min(2, len(types) - 1) + (15 if q else 0)
    return {"label": label, "score": min(100, score), "sources": n, "types": types, "verified_quotes": q,
            "sites": sorted({k for (t, k) in srcs if t == "news"}),
            "items": sorted(set(srcs.values()), key=_eid_key)}



def strength_summary(chain, pool):
    steps = []
    for s in chain:
        if s["status"] != "supported":
            continue
        steps.append({"step": s["step"], **rate_evidence(s["evidence_ids"], pool, s.get("support_verified"))})
    if not steps:
        return {"version": 1, "overall": {"label": "None", "score": 0}, "steps": []}
    weakest = min(steps, key=lambda x: STRENGTH_ORDER.index(x["label"]))
    return {"version": 1,
            "overall": {"label": weakest["label"], "score": round(sum(x["score"] for x in steps) / len(steps)),
                        "weakest_step": weakest["step"], "root_cause_step": steps[-1]["step"]},
            "steps": steps,
            "how": "Strong: 3+ independent sources, or 2 sources of different types, plus a verified quote. "
                   "Good: 2+ independent sources. Limited: 1 source with a verified quote. Weak: 1 source, no verified quote. "
                   "Same site counts once. This is a label, not a rule: nothing is blocked because of it."}



# ---------- core ----------
def run_rca(item, items, company, objective, notes, max_whys, draft_sha, model,
            index=None, aliases=None, per_step=4, verify_dir=None, facts=None):
    pool = build_pool(items, item)
    links = hash_links(items)
    problem = statement(item)
    seen, counter = set(), [0]
    fseen, fcounter = set(), [0]
    cseen, ccounter = set(), [0]
    rseen, rcounter = set(), [0]
    chain, outcome = [], "max_depth_reached"
    for n in range(1, max_whys + 1):
        premise = chain[-1]["answer"] if chain else problem
        added = add_passages(pool, index, premise, aliases, company, per_step, seen, counter, links) if index is not None else []
        fadded = add_facts(pool, facts, premise, 3, fseen, fcounter)
        cadded = add_filings(pool, FILINGS, premise, 2, cseen, ccounter)
        radded = add_regulations(pool, REGS, premise, 2, rseen, rcounter)
        print(f"  Why {n} ... (+{len(added)} article passages, +{len(fadded)} financial facts, +{len(cadded)} filing quotes, +{len(radded)} regulatory documents)")
        r = call_llm(step_prompt(problem, chain, pool, notes, n, max_whys))
        ans = (r.get("answer") or "").strip()
        if chain and ans and HAVE_ARTICLES and similar(ans, chain[-1]["answer"]) >= REPEAT_AT:
            print("  (this answer repeats the previous step - stopping here)")
            outcome = "root_cause_reached"
            break
        ids = [str(i).strip().upper() for i in (r.get("evidence_ids") or [])]
        valid = [i for i in ids if i in pool]
        dropped = [i for i in ids if i not in pool]
        basis = r.get("basis") if r.get("basis") in ("reported", "inference") else "unknown"

        sup = _clean_support(r.get("support"))
        graded = grade_support(sup, support_text(pool)) if HAVE_ARTICLES else \
            {"basis": basis, "verified": sup, "rejected": []}
        for q in graded["verified"]:
            if q["id"] not in valid:
                valid.append(q["id"])
        downgraded = False
        if basis == "reported" and graded["basis"] != "reported":
            basis, downgraded = "inference", True

        ok = bool(valid) and basis != "unknown" and bool(ans)
        step = {"step": n, "question": f"Why is this true: {premise}?", "answer": ans,
                "evidence_ids": valid, "basis": basis, "status": "supported" if ok else "unsupported",
                "support_verified": graded["verified"], "support_rejected": graded["rejected"],
                "downgraded_from_reported": downgraded,
                "competing_explanations": [str(c) for c in (r.get("competing_explanations") or [])][:4],
                "unverified_numbers": unverified_numbers(ans, valid, pool) if ok else [],
                "dropped_invalid_ids": dropped, "model_said_root": bool(r.get("is_root_cause")),
                "why_stop": r.get("why_stop", "")}
        chain.append(step)
        if not ok:
            outcome = "stopped_insufficient_evidence"
            break
        if step["model_said_root"] and n >= 2:
            outcome = "root_cause_reached"
            break
    supported = [s for s in chain if s["status"] == "supported"]
    summary = {}
    if supported:
        if index is not None:
            q = f"{supported[-1]['answer']} company response plan strategy actions announced"
            added = add_passages(pool, index, q, aliases, company, 5, seen, counter, links)
            print(f"  Fix evidence: +{len(added)} article passages")
        fadded = add_facts(pool, facts, f"{supported[-1]['answer']} {FIX_FACT_CUES}", 3, fseen, fcounter)
        if fadded:
            print(f"  Fix evidence: +{len(fadded)} financial facts")
        print("  Summarising ...")
        summary = call_llm(final_prompt(problem, chain, pool, notes))
    action_ids = set()
    st = support_text(pool)
    for act in (summary.get("corrective_actions") or []) if isinstance(summary, dict) else []:
        if not isinstance(act, dict):
            continue
        ids_ = [str(i).strip().upper() for i in (act.get("evidence_ids") or [])]
        g = grade_support(_clean_support(act.get("support")), st) if HAVE_ARTICLES else \
            {"verified": [], "rejected": []}
        keep = [i for i in ids_ if i in pool] + [q["id"] for q in g["verified"]]
        act["evidence_ids"] = list(dict.fromkeys(keep))
        act["support_verified"], act["support_rejected"] = g["verified"], g["rejected"]
        act["evidence_status"] = ("supported" if g["verified"] else
                                  "cited_unverified" if act["evidence_ids"] else "hypothesis")
        act["strength"] = rate_evidence(act["evidence_ids"], pool, g["verified"])
        action_ids.update(act["evidence_ids"])
    used = sorted({i for s in chain for i in s["evidence_ids"]} | action_ids | {"E0"}, key=_eid_key)
    evidence_used = {}
    for i in used:
        it = pool[i][0]
        first = (ev_list(it) or [{}])[0]
        evidence_used[i] = {"item_id": it.get("id"), "text": pool[i][1],
                            "source": first.get("source"), "published_at": first.get("published_at"),
                            "content_hash": it.get("content_hash"), "span": it.get("span"),
                            "link_confirmed": it.get("link_confirmed"),
                            "links": [e.get("url") for e in ev_list(it) if e.get("url")]}
    urls = {u for e in evidence_used.values() for u in e["links"]}
    domains = {urlparse(u).netloc.lower().removeprefix("www.") for u in urls if u}
    return {
        "stage": "rca", "prompt_version": PROMPT_VERSION, "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "draft_sha256": draft_sha, "objective": objective, "company": company,
        "item_id": item.get("id"), "item_status": status(item) or "unknown",
        "quadrant": quadrant(item), "problem": problem, "analyst_notes": notes,
        "outcome": outcome,
        "chain": chain,
        "root_cause": summary.get("root_cause") or (supported[-1]["answer"] if supported else ""),
        "summary": summary,
        "has_inference": any(s["basis"] == "inference" for s in supported),
        "retrieval": {"enabled": index is not None, "verify_dir": str(verify_dir) if verify_dir else None,
                      "passages_per_step": per_step if index is not None else 0,
                      "passages_retrieved": counter[0]},
        "evidence_depth": {"items_cited": len(used),
                           "passages_cited": sum(1 for i in used if i.startswith("P")),
                           "facts_cited": sum(1 for i in used if i.startswith("F")),
                           "distinct_links_cited": len(urls), "distinct_domains_cited": len(domains),
                           "quotes_verified": sum(len(s["support_verified"]) for s in chain),
                           "quotes_rejected": sum(len(s["support_rejected"]) for s in chain)},
        "evidence_used": evidence_used,
        "evidence_strength": strength_summary(chain, pool),
        "warnings": ["Analysis rests on the SWOT draft items plus passages retrieved from verified article texts; it is not a complete causal study.",
                     "Inference steps are the model's reasoning over cited facts, not source claims.",
                     "A verified quote proves the words are in the saved article text, not that the source proves the cause. Read the quotes and open the links.",
                     "Passages marked 'link not confirmed' may come from a different page than the link shown.",
                     "Financial facts (F items) are numbers calculated from the approved statements. They show that something changed, not why, and statements only say they were approved, not that they are correct.",
                     "Corrective actions are recommendations, not evidence. Only actions marked supported have a verified quote."],
    }



def _strength_line(r):
    es = r.get("evidence_strength") or {}
    o = es.get("overall") or {}
    if not o:
        return "- Evidence strength: n/a"
    return (f"- Evidence strength: **{o.get('label')}** ({o.get('score')}/100, weakest step {o.get('weakest_step', '-')}); "
            f"Strong = 3+ independent sources or 2 different types with a verified quote, Good = 2+ sources, "
            f"Limited = 1 source with a quote, Weak = 1 source, no quote")



def to_md(r):
    esc = lambda s: str(s).replace("|", "/").replace("\n", " ")
    d = r["evidence_depth"]
    L = [f"# 5 Whys: {r['company']}", "", f"**Problem:** {r['problem']}", "",
         f"- Item: `{r['item_id']}` ({r['quadrant']}, review status: {r['item_status']})",
         f"- Outcome: **{r['outcome']}**", f"- Model: {r['model']} | prompt {r['prompt_version']}",
         f"- Generated: {r['generated_at']}",
         f"- Evidence depth: {d['items_cited']} items cited, {d['passages_cited']} article passages, "
         f"{d['distinct_domains_cited']} distinct sites, {d['quotes_verified']} quotes verified, "
         f"{d['quotes_rejected']} rejected, {d.get('facts_cited', 0)} financial facts",
         _strength_line(r), "",
         "| # | Question | Answer | Evidence | Basis | Check |", "|---|---|---|---|---|---|"]
    for s in r["chain"]:
        chk = s["status"] + (f"; unverified numbers: {', '.join(s['unverified_numbers'])}"
                             if s["unverified_numbers"] else "")
        if s.get("downgraded_from_reported"):
            chk += "; claimed 'reported' but no verbatim supporting quote, downgraded to inference"
        st_ = next((x for x in (r.get("evidence_strength") or {}).get("steps", []) if x["step"] == s["step"]), None)
        if st_:
            chk += f"; strength: {st_['label']} ({st_['sources']} source{'s' if st_['sources'] != 1 else ''}, {', '.join(st_['types']) or 'none'})"
        L.append(f"| {s['step']} | {esc(s['question'])} | {esc(s['answer'])} | "
                 f"{', '.join(s['evidence_ids'])} | {s['basis']} | {esc(chk)} |")
    L += ["", "## Supporting quotes (checked word for word)", ""]
    any_q = False
    for s in r["chain"]:
        for q in s.get("support_verified", []):
            e = r["evidence_used"].get(q["id"], {})
            link = f" {e['links'][0]}" if e.get("links") else ""
            warn = " (link not confirmed)" if e.get("link_confirmed") is False else ""
            moved = f" [model cited {q['model_cited']}, found in {q['id']}]" if q.get("model_cited") and q["model_cited"] != q["id"] else ""
            L.append(f"- Why {s['step']} [{q['id']}] {e.get('source') or ''} {str(e.get('published_at') or '')[:10]}: "
                     f"\"{q['quote']}\"{link}{warn}{moved}")
            any_q = True
        for q in s.get("support_rejected", []):
            L.append(f"- Why {s['step']} [{q['id']}] REJECTED ({q.get('reason','')}): \"{q['quote']}\"")
            any_q = True
    if not any_q:
        L.append("- none")
    comp = [(s["step"], c) for s in r["chain"] for c in s.get("competing_explanations", [])]
    if comp:
        L += ["", "## Competing explanations considered", ""] + [f"- Why {n}: {c}" for n, c in comp]
    sm = r["summary"]
    L += ["", "## Root cause", "", r["root_cause"] or "_not determined_", ""]
    if sm:
        L += [f"**Type:** {sm.get('root_cause_type','unknown')}", "",
              f"**Validation test:** {sm.get('validation_test','')}", "", "## Corrective actions (recommendations)", ""]
        tag = {"supported": "", "cited_unverified": " (cited, but no verified quote)",
               "hypothesis": " (hypothesis: no cited support)"}
        for a in sm.get("corrective_actions", []):
            L.append(f"- {a.get('action')} - {a.get('rationale')} [{', '.join(a.get('evidence_ids') or [])}]"
                     + tag.get(a.get("evidence_status"), "")
                     + (f" [strength: {a['strength']['label']}, {a['strength']['sources']} source(s)]" if a.get("strength") else ""))
            for q in a.get("support_verified", []):
                L.append(f"  - evidence [{q['id']}]: \"{q['quote']}\"")
        if sm.get("evidence_gaps"):
            L += ["", "## Evidence gaps", ""] + [f"- {g}" for g in sm["evidence_gaps"]]
        if sm.get("caveats"):
            L += ["", "## Caveats", ""] + [f"- {c}" for c in sm["caveats"]]
    L += ["", "## Evidence used", ""]
    for i, e in r["evidence_used"].items():
        L.append(f"- {e['text']}" + (f" ({'; '.join(e['links'])})" if e["links"] else "")
                 + (" (link not confirmed)" if e.get("link_confirmed") is False else ""))
    L += ["", "## Warnings", ""] + [f"- {w}" for w in r["warnings"]]
    return "\n".join(L) + "\n"



# ---------- CLI ----------
def pick(items, key):
    hits = [i for i in items if i.get("id") == key] or [i for i in items if str(i.get("id", "")).startswith(key)]
    if len(hits) != 1:
        sys.exit(f"Item '{key}' matched {len(hits)} items. Use `list` to see ids.")
    return hits[0]



def main():
    global llm_client
    ap = argparse.ArgumentParser(description="Evidence-bound 5 Whys on a SWOT item")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "run"):
        p = sub.add_parser(name)
        p.add_argument("--draft", required=True)
        p.add_argument("--company")
    r = sub.choices["run"]
    r.add_argument("--item", action="append", default=[])
    r.add_argument("--quadrant", choices=["weakness", "threat", "strength", "opportunity"])
    r.add_argument("--max-whys", type=int, default=5)
    r.add_argument("--note", action="append", default=[])
    r.add_argument("--out")
    r.add_argument("--verify-dir", help="verify_triage_<id> folder (auto-detected from the draft if omitted)")
    r.add_argument("--passages-per-step", type=int, default=4)
    r.add_argument("--no-passages", action="store_true", help="run without article passages")
    r.add_argument("--no-facts", action="store_true", help="run without computed financial facts")
    a = ap.parse_args()


    draft, sha = load(a.draft)
    blocks = company_blocks(draft)
    if a.company:
        blocks = [b for b in blocks if a.company.lower() in str(b[0]).lower()] or sys.exit("Company not found.")
    company, items = blocks[0]


    if a.cmd == "list":
        for it in items:
            print(f"{it.get('id'):<28} {it.get('kind','?'):<9} {quadrant(it) or '-':<12} "
                  f"{status(it) or '-':<9} {statement(it)[:80]}")
        return


    targets = [pick(items, k) for k in a.item]
    if a.quadrant:
        targets += [i for i in items if quadrant(i) == a.quadrant and i not in targets]
    if not targets:
        sys.exit("Give --item ID and/or --quadrant.")


    index, vdir = None, None
    aliases = company_aliases(draft, company)
    if not a.no_passages:
        if not HAVE_ARTICLES:
            print("NOTE: evidence_articles.py not found next to rca.py - running without article passages.")
        else:
            vdir = find_verify_dir(draft, a.draft, a.verify_dir)
            if vdir is None:
                print("NOTE: verify folder not found (use --verify-dir) - running without article passages.")
            else:
                index = ArticleIndex.from_verify_dir(vdir)
                print(f"Article passages: {len(index.passages)} from {vdir.name} | aliases: {aliases}")


    facts = None
    if not a.no_facts and HAVE_FACTS:
        facts = FactIndex.from_items(items, a.draft)
        print(f"Financial facts: {len(facts.facts)} from {facts.path.name}" if facts else "Financial facts: none (no approved statements found)")


    import importlib.util
    _llm_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "planning", "llm_client.py"
    )
    _spec = importlib.util.spec_from_file_location("llm_client", _llm_path)
    llm_client = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(llm_client)


    out_dir = Path(a.out) if a.out else Path(a.draft).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for it in targets:
        if status(it) == "proposed":
            print(f"NOTE: {it.get('id')} is still 'proposed' (not human-approved).")
        print(f"RCA: {it.get('id')}")
        res = run_rca(it, items, company, draft.get("objective", ""), a.note, a.max_whys, sha, llm_client.MODEL,
                      index=index, aliases=aliases, per_step=a.passages_per_step, verify_dir=vdir, facts=facts)
        safe_id = str(it.get('id')).replace(":", "_")
        base = out_dir / f"rca_{safe_id}"
        base.with_suffix(".json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
        base.with_suffix(".md").write_text(to_md(res), encoding="utf-8")
        print(f"  outcome: {res['outcome']}\n  Saved -> {base}.json / .md")



if __name__ == "__main__":
    main()