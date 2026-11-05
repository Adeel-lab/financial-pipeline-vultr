"""Configuration. Every value can be overridden with an environment variable."""
import os
import re
import sys
from pathlib import Path


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


ROOT = Path(os.environ.get("FINANALYST_ROOT") or Path(__file__).resolve().parent.parent).resolve()
INFO_DIR = Path(os.environ.get("FINANALYST_INFO_DIR") or ROOT / "information_new").resolve()
PLAN_DIR = Path(os.environ.get("FINANALYST_PLAN_DIR") or ROOT / "planning").resolve()

from .env import load_dotenv_files  # noqa: E402

load_dotenv_files()  # .env must be loaded before the settings below are read
JOBS_DIR = Path(os.environ.get("FINANALYST_JOBS_DIR") or ROOT / "jobs").resolve()
DB_PATH = JOBS_DIR / "jobs.db"
PYTHON = os.environ.get("FINANALYST_PYTHON") or sys.executable

# script of every stage that the orchestrator runs
SCRIPTS = {
    "discovery": INFO_DIR / "discovery.py",
    "triage": INFO_DIR / "triage.py",
    "resolve": INFO_DIR / "resolve.py",
    "verify": INFO_DIR / "verify.py",
    "sentiment": INFO_DIR / "sentiment.py",
    "filing_explanations": INFO_DIR / "filing_explanations.py",
    "federal_register": INFO_DIR / "federal_register.py",
    "swot_builder": INFO_DIR / "swot_builder.py",
    "rca": INFO_DIR / "rca.py",
    "sec_peers": INFO_DIR / "sec_peers.py",
    "financial_gate": PLAN_DIR / "financial_gate.py",
}

MAX_WORKERS = _int("FINANALYST_MAX_WORKERS", 6)          # background tasks at once
NEWS_CONCURRENCY = _int("FINANALYST_NEWS_CONCURRENCY", 1)  # news pipelines at once (browser + rate limits)
MAX_UPLOAD_BYTES = _int("FINANALYST_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)

# seconds a stage may run before it is killed
TIMEOUTS = {
    "finance_preview": 300, "finance_approve": 600, "discovery": 1800, "triage": 1200,
    "resolve": 1800, "verify": 3000, "sentiment": 3600, "swot_build": 1800,
    "swot_review": 120, "rca": 2400, "peers": 300,
}


def timeout_for(stage: str) -> int:
    base = re.sub(r"_\d+$", "", stage)
    return _int("FINANALYST_TIMEOUT_" + base.upper(), TIMEOUTS.get(base, 1800))


# sandbox: "local" runs every stage as a plain subprocess; "docker" runs SANDBOX_STAGES in a container
SANDBOX = os.environ.get("FINANALYST_SANDBOX", "local").strip().lower()
SANDBOX_STAGES = {s.strip() for s in os.environ.get("FINANALYST_SANDBOX_STAGES", "verify").split(",") if s.strip()}
DOCKER_IMAGE = os.environ.get("FINANALYST_DOCKER_IMAGE", "finanalyst-sandbox")
DOCKER_MEMORY = os.environ.get("FINANALYST_DOCKER_MEMORY", "2g")
DOCKER_CPUS = os.environ.get("FINANALYST_DOCKER_CPUS", "2")
DOCKER_PIDS = os.environ.get("FINANALYST_DOCKER_PIDS", "512")
DOCKER_EXTRA_ARGS = os.environ.get("FINANALYST_DOCKER_EXTRA_ARGS", "").split()

CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "FINANALYST_CORS_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]