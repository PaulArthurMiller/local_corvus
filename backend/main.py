"""
Local Corvus — FastAPI application entry point.

Registers all routers. Contains no business logic.
"""

import logging
import os

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.admin import router as admin_router
from api.chat import router as chat_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Local Corvus Backend")


@app.on_event("startup")
async def _sync_probes_on_startup():
    """Upsert probe definitions from YAML into the DB on every startup."""
    try:
        from analytics.probes import load_probe_set
        from memory.store import store as graph_store
        probes = load_probe_set(version="v1")
        await graph_store.sync_probes([p.model_dump() for p in probes])
        logger.info("startup: synced %d probe definitions to DB", len(probes))
    except Exception as exc:
        # Probe sync failure is non-fatal — log and continue
        logger.warning("startup: probe sync failed (non-fatal) — %s", exc)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(chat_router)
app.include_router(admin_router)

# ---------------------------------------------------------------------------
# Utility endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ollama-health")
async def ollama_health():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{OLLAMA_HOST}/api/tags", timeout=5.0)
            return {"status": "ok", "ollama": response.json()}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
