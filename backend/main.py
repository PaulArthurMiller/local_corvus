"""
Local Corvus — FastAPI application entry point.

Registers all routers. Contains no business logic.
"""

import logging
import os

import httpx
from fastapi import FastAPI

from api.chat import router as chat_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Local Corvus Backend")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(chat_router)

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
