"""FinAnalyst entry point: the API plus the web interface.

    python app.py                                   # development
    uvicorn app:app --host 0.0.0.0 --port 8000      # server

Interface:  http://127.0.0.1:8000/ui        Technical API docs:  http://127.0.0.1:8000/docs
"""
import os
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse

from backend.api import create_app

ROOT = Path(__file__).resolve().parent
UI_FILE = ROOT / "ui" / "index.html"

app = create_app()


@app.get("/ui", include_in_schema=False)
def ui():
    if not UI_FILE.is_file():
        return JSONResponse({"detail": f"{UI_FILE} was not found. Put the ui folder next to app.py."}, status_code=404)
    return FileResponse(UI_FILE, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    print(f"\n  FinAnalyst interface:  http://{shown}:{port}/ui\n  Technical API docs:    http://{shown}:{port}/docs\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
