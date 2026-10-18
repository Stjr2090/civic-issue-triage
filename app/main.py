"""FastAPI application entrypoint."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import reports, whatsapp

app = FastAPI(title="Civic Issue Triage", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.include_router(reports.router, prefix="/api")
app.include_router(whatsapp.router)  # /webhook at root (Meta posts here)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC_DIR / "dashboard.html")
