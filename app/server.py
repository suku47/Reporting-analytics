"""
FastAPI application setup and static file serving.
State and query functions live in app.core.database to avoid circular imports.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import sys

from app.api import scene, tracks, gates, video, analytics, export, filters, batch, trajectories

# Re-export for run.py convenience
from app.core.database import load_traf, load_video, load_image  # noqa: F401


def _get_base_dir() -> Path:
    """Resolve base directory — works both in source and PyInstaller frozen mode."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle: data files are in sys._MEIPASS (onefile)
        # or next to the exe (onedir / COLLECT mode)
        bundle_dir = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else Path(sys.executable).parent
        return bundle_dir
    return Path(__file__).resolve().parent.parent


# ── App ──
app = FastAPI(title="Traffic Analytics Viewer", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Mount API routers ──
app.include_router(scene.router, prefix="/api")
app.include_router(tracks.router, prefix="/api")
app.include_router(gates.router, prefix="/api")
app.include_router(video.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(filters.router, prefix="/api")
app.include_router(batch.router, prefix="/api")
app.include_router(trajectories.router, prefix="/api")

# ── Static files ──
BASE_DIR = _get_base_dir()
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ── Frontend route ──
@app.get("/", response_class=HTMLResponse)
def serve_index():
    html_path = BASE_DIR / "templates" / "index.html"
    return html_path.read_text(encoding='utf-8')
