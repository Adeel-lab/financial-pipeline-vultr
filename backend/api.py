"""HTTP API: a thin layer over JobManager. It validates input and turns JobError into HTTP status codes."""
import json
import logging
import mimetypes
import re
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import config
from .db import DB
from .env import load_dotenv_files
from .jobs import JobError, JobManager

log = logging.getLogger("finanalyst")

Quadrant = Literal["strength", "weakness", "opportunity", "threat"]
Units = Literal["unknown", "units", "thousands", "millions", "billions"]
Frequency = Literal["auto", "annual", "quarterly"]
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
ITEM_ID = re.compile(r"^[A-Za-z0-9_:.\-]{1,80}$")
TICKER = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")
EXTRA_TYPES = {".md": "text/markdown; charset=utf-8", ".json": "application/json", ".png": "image/png", ".html": "text/html"}


# ---------------------------------------------------------------- request bodies
class FinanceApproval(BaseModel):
    approved: bool = True
    choices: dict[str, str] = Field(default_factory=dict, description="field name -> the exact row label chosen")


class ReviewDecision(BaseModel):
    company: str
    id: str
    action: Literal["approve", "edit", "reject"]
    quadrant: Optional[Quadrant] = None
    statement: Optional[str] = Field(None, max_length=1000)
    note: str = Field("", max_length=1000)


class SwotReview(BaseModel):
    decisions: list[ReviewDecision] = Field(min_length=1)
    reviewer: str = Field("consultant", max_length=80)


class RcaRequest(BaseModel):
    items: list[str] = Field(default_factory=list, max_length=10)
    quadrant: Optional[Quadrant] = None
    max_whys: int = Field(5, ge=1, le=8)
    notes: list[str] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------- input cleaning
def clean(name: str, value: Optional[str], max_len: int = 200, required: bool = False) -> str:
    v = CONTROL.sub(" ", value or "").strip()
    if required and not v:
        raise JobError(422, f"{name} is required")
    if len(v) > max_len:
        raise JobError(422, f"{name} is too long (at most {max_len} characters)")
    return v


def split_items(name: str, value: str, pattern: str = r"[;\n]") -> list:
    out = []
    for part in re.split(pattern, value or ""):
        part = clean(name, part, 120)
        if not part:
            continue
        if part.startswith("-"):
            raise JobError(422, f"{name}: a value cannot start with '-'")
        out.append(part)
    return out[:10]


def clean_tickers(value: str) -> list:
    """Tickers of listed companies to compare with (any company). Their numbers are read from SEC filings."""
    out = []
    for t in split_items("compare with", value, r"[,;\s]+"):
        if not TICKER.match(t):
            raise JobError(422, f"compare with: '{t[:20]}' does not look like a ticker")
        out.append(t.upper())
    return out[:10]


