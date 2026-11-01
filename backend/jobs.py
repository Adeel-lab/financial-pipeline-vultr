"""Job orchestration: runs the FinAnalyst pipeline stage by stage and tracks every step.

One job = one company.  Two branches start together and meet at the SWOT:

    financial_gate --report-only  ->  [human approves]  ->  financial_gate --decisions   -> approved_financials.json
    discovery -> triage -> resolve -> verify -> sentiment                                -> sentiment folder
                                   both finished  ->  swot_builder build  ->  [human review]  ->  swot_builder review
                                                                           ->  rca (on chosen items)

This module knows nothing about HTTP; api.py is a thin layer on top of it.
"""
import json
import logging
import re
import shutil
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from . import config
from .db import DB, now
from .env import build_env, llm_key_present
from .runner import CommandSpec, get_runner, tail_of

log = logging.getLogger("finanalyst")

NEWS_STAGES = ["discovery", "triage", "resolve", "verify", "sentiment"]
PIPELINE = ["finance_preview", "finance_approve", *NEWS_STAGES, "swot_build"]
ALL_STAGES = [*PIPELINE, "swot_review"]
QUADRANTS = ("strength", "weakness", "opportunity", "threat")
DONE = ("succeeded", "skipped")
JOB_ID_RE = re.compile(r"^[0-9a-f]{10}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

ARTIFACT_PATTERNS = [
    ("gate_report", "finance/*/gate_report.json"),
    ("approved_financials", "finance/*/approved_financials.json"),
    ("company_sentiment", "news/sentiment_*/company_sentiment.json"),
    ("article_scores", "news/sentiment_*/article_scores.json"),
    ("swot_draft", "swot/*/swot_draft.json"),
    ("swot_draft_md", "swot/*/swot_draft.md"),
    ("swot_final", "swot/*/swot_final.json"),
    ("swot_final_md", "swot/*/swot_final.md"),
    ("rca", "rca/*.json"),
    ("rca_md", "rca/*.md"),
    ("screenshot", "news/verify_*/screenshots/*.png"),
    ("source_article", "news/verify_*/articles/*.json"),
]


class JobError(Exception):
    """An error the caller should see (becomes an HTTP status)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class StageError(Exception):
    """A stage failed; the message is shown to the user."""


class StageCancelled(Exception):
    pass


def compute_status(job: dict, stages: dict) -> str:
    st = {n: s["status"] for n, s in stages.items()}
    if job["cancelled"]:
        return "cancelled"
    if any(st.get(n) == "failed" for n in PIPELINE):
        return "failed"
    if st.get("finance_approve") in ("blocked", "rejected"):
        return "blocked"
    if st.get("swot_review") == "succeeded":
        return "complete"
    if st.get("swot_build") == "succeeded":
        return "awaiting_swot_review"
    if st.get("finance_approve") == "waiting":
        return "awaiting_finance_approval"
    return "running"


class JobManager:
    def __init__(self, db: DB):
        self.db = db
        self.pool = ThreadPoolExecutor(max_workers=config.MAX_WORKERS, thread_name_prefix="job")
        self._news_sem = threading.Semaphore(config.NEWS_CONCURRENCY)
        self._lock = threading.RLock()
        self._inflight: set = set()
        self._cancel: dict = {}

    # ------------------------------------------------------------------ small helpers
    def job_dir(self, job_id: str) -> Path:
        return config.JOBS_DIR / job_id

    def _rel(self, job_id: str, path: Path) -> str:
        return Path(path).resolve().relative_to(self.job_dir(job_id).resolve()).as_posix()

    def _abs(self, job_id: str, rel: str) -> Path:
        return self.job_dir(job_id) / rel

    def _require_job(self, job_id: str) -> dict:
        if not JOB_ID_RE.match(job_id or ""):
            raise JobError(404, "job not found")
        job = self.db.get_job(job_id)
        if not job:
            raise JobError(404, "job not found")
        return job

    def _stage(self, job_id: str, name: str) -> dict:
        return self.db.get_stages(job_id)[name]

    def _set_stage(self, job_id: str, name: str, status: str, **fields) -> None:
        self.db.update_stage(job_id, name, status=status, **fields)
        self.db.touch_job(job_id)

    def _event(self, job_id: str, level: str, message: str) -> None:
        log.log({"error": logging.ERROR, "warning": logging.WARNING}.get(level, logging.INFO), "[%s] %s", job_id, message)
        self.db.add_event(job_id, level, message)

    def _cancel_event(self, job_id: str) -> threading.Event:
        with self._lock:
            return self._cancel.setdefault(job_id, threading.Event())

    def _busy(self, job_id: str) -> bool:
        with self._lock:
            return any(k[0] == job_id for k in self._inflight)

    @contextmanager
    def _exclusive(self, job_id: str, kind: str):
        key = (job_id, kind)
        with self._lock:
            if key in self._inflight:
                raise JobError(409, f"'{kind}' is already running for this job")
            self._inflight.add(key)
        try:
            yield
        finally:
            with self._lock:
                self._inflight.discard(key)

    def _submit(self, job_id: str, kind: str, fn) -> bool:
        key = (job_id, kind)
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)

        def task():
            try:
                fn(job_id)
            except StageCancelled:
                self._event(job_id, "warning", f"{kind} cancelled")
            except StageError as e:
                self._event(job_id, "error", f"{kind} failed: {e}")
            except Exception as e:  # a bug, not a pipeline failure: never leave a stage 'running'
                log.exception("unexpected error in %s for job %s", kind, job_id)
                self._event(job_id, "error", f"{kind}: unexpected error {type(e).__name__}: {e}")
                for name, s in self.db.get_stages(job_id).items():
                    if s["status"] == "running":
                        self._set_stage(job_id, name, "failed", finished_at=now(), error=f"unexpected error: {e}")
            finally:
                with self._lock:
                    self._inflight.discard(key)
                try:
                    self._advance(job_id)
                except Exception:
                    log.exception("advance failed for job %s", job_id)

        self.pool.submit(task)
        return True

    # ------------------------------------------------------------------ stage runner
    @staticmethod
    def _explain(result) -> str:
        lines = [l.rstrip() for l in result.log_tail.splitlines() if l.strip() and not l.startswith("=====")]
        text = "\n".join(lines[-12:])[-1500:]
        return f"exited with code {result.returncode}" + (f":\n{text}" if text else "")

    def _run_stage(self, job_id: str, stage: str, *, script_key: str, args: list, cwd: Path, validate=None) -> dict:
        jd = self.job_dir(job_id)
        cancel = self._cancel_event(job_id)
        if cancel.is_set():
            self._set_stage(job_id, stage, "cancelled", finished_at=now())
            raise StageCancelled()
        self._set_stage(job_id, stage, "running", started_at=now(), finished_at=None, error=None, meta={})
        self._event(job_id, "info", f"{stage} started")
        spec = CommandSpec(stage=stage, script=config.SCRIPTS[script_key], args=[str(a) for a in args], cwd=cwd,
                           env=build_env(), timeout=config.timeout_for(stage), job_dir=jd)
        try:
            res = get_runner(stage).run(spec, jd / "logs" / f"{stage}.log", cancel)
            if res.cancelled:
                self._set_stage(job_id, stage, "cancelled", finished_at=now(), error="cancelled by the user")
                raise StageCancelled()
            if res.timed_out:
                raise StageError(f"timed out after {spec.timeout} seconds")
            if res.returncode != 0:
                raise StageError(self._explain(res))
            meta = (validate() if validate else {}) or {}
        except StageCancelled:
            raise
        except StageError as e:
            self._set_stage(job_id, stage, "failed", finished_at=now(), error=str(e))
            raise
        except Exception as e:
            self._set_stage(job_id, stage, "failed", finished_at=now(), error=f"{type(e).__name__}: {e}")
            raise StageError(f"{type(e).__name__}: {e}") from e
        self._set_stage(job_id, stage, "succeeded", finished_at=now(), error=None, meta=meta)
        self._event(job_id, "info", f"{stage} finished")
        return meta

    @staticmethod
    def _read_json(path: Path):
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))

    # ------------------------------------------------------------------ create / advance
    def create_job(self, params: dict, files: dict) -> str:
        job_id = uuid.uuid4().hex[:10]
        jd = self.job_dir(job_id)
        for sub in ("inputs", "logs", "news", "finance", "swot", "rca"):
            (jd / sub).mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (jd / "inputs" / name).write_bytes(data)
        params = dict(params)
        params["has_financials"] = "balance.csv" in files and "income.csv" in files
        params["has_peers"] = "peers.json" in files
        self.db.create_job(job_id, params["company"], params.get("objective", ""), params)
        for n in ALL_STAGES:
            self.db.ensure_stage(job_id, n, "pending")
        if not params["has_financials"]:
            for n in ("finance_preview", "finance_approve"):
                self.db.update_stage(job_id, n, status="skipped")
            self._event(job_id, "warning", "No financial statements uploaded: the SWOT will rest on news only.")
        self._event(job_id, "info", f"Job created for {params['company']}")
        if not params.get("no_llm") and not llm_key_present():
            self._event(job_id, "warning", "VULTR_INFERENCE_API_KEY is not set: stages fall back to keyword scoring and the "
                                           "SWOT builder cannot classify news items.")
        self._submit(job_id, "news", self._run_news)
        if params["has_financials"]:
            self._submit(job_id, "finance_preview", self._run_finance_preview)
        return job_id

    def _advance(self, job_id: str) -> None:
        """Start the SWOT build as soon as both branches are ready."""
        job = self.db.get_job(job_id)
        if not job or job["cancelled"]:
            return
        stages = self.db.get_stages(job_id)
        if (stages["swot_build"]["status"] == "pending" and stages["sentiment"]["status"] == "succeeded"
                and stages["finance_approve"]["status"] in DONE):
            self._submit(job_id, "swot_build", self._run_swot_build)

    # ------------------------------------------------------------------ news branch
    def _run_news(self, job_id: str) -> None:
        with self._news_sem:
            for name in NEWS_STAGES:
                if self._stage(job_id, name)["status"] == "succeeded":
                    continue
                getattr(self, f"_s_{name}")(job_id)

    def _meta(self, job_id: str, stage: str) -> dict:
        return self._stage(job_id, stage)["meta"]

    def _s_discovery(self, job_id: str) -> None:
        p, jd = self.db.get_job(job_id)["params"], self.job_dir(job_id)
        news = jd / "news"
        if news.exists():
            shutil.rmtree(news)          # a new discovery makes every later result obsolete
        news.mkdir(parents=True)
        args = [f"--company={p['company']}", f"--engagement-id={job_id}", "--outdir", news, "--days", p["days"]]
        if p.get("locations"):
            args += ["--location", *p["locations"]]
        args += [f"--ticker={t}" for t in p.get("tickers", [])]
        args += [f"--alias={a}" for a in p.get("aliases", [])]
        args += [f"--exclude={x}" for x in p.get("excludes", [])]
        if p.get("strict_location"):
            args.append("--strict-location")
        if p.get("edition"):
            args.append(f"--edition={p['edition']}")

        def validate():
            runs = [d for d in news.iterdir() if d.is_dir() and any(d.glob("discovery_*.json"))]
            if not runs:
                raise StageError("discovery wrote no results")
            if len(runs) > 1:
                raise StageError("discovery wrote more than one result folder")
            rec = self._read_json(next(runs[0].glob("discovery_*.json")))
            n = len(rec.get("items", []))
            if n == 0:
                raise StageError("no news articles were found for this company. Try an alias, a wider location, or more days.")
            if n < 15:
                self._event(job_id, "warning", f"Only {n} unique articles were found: coverage is thin.")
            return {"run_dir": self._rel(job_id, runs[0]), "articles": n}

        self._run_stage(job_id, "discovery", script_key="discovery", args=args, cwd=config.INFO_DIR, validate=validate)

    def _s_triage(self, job_id: str) -> None:
        p, jd = self.db.get_job(job_id)["params"], self.job_dir(job_id)
        news, run_dir = jd / "news", self._abs(job_id, self._meta(job_id, "discovery")["run_dir"])
        args = ["--input", run_dir, "--base", news] + (["--no-llm"] if p.get("no_llm") else [])

        def validate():
            tdir = news / f"triage_{run_dir.name}"
            sel = tdir / "selected_for_resolution.json"
            if not sel.exists():
                raise StageError("triage wrote no selection file")
            n = len(self._read_json(sel).get("items", []))
            if n == 0:
                raise StageError("triage selected no events")
            return {"triage_dir": self._rel(job_id, tdir), "selected_events": n}

        self._run_stage(job_id, "triage", script_key="triage", args=args, cwd=config.INFO_DIR, validate=validate)

    def _s_resolve(self, job_id: str) -> None:
        jd = self.job_dir(job_id)
        news, tdir = jd / "news", self._abs(job_id, self._meta(job_id, "triage")["triage_dir"])
        args = ["--input", tdir / "selected_for_resolution.json", "--base", news]

        def validate():
            rf = tdir / "resolved.json"
            if not rf.exists():
                raise StageError("resolve wrote no result file")
            rec = self._read_json(rf)
            if not rec.get("n_ok"):
                raise StageError("none of the selected articles could be resolved to a publisher URL")
            return {"resolved": rec.get("n_ok"), "failed": rec.get("n_failed", 0)}

        self._run_stage(job_id, "resolve", script_key="resolve", args=args, cwd=config.INFO_DIR, validate=validate)

    def _s_verify(self, job_id: str) -> None:
        jd = self.job_dir(job_id)
        news, tdir = jd / "news", self._abs(job_id, self._meta(job_id, "triage")["triage_dir"])
        args = ["--input", tdir, "--base", news]

        def validate():
            vdir = news / f"verify_{tdir.name}"
            n = len(list((vdir / "articles").glob("*.json"))) if (vdir / "articles").is_dir() else 0
            if n == 0:
                raise StageError("no article page could be verified (all were blocked, paywalled or mismatched)")
            statuses = {}
            if (vdir / "verify_report.json").exists():
                statuses = self._read_json(vdir / "verify_report.json").get("statuses", {})
            return {"verify_dir": self._rel(job_id, vdir), "verified_articles": n, "statuses": statuses}

        self._run_stage(job_id, "verify", script_key="verify", args=args, cwd=config.INFO_DIR, validate=validate)

    def _s_sentiment(self, job_id: str) -> None:
        p, jd = self.db.get_job(job_id)["params"], self.job_dir(job_id)
        news, vdir = jd / "news", self._abs(job_id, self._meta(job_id, "verify")["verify_dir"])
        args = ["--input", vdir, "--base", news] + (["--no-llm"] if p.get("no_llm") else [])

        def validate():
            sdir = news / vdir.name.replace("verify_", "sentiment_", 1)
            if not (sdir / "company_sentiment.json").exists() or not (sdir / "article_scores.json").exists():
                raise StageError("sentiment wrote no result files")
            comp = self._read_json(sdir / "company_sentiment.json").get("companies", {})
            if not comp:
                raise StageError("sentiment produced no company result")
            q = next(iter(comp.values())).get("quality", {})
            if q and not q.get("llm_scored"):
                self._event(job_id, "warning", "No article was scored by the language model (keyword fallback): "
                                               "news items cannot be used as SWOT evidence.")
            return {"sentiment_dir": self._rel(job_id, sdir), "quality": q}

        self._run_stage(job_id, "sentiment", script_key="sentiment", args=args, cwd=config.INFO_DIR, validate=validate)

    # ------------------------------------------------------------------ finance branch
    def _gate_args(self, job_id: str) -> list:
        p, jd = self.db.get_job(job_id)["params"], self.job_dir(job_id)
        args = [f"--company={p['company']}", "--balance", jd / "inputs" / "balance.csv", "--income", jd / "inputs" / "income.csv",
                "--outdir", jd / "finance", "--units", p.get("units", "unknown"), "--frequency", p.get("frequency", "auto")]
        if p.get("currency"):
            args.append(f"--currency={p['currency']}")
        if p.get("entity"):
            args.append(f"--entity={p['entity']}")
        if p.get("no_llm"):
            args.append("--no-llm")
        return args

    @staticmethod
    def _find_output(folder: Path, before: set, filename: str) -> Path:
        found = [d for d in folder.iterdir() if d.is_dir() and (d / filename).exists()]
        fresh = [d for d in found if d.name not in before] or found
        if not fresh:
            raise StageError(f"the financial gate wrote no {filename}")
        return max(fresh, key=lambda d: d.stat().st_mtime_ns)

    def _run_finance_preview(self, job_id: str) -> None:
        jd = self.job_dir(job_id)
        fin = jd / "finance"
        before = {d.name for d in fin.iterdir()} if fin.exists() else set()

        def validate():
            d = self._find_output(fin, before, "gate_report.json")
            rep = self._read_json(d / "gate_report.json")
            counts = Counter(c.get("severity") for c in rep.get("checks", []))
            return {"gate_dir": self._rel(job_id, d), "proposal_status": rep.get("status"),
                    "counts": dict(counts), "periods": len(rep.get("periods", []))}

        meta = self._run_stage(job_id, "finance_preview", script_key="financial_gate",
                               args=self._gate_args(job_id) + ["--report-only"], cwd=config.PLAN_DIR, validate=validate)
        if meta["proposal_status"] == "blocked":
            self._set_stage(job_id, "finance_approve", "blocked",
                            error="The statements have blocking problems. Upload corrected files.")
            self._event(job_id, "warning", "The financial gate found blocking problems; corrected files are needed.")
        else:
            self._set_stage(job_id, "finance_approve", "waiting", error=None)
            self._event(job_id, "info", "Financial proposal is ready for review.")

    def get_finance_report(self, job_id: str) -> dict:
        self._require_job(job_id)
        meta = self._stage(job_id, "finance_preview")["meta"]
        if not meta.get("gate_dir"):
            raise JobError(404, "no financial report yet")
        return self._read_json(self._abs(job_id, meta["gate_dir"]) / "gate_report.json")

    def approve_finance(self, job_id: str, approved: bool, choices: dict) -> dict:
        self._require_job(job_id)
        st = self._stage(job_id, "finance_approve")
        if st["status"] not in ("waiting", "failed", "rejected"):
            raise JobError(409, f"the financial approval is not pending (status: {st['status']})")
        report = self.get_finance_report(job_id)
        if report.get("status") == "blocked":
            raise JobError(409, "the proposal is blocked: upload corrected statements")
        if not approved:
            self._set_stage(job_id, "finance_approve", "rejected", error="Not approved. Upload corrected statements or approve again.")
            self._event(job_id, "warning", "The financial proposal was rejected by the reviewer.")
            return {"status": "rejected"}
        problems = []
        for field, entry in report.get("fields", {}).items():
            if entry.get("needs_choice"):
                options = {o["label"] for o in (entry.get("options") or entry.get("candidates") or [])}
                if field not in choices:
                    problems.append(f"choose a row for '{field}'")
                elif choices[field] not in options:
                    problems.append(f"'{choices[field]}' is not one of the rows offered for '{field}'")
        if problems:
            raise JobError(422, "; ".join(problems))
        fin = self.job_dir(job_id) / "finance"
        body = json.dumps({"choices": choices, "approved": True}, indent=2)
        (fin / "decisions_latest.json").write_text(body, encoding="utf-8")
        (fin / f"decisions_{now().replace(':', '-')}.json").write_text(body, encoding="utf-8")
        if not self._submit(job_id, "finance_approve", self._run_finance_approve):
            raise JobError(409, "the approval is already being processed")
        return {"status": "approving"}

    def _run_finance_approve(self, job_id: str) -> None:
        jd = self.job_dir(job_id)
        fin = jd / "finance"
        before = {d.name for d in fin.iterdir()}

        def validate():
            d = self._find_output(fin, before, "approved_financials.json")
            data = self._read_json(d / "approved_financials.json")
            if not data.get("fields") or not data.get("periods"):
                raise StageError("approved_financials.json has no fields or periods")
            return {"approved_path": self._rel(job_id, d / "approved_financials.json"), "gate_dir": self._rel(job_id, d),
                    "periods": len(data["periods"]), "accepted_warnings": len(data.get("accepted_warnings", []))}

        self._run_stage(job_id, "finance_approve", script_key="financial_gate",
                        args=self._gate_args(job_id) + ["--decisions", fin / "decisions_latest.json"],
                        cwd=config.PLAN_DIR, validate=validate)
        self._event(job_id, "info", "Financial statements approved.")

    def replace_finance_files(self, job_id: str, files: dict) -> None:
        job = self._require_job(job_id)
        if self._busy(job_id):
            raise JobError(409, "the job is still running")
        jd = self.job_dir(job_id)
        for name in ("balance.csv", "income.csv"):
            (jd / "inputs" / name).write_bytes(files[name])
        params = dict(job["params"], has_financials=True)
        self.db.update_params(job_id, params)
        for n in ("finance_preview", "finance_approve", "swot_build", "swot_review"):
            self._set_stage(job_id, n, "pending", started_at=None, finished_at=None, error=None, meta={})
        self.db.set_cancelled(job_id, False)
        self._cancel_event(job_id).clear()
        self._event(job_id, "info", "Financial statements replaced; running the gate again.")
        self._submit(job_id, "finance_preview", self._run_finance_preview)

    # ------------------------------------------------------------------ SWOT
    def _run_swot_build(self, job_id: str) -> None:
        p, jd = self.db.get_job(job_id)["params"], self.job_dir(job_id)
        sdir = self._abs(job_id, self._meta(job_id, "sentiment")["sentiment_dir"])
        root = jd / "swot"
        before = {d.name for d in root.iterdir()}
        args = ["build", "--sentiment", sdir, "--outdir", root]
        if p.get("objective"):
            args.append(f"--objective={p['objective']}")
        fin = self._stage(job_id, "finance_approve")
        if fin["status"] == "succeeded":
            args += ["--financial", self._abs(job_id, fin["meta"]["approved_path"])]
        if p.get("has_peers"):
            args += ["--peers", jd / "inputs" / "peers.json"]
        if p.get("no_llm"):
            args.append("--no-llm")

        def validate():
            fresh = [d for d in root.iterdir() if d.is_dir() and (d / "swot_draft.json").exists()]
            new = [d for d in fresh if d.name not in before] or fresh
            if not new:
                raise StageError("swot_builder wrote no draft")
            d = max(new, key=lambda x: x.stat().st_mtime_ns)
            draft = self._read_json(d / "swot_draft.json")
            errors = sorted(set(draft.get("llm", {}).get("errors", [])))
            companies = {}
            for label, c in draft.get("companies", {}).items():
                items = c.get("items", [])
                companies[label] = {"items": len(items),
                                    "by_quadrant": dict(Counter((i["proposal"].get("quadrant") or "unclassified") for i in items)),
                                    "needs_classification": sum(1 for i in items if i.get("status") == "needs_classification")}
            if errors:
                self._event(job_id, "warning", "The model had problems during the SWOT build: " + "; ".join(errors[:3]))
            return {"draft": self._rel(job_id, d / "swot_draft.json"), "llm_enabled": draft.get("llm", {}).get("enabled"),
                    "llm_errors": errors, "companies": companies}

        self._run_stage(job_id, "swot_build", script_key="swot_builder", args=args, cwd=config.INFO_DIR, validate=validate)
        self._event(job_id, "info", "SWOT draft is ready for review.")

    def _draft(self, job_id: str):
        st = self._stage(job_id, "swot_build")
        if st["status"] != "succeeded":
            raise JobError(409, "the SWOT draft is not ready")
        path = self._abs(job_id, st["meta"]["draft"])
        return path, self._read_json(path)

    def get_swot_draft(self, job_id: str) -> dict:
        self._require_job(job_id)
        return self._draft(job_id)[1]

    def get_swot_final(self, job_id: str) -> dict:
        self._require_job(job_id)
        st = self._stage(job_id, "swot_review")
        if st["status"] != "succeeded":
            raise JobError(404, "the SWOT has not been reviewed yet")
        return self._read_json(self._abs(job_id, st["meta"]["final"]))

    def review_swot(self, job_id: str, decisions: list, reviewer: str) -> dict:
        self._require_job(job_id)
        draft_path, draft = self._draft(job_id)
        items = {(label, it["id"]): it for label, c in draft.get("companies", {}).items() for it in c.get("items", [])}
        errors, seen = [], set()
        for i, d in enumerate(decisions, 1):
            key = (d["company"], d["id"])
            if key not in items:
                errors.append(f"decision {i}: there is no item '{d['id']}' for '{d['company']}'")
                continue
            if key in seen:
                errors.append(f"decision {i}: '{d['id']}' appears twice")
            seen.add(key)
            it = items[key]
            if d["action"] == "approve" and it["proposal"].get("quadrant") not in QUADRANTS:
                errors.append(f"'{d['id']}' has no proposed quadrant: use action 'edit' and choose one")
            if d["action"] == "edit":
                q = d.get("quadrant") or it["proposal"].get("quadrant")
                if q not in QUADRANTS:
                    errors.append(f"'{d['id']}': choose a quadrant")
                elif it["kind"] == "financial" and q in ("opportunity", "threat"):
                    errors.append(f"'{d['id']}': financial items are internal (strength or weakness only)")
        if errors:
            raise JobError(422, "; ".join(errors[:10]))
        decisions_file = draft_path.parent / "decisions_in.json"
        decisions_file.write_text(json.dumps(decisions, indent=2, ensure_ascii=False), encoding="utf-8")

        def validate():
            final = draft_path.parent / "swot_final.json"
            if not final.exists():
                raise StageError("swot_builder review wrote no final file")
            data = self._read_json(final)
            summary = {}
            for label, c in data.get("companies", {}).items():
                summary[label] = {**{q: len(c["swot"][q]) for q in QUADRANTS},
                                  "pending": len(c["pending"]), "rejected": len(c["rejected"])}
            return {"final": self._rel(job_id, final), "summary": summary}

        with self._exclusive(job_id, "swot_review"):
            self._run_stage(job_id, "swot_review", script_key="swot_builder", cwd=config.INFO_DIR, validate=validate,
                            args=["review", "--draft", draft_path, "--decisions", decisions_file, f"--reviewer={reviewer}"])
        self._event(job_id, "info", "SWOT review saved.")
        return self._stage(job_id, "swot_review")["meta"]

    # ------------------------------------------------------------------ RCA
    def start_rca(self, job_id: str, items: list, quadrant: Optional[str], max_whys: int, notes: list) -> str:
        self._require_job(job_id)
        draft_path, draft = self._draft(job_id)
        companies = list(draft.get("companies", {}))
        if not companies:
            raise JobError(409, "the draft has no company")
        known = {it["id"] for it in draft["companies"][companies[0]].get("items", [])}
        unknown = [i for i in items if i not in known]
        if unknown:
            raise JobError(422, f"unknown item ids: {', '.join(unknown[:5])}")
        if not items and not quadrant:
            raise JobError(422, "give at least one item id or a quadrant")
        if len(items) > 10:
            raise JobError(422, "at most 10 items per RCA run")
        n = 1 + sum(1 for s in self.db.get_stages(job_id) if s.startswith("rca_"))
        stage = f"rca_{n}"
        self.db.ensure_stage(job_id, stage, "pending")
        rca_dir = self.job_dir(job_id) / "rca"
        args = ["run", "--draft", draft_path, f"--company={companies[0]}", "--max-whys", max_whys, "--out", rca_dir]
        args += [f"--item={i}" for i in items]
        if quadrant:
            args += ["--quadrant", quadrant]
        args += [f"--note={x}" for x in notes]

        def work(_job_id):
            before = {p.name for p in rca_dir.glob("rca_*.json")}

            def validate():
                fresh = [p for p in rca_dir.glob("rca_*.json") if p.name not in before]
                if not fresh:
                    raise StageError("the RCA wrote no result")
                outcomes = {}
                for p in fresh:
                    rec = self._read_json(p)
                    outcomes[rec.get("item_id", p.stem)] = rec.get("outcome")
                return {"files": sorted(self._rel(job_id, p) for p in fresh), "outcomes": outcomes,
                        "items": items, "quadrant": quadrant}

            self._run_stage(job_id, stage, script_key="rca", args=args, cwd=config.INFO_DIR, validate=validate)

        if not self._submit(job_id, stage, work):
            raise JobError(409, "this RCA run is already in progress")
        return stage

    def list_rca(self, job_id: str) -> list:
        self._require_job(job_id)
        out = []
        for p in sorted((self.job_dir(job_id) / "rca").glob("rca_*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                rec = self._read_json(p)
            except (OSError, ValueError):
                continue
            out.append({"name": p.stem, "item_id": rec.get("item_id"), "quadrant": rec.get("quadrant"), "outcome": rec.get("outcome"),
                        "problem": rec.get("problem"), "root_cause": rec.get("root_cause"), "generated_at": rec.get("generated_at")})
        return out

    def get_rca(self, job_id: str, name: str) -> dict:
        self._require_job(job_id)
        if not NAME_RE.match(name or ""):
            raise JobError(404, "not found")
        p = self.job_dir(job_id) / "rca" / f"{name}.json"
        if not p.is_file():
            raise JobError(404, "RCA result not found")
        return self._read_json(p)

    # ------------------------------------------------------------------ retry / cancel / delete
    def retry(self, job_id: str) -> dict:
        job = self._require_job(job_id)
        with self._lock:
            if self._busy(job_id):
                raise JobError(409, "the job is still running")
            stages = self.db.get_stages(job_id)
            bad = [n for n in PIPELINE if stages[n]["status"] in ("failed", "cancelled")]
            if not bad and not job["cancelled"]:
                raise JobError(409, "nothing to retry")
            reset, wait = set(), False
            for n in bad:
                if n in NEWS_STAGES:
                    reset.update(NEWS_STAGES[NEWS_STAGES.index(n):])
                    reset.update(("swot_build", "swot_review"))
                elif n == "finance_preview":
                    reset.update(("finance_preview", "finance_approve", "swot_build", "swot_review"))
                elif n == "finance_approve":
                    if "finance_approve" not in reset:
                        wait = True
                    reset.update(("swot_build", "swot_review"))
                elif n == "swot_build":
                    reset.update(("swot_build", "swot_review"))
            if wait and "finance_approve" not in reset:
                self._set_stage(job_id, "finance_approve", "waiting", finished_at=None, error=None)
            for n in reset:
                self._set_stage(job_id, n, "pending", started_at=None, finished_at=None, error=None, meta={})
            self.db.set_cancelled(job_id, False)
            self._cancel_event(job_id).clear()
        stages = self.db.get_stages(job_id)
        self._event(job_id, "info", "Retrying: " + ", ".join(n for n in PIPELINE if n in reset or (n == "finance_approve" and wait)))
        if any(stages[n]["status"] == "pending" for n in NEWS_STAGES):
            self._submit(job_id, "news", self._run_news)
        if job["params"].get("has_financials") and stages["finance_preview"]["status"] == "pending":
            self._submit(job_id, "finance_preview", self._run_finance_preview)
        self._advance(job_id)
        return {"status": "retrying"}

    def cancel(self, job_id: str) -> dict:
        job = self._require_job(job_id)
        stages = self.db.get_stages(job_id)
        if compute_status(job, stages) in ("complete", "cancelled"):
            raise JobError(409, "the job is already finished")
        self._cancel_event(job_id).set()
        self.db.set_cancelled(job_id, True)
        for name, s in stages.items():
            if s["status"] in ("pending", "waiting", "blocked"):
                self._set_stage(job_id, name, "cancelled", finished_at=now())
        self._event(job_id, "warning", "Job cancelled by the user.")
        return {"status": "cancelling"}

    def delete(self, job_id: str) -> None:
        self._require_job(job_id)
        if self._busy(job_id):
            raise JobError(409, "cancel the job and wait for it to stop before deleting it")
        self.db.delete_job(job_id)
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
        with self._lock:
            self._cancel.pop(job_id, None)

    # ------------------------------------------------------------------ read side
    def _view(self, job: dict, stages: dict, full: bool = True) -> dict:
        status = compute_status(job, stages)
        done = sum(1 for n in PIPELINE if stages[n]["status"] in DONE)
        order = [*ALL_STAGES, *sorted((n for n in stages if n.startswith("rca_")), key=lambda x: int(x.split("_")[1]))]
        actions = []
        s = {n: v["status"] for n, v in stages.items()}
        if s["finance_approve"] == "waiting" or s["finance_approve"] == "rejected":
            actions.append("approve_finance")
        if s["finance_approve"] in ("blocked", "rejected"):
            actions.append("replace_finance_files")
        if s["swot_build"] == "succeeded":
            actions += ["review_swot", "run_rca"]
        if any(s[n] in ("failed", "cancelled") for n in PIPELINE):
            actions.append("retry")
        if status in ("running", "awaiting_finance_approval") or any(v == "running" for v in s.values()):
            actions.append("cancel")
        p = job["params"]
        view = {"id": job["id"], "company": job["company"], "objective": job["objective"], "status": status,
                "progress": round(done / len(PIPELINE), 2), "created_at": job["created_at"], "updated_at": job["updated_at"],
                "actions": actions}
        if full:
            view["params"] = {k: v for k, v in p.items()}
            view["stages"] = [{"name": n, **{k: stages[n][k] for k in ("status", "started_at", "finished_at", "error", "meta")}}
                              for n in order if n in stages]
        return view

    def get_job(self, job_id: str) -> dict:
        job = self._require_job(job_id)
        return self._view(job, self.db.get_stages(job_id))

    def list_jobs(self, limit: int = 50, offset: int = 0) -> list:
        return [self._view(j, self.db.get_stages(j["id"]), full=False) for j in self.db.list_jobs(limit, offset)]

    def get_events(self, job_id: str, after: int, limit: int) -> list:
        self._require_job(job_id)
        return self.db.get_events(job_id, after, limit)

    def get_log(self, job_id: str, stage: str, nbytes: int) -> str:
        self._require_job(job_id)
        if not NAME_RE.match(stage or ""):
            raise JobError(404, "log not found")
        path = self.job_dir(job_id) / "logs" / f"{stage}.log"
        if not path.is_file():
            raise JobError(404, "log not found")
        return tail_of(path, nbytes)

    def get_news(self, job_id: str) -> dict:
        self._require_job(job_id)
        st = self._stage(job_id, "sentiment")
        if st["status"] != "succeeded":
            raise JobError(404, "no news results yet")
        sdir = self._abs(job_id, st["meta"]["sentiment_dir"])
        return {"company_sentiment": self._read_json(sdir / "company_sentiment.json"),
                "articles": self._read_json(sdir / "article_scores.json").get("items", [])}

    def list_artifacts(self, job_id: str) -> list:
        self._require_job(job_id)
        jd, out = self.job_dir(job_id), []
        for kind, pattern in ARTIFACT_PATTERNS:
            for p in sorted(jd.glob(pattern))[:300]:
                out.append({"kind": kind, "path": p.relative_to(jd).as_posix(), "size": p.stat().st_size})
        return out

    def resolve_file(self, job_id: str, rel: str) -> Path:
        self._require_job(job_id)
        jd = self.job_dir(job_id).resolve()
        p = (jd / rel).resolve()
        if not p.is_relative_to(jd) or not p.is_file():
            raise JobError(404, "file not found")
        return p

    def health(self) -> dict:
        import shutil as _sh
        import sys
        return {"python": sys.version.split()[0], "scripts": {k: v.exists() for k, v in config.SCRIPTS.items()},
                "jobs_dir": str(config.JOBS_DIR), "llm_key_configured": llm_key_present(),
                "llm_base": __import__("os").environ.get("VULTR_INFERENCE_BASE", "(script default)"),
                "sandbox": config.SANDBOX, "sandboxed_stages": sorted(config.SANDBOX_STAGES) if config.SANDBOX == "docker" else [],
                "docker_found": bool(_sh.which("docker")) if config.SANDBOX == "docker" else None}

    # ------------------------------------------------------------------ lifecycle
    def recover(self) -> None:
        """After a restart: fail stages that were mid-run, then restart anything that was only queued."""
        n = self.db.recover_interrupted()
        if n:
            log.warning("marked %d interrupted stage(s) as failed", n)
        for job in self.db.list_jobs(limit=500):
            if job["cancelled"]:
                continue
            stages = self.db.get_stages(job["id"])
            if any(stages[x]["status"] == "failed" for x in PIPELINE):
                continue
            if any(stages[x]["status"] == "pending" for x in NEWS_STAGES):
                self._submit(job["id"], "news", self._run_news)
            if job["params"].get("has_financials") and stages["finance_preview"]["status"] == "pending":
                self._submit(job["id"], "finance_preview", self._run_finance_preview)
            self._advance(job["id"])

    def shutdown(self) -> None:
        with self._lock:
            for ev in self._cancel.values():
                ev.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
