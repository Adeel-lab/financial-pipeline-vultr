"""Environment handling: loads .env files and maps the VULTR_* settings to what every script expects."""
import os
from pathlib import Path

from . import config


def load_dotenv_files() -> None:
    """Read KEY=VALUE lines from .env files. Variables already set in the terminal always win."""
    for path in (config.ROOT / ".env", config.PLAN_DIR / ".env"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def llm_key_present() -> bool:
    return bool(os.environ.get("VULTR_INFERENCE_API_KEY"))


def build_env(extra: dict | None = None) -> dict:
    """Environment for a local stage process.

    The pipeline scripts read VULTR_INFERENCE_*; rca.py goes through llm_client.py, which reads
    LLM_API_KEY / GROQ_API_KEY, LLM_BASE_URL and LLM_MODEL. Both are filled from the same settings.
    """
    env = dict(os.environ)
    key = env.get("VULTR_INFERENCE_API_KEY", "")
    base = env.get("VULTR_INFERENCE_BASE", "")
    model = env.get("VULTR_CHAT_MODEL", "")
    if key:
        env.setdefault("LLM_API_KEY", key)
        env.setdefault("GROQ_API_KEY", key)
    if base:
        env.setdefault("LLM_BASE_URL", base)
    if model:
        env.setdefault("LLM_MODEL", model)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
    if extra:
        env.update(extra)
    return env


def sandbox_env() -> dict:
    """The only variables a sandboxed stage may see. No API keys, ever."""
    return {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"}
