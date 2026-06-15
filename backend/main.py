import httpx
import os
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Local Corvus Backend")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

class ChatRequest(BaseModel):
    message: str

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

@app.post("/chat")
async def chat(request: ChatRequest):
    payload = {
        "model": "gemma4:e4b",
        "messages": [{"role": "user", "content": request.message}],
        "stream": False
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{OLLAMA_HOST}/v1/chat/completions",
            json=payload,
            timeout=60.0
        )
    data = response.json()
    return {"response": data["choices"][0]["message"]["content"]}