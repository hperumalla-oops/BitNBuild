"""
One process that serves the map and answers chat.

    python -m uvicorn server.app:app --port 8000 --reload

  /             -> BitNBuild/web/map.html
  /output/*     -> the generated files (the map's offline fallback)
  /api/chat     -> the agent; OpenAI and Tavily keys never leave this process

Run it from the repo root.
"""

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

from .agent import CHAT_MODEL, run
from .tools import Tools

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "BitNBuild" / "web"
OUTPUT = ROOT / "BitNBuild" / "output"
DATA = ROOT / "BitNBuild" / "data"
ENV_FILE = ROOT / ".env"


def load_env(path):
    """Read .env without a python-dotenv dependency. Keys are stripped, so a
    stray space (TAVILY_API_KEY =…) still resolves."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = {**load_env(ENV_FILE), **os.environ}

SUPABASE_URL = ENV.get("SUPABASE_URL", "")
SERVICE_KEY = ENV.get("SUPABASE_SECRET_KEY", "")
OPENAI_KEY = ENV.get("OPENAI_API", "") or ENV.get("OPENAI_API_KEY", "")
TAVILY_KEY = ENV.get("TAVILY_API_KEY", "")

_client = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None
_tools = (Tools(SUPABASE_URL, SERVICE_KEY, _client, TAVILY_KEY)
          if (_client and SUPABASE_URL and SERVICE_KEY) else None)

app = FastAPI(title="Bengaluru Ward Atlas")


# In-memory, per-instance rate limit. On serverless each instance has its own
# counter and cold starts reset it, so this is a speed bump against accidental
# hammering — NOT access control. Put Vercel Deployment Protection in front of
# a public deploy if you care about who can spend your OpenAI credits.
_HITS: dict[str, list[float]] = {}
RATE_LIMIT = int(ENV.get("CHAT_RATE_LIMIT", "20"))     # requests
RATE_WINDOW = int(ENV.get("CHAT_RATE_WINDOW", "300"))  # seconds


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HITS.get(ip, []) if now - t < RATE_WINDOW]
    hits.append(now)
    _HITS[ip] = hits[-RATE_LIMIT * 2:]
    return len(hits) > RATE_LIMIT


class ChatRequest(BaseModel):
    messages: list[dict]
    mode: str = "map"


@app.get("/api/health")
def health():
    """What the UI checks on load, so it can disable chat with a real reason."""
    return {
        "ok": bool(_tools),
        "chat_model": CHAT_MODEL,
        "supabase": bool(SUPABASE_URL and SERVICE_KEY),
        "openai": bool(OPENAI_KEY),
        "tavily": bool(TAVILY_KEY),
        "missing": [k for k, v in {
            "SUPABASE_URL": SUPABASE_URL, "SUPABASE_SECRET_KEY": SERVICE_KEY,
            "OPENAI_API": OPENAI_KEY, "TAVILY_API_KEY": TAVILY_KEY,
        }.items() if not v],
    }


@app.get("/api/config.js")
def config_js():
    """The browser's Supabase config, built from env vars at request time.

    A generated, gitignored config.js cannot exist on a deployed build, so the
    page asks for it here instead. Only the PUBLISHABLE key is ever sent — it
    is public by design and row level security is what protects the data. The
    service key stays in this process.
    """
    pub = ENV.get("SUPABASE_PUBLISHABLE_KEY", "")
    lines = ["// served by /api/config.js — built from environment variables"]
    if SUPABASE_URL and pub:
        lines.append("window.SUPABASE_CONFIG = "
                     + json.dumps({"url": SUPABASE_URL, "key": pub}) + ";")
    else:
        lines.append("// no Supabase credentials set; the map reads its local files")
    return Response(content="\n".join(lines) + "\n",
                    media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


# A rewrite that does not preserve the path would land here; say so plainly
# rather than 404ing, so the first deploy is easy to diagnose.
@app.get("/api/index")
def api_index():
    return {"ok": True, "note": "API root. Try /api/health."}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request):
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "unknown"))
    if rate_limited(ip):
        return JSONResponse(status_code=429, content={
            "error": f"Rate limit: {RATE_LIMIT} messages per "
                     f"{RATE_WINDOW // 60} minutes. Try again shortly."})
    if not _tools:
        return JSONResponse(status_code=503, content={
            "error": "Server is missing credentials. Check /api/health."})
    if not req.messages:
        return JSONResponse(status_code=400, content={"error": "no messages"})
    try:
        reply, trace = run(req.messages, req.mode, _tools, _client)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"})
    return {"reply": reply, "tools_used": trace, "mode": req.mode}


@app.get("/")
def index():
    return RedirectResponse("/web/map.html")


# Mounted last so /api/* wins. /data carries the MLA and MP photographs, which
# the panel references as ../data/mla_photos/… — the same path a plain
# `python -m http.server` inside BitNBuild/ would serve them on.
app.mount("/output", StaticFiles(directory=str(OUTPUT)), name="output")
app.mount("/data", StaticFiles(directory=str(DATA)), name="data")
app.mount("/web", StaticFiles(directory=str(WEB), html=True), name="web")