async def read_upload(kind: str, up: UploadFile) -> bytes:
    data = await up.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise JobError(413, f"{kind}: the file is larger than {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not data.strip():
        raise JobError(422, f"{kind}: the file is empty")
    if b"\x00" in data:
        raise JobError(422, f"{kind}: this is not a text file")
    if kind in ("balance", "income"):
        if not (up.filename or "").lower().endswith(".csv"):
            raise JobError(422, f"{kind}: upload a .csv file")
        if data.count(b"\n") < 1:
            raise JobError(422, f"{kind}: the CSV needs a header row and at least one data row")
    if kind == "peers":
        try:
            obj = json.loads(data.decode("utf-8-sig"))
        except ValueError:
            raise JobError(422, "peers: not valid JSON")
        if not isinstance(obj, dict):
            raise JobError(422, "peers: expected a JSON object")
    return data


def _has_file(up) -> bool:
    return bool(up is not None and getattr(up, "filename", ""))


# ---------------------------------------------------------------- app
def create_app() -> FastAPI:
    load_dotenv_files()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        manager = JobManager(DB(config.DB_PATH))
        manager.recover()
        app.state.manager = manager
        log.info("FinAnalyst backend ready. Jobs folder: %s", config.JOBS_DIR)
        yield
        manager.shutdown()

    app = FastAPI(title="FinAnalyst backend", version="1.0.0", lifespan=lifespan,
                  description="Runs the FinAnalyst pipeline as jobs: financial gate + news pipeline -> SWOT -> review -> RCA.")
    app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(JobError)
    async def job_error_handler(request: Request, exc: JobError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message})

    def mgr(request: Request) -> JobManager:
        return request.app.state.manager

    # ---- service
    @app.get("/", tags=["service"])
    def root():
        return {"service": "FinAnalyst backend", "docs": "/docs", "health": "/health"}

    @app.get("/health", tags=["service"])
    def health(request: Request):
        return mgr(request).health()

    # ---- jobs
    @app.post("/jobs", status_code=201, tags=["jobs"])
    async def create_job(
        request: Request,
        company: str = Form(...),
        objective: str = Form(""),
        location: str = Form("", description="places, separated by ';' (empty = worldwide)"),
        ticker: str = Form("", description="stock tickers, separated by ',' or ';'"),
        peers_tickers: str = Form("", description="tickers of listed companies to compare with, separated by ',' or ';'"),
        alias: str = Form("", description="other names used in headlines, separated by ';'"),
        exclude: str = Form("", description="phrases that mean 'wrong company', separated by ';'"),
        days: int = Form(30, ge=1, le=90),
        strict_location: bool = Form(False),
        edition: str = Form("", description='Google News edition, e.g. "hl=hu&gl=HU&ceid=HU:hu"'),
        currency: str = Form(""),
        units: Units = Form("unknown"),
        frequency: Frequency = Form("auto"),
        entity: str = Form("", description='e.g. "consolidated group"'),
        no_llm: bool = Form(False),
        balance: Optional[UploadFile] = File(None),
        income: Optional[UploadFile] = File(None),
        peers: Optional[UploadFile] = File(None),
    ):
        if _has_file(balance) != _has_file(income):
            raise JobError(422, "upload both the balance sheet and the income statement, or neither")
        params = {
            "company": clean("company", company, 120, required=True),
            "objective": clean("objective", objective, 500),
            "locations": split_items("location", location),
            "tickers": split_items("ticker", ticker, r"[,;\s]+"),
            "peer_tickers": clean_tickers(peers_tickers),
            "aliases": split_items("alias", alias),
            "excludes": split_items("exclude", exclude),
            "days": days, "strict_location": strict_location, "edition": clean("edition", edition, 200),
            "currency": clean("currency", currency, 10), "units": units, "frequency": frequency,
            "entity": clean("entity", entity, 120), "no_llm": no_llm,
        }
        files = {}
        for kind, up, fname in (("balance", balance, "balance.csv"), ("income", income, "income.csv"), ("peers", peers, "peers.json")):
            if _has_file(up):
                files[fname] = await read_upload(kind, up)
        manager = mgr(request)
        return manager.get_job(manager.create_job(params, files))

    @app.get("/jobs", tags=["jobs"])
    def list_jobs(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return mgr(request).list_jobs(limit, offset)

    @app.get("/jobs/{job_id}", tags=["jobs"])
    def get_job(job_id: str, request: Request):
        return mgr(request).get_job(job_id)

    @app.post("/jobs/{job_id}/retry", tags=["jobs"])
    def retry(job_id: str, request: Request):
        return mgr(request).retry(job_id)

    @app.post("/jobs/{job_id}/cancel", tags=["jobs"])
    def cancel(job_id: str, request: Request):
        return mgr(request).cancel(job_id)

    @app.delete("/jobs/{job_id}", tags=["jobs"])
    def delete(job_id: str, request: Request):
        mgr(request).delete(job_id)
        return {"deleted": job_id}

    @app.get("/jobs/{job_id}/events", tags=["progress"])
    def events(job_id: str, request: Request, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        return mgr(request).get_events(job_id, after, limit)

    @app.get("/jobs/{job_id}/logs/{stage}", response_class=PlainTextResponse, tags=["progress"])
    def stage_log(job_id: str, stage: str, request: Request, tail_kb: int = Query(64, ge=1, le=1024)):
        return PlainTextResponse(mgr(request).get_log(job_id, stage, tail_kb * 1024))

    # ---- financial gate
    @app.get("/jobs/{job_id}/finance/report", tags=["finance"])
    def finance_report(job_id: str, request: Request):
        return mgr(request).get_finance_report(job_id)

    @app.post("/jobs/{job_id}/finance/approve", status_code=202, tags=["finance"])
    def finance_approve(job_id: str, body: FinanceApproval, request: Request):
        choices = {clean("field", k, 80): clean("row", v, 300) for k, v in body.choices.items()}
        return mgr(request).approve_finance(job_id, body.approved, choices)

    @app.post("/jobs/{job_id}/finance/files", status_code=202, tags=["finance"])
    async def finance_files(job_id: str, request: Request, balance: UploadFile = File(...), income: UploadFile = File(...)):
        files = {"balance.csv": await read_upload("balance", balance), "income.csv": await read_upload("income", income)}
        mgr(request).replace_finance_files(job_id, files)
        return {"status": "rerunning"}

    # ---- news
    @app.get("/jobs/{job_id}/news", tags=["news"])
    def news(job_id: str, request: Request):
        return mgr(request).get_news(job_id)

    # ---- SWOT
    @app.get("/jobs/{job_id}/swot/draft", tags=["swot"])
    def swot_draft(job_id: str, request: Request):
        return mgr(request).get_swot_draft(job_id)

    @app.post("/jobs/{job_id}/swot/review", tags=["swot"])
    def swot_review(job_id: str, body: SwotReview, request: Request):
        decisions = []
        for d in body.decisions:
            item = d.model_dump(exclude_none=True)
            if not ITEM_ID.match(item["id"]):
                raise JobError(422, f"invalid item id: {item['id'][:40]}")
            item["company"] = clean("company", item["company"], 200, required=True)
            for k in ("statement", "note"):
                if k in item:
                    item[k] = CONTROL.sub(" ", item[k]).strip()
            decisions.append(item)
        return mgr(request).review_swot(job_id, decisions, clean("reviewer", body.reviewer, 80) or "consultant")

    @app.get("/jobs/{job_id}/swot/final", tags=["swot"])
    def swot_final(job_id: str, request: Request):
        return mgr(request).get_swot_final(job_id)

    # ---- RCA
    @app.post("/jobs/{job_id}/rca", status_code=202, tags=["rca"])
    def rca_start(job_id: str, body: RcaRequest, request: Request):
        items = [clean("item", i, 80, required=True) for i in body.items]
        if any(not ITEM_ID.match(i) for i in items):
            raise JobError(422, "invalid item id")
        notes = [n for n in (clean("note", n, 500) for n in body.notes) if n]
        stage = mgr(request).start_rca(job_id, items, body.quadrant, body.max_whys, notes)
        return {"stage": stage}

    @app.get("/jobs/{job_id}/rca", tags=["rca"])
    def rca_list(job_id: str, request: Request):
        return mgr(request).list_rca(job_id)

    @app.get("/jobs/{job_id}/rca/{name}", tags=["rca"])
    def rca_get(job_id: str, name: str, request: Request):
        return mgr(request).get_rca(job_id, name)

    # ---- files
    @app.get("/jobs/{job_id}/artifacts", tags=["files"])
    def artifacts(job_id: str, request: Request):
        return mgr(request).list_artifacts(job_id)

    @app.get("/jobs/{job_id}/files/{path:path}", tags=["files"])
    def download(job_id: str, path: str, request: Request):
        p = mgr(request).resolve_file(job_id, path)
        media = EXTRA_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return FileResponse(p, media_type=media)

    return app