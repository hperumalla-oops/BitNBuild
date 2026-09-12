"""
Vercel entry point.

Vercel runs ASGI apps from api/index.py, so this just re-exports the same
FastAPI app that `uvicorn server.app:app` serves locally. Nothing about the
app is Vercel-specific — it reads os.environ when there is no .env file.
"""

import sys
from pathlib import Path

# the function's working directory is the repo root on Vercel, but be explicit
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.app import app  # noqa: E402

__all__ = ["app"]
