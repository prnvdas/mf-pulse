"""FastAPI price endpoint — same contract as the Cloudflare Worker.

The dashboard can't tell the difference between this, the Worker, and the
Lambda Function URL; they all answer ?symbols= with the same JSON shape.
"""

from __future__ import annotations

import pathlib
import sys

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy" / "aws-lambda"))
sys.path.insert(0, str(ROOT / "src"))

from handler import price_proxy  # noqa: E402 — reuse the Lambda's fetch logic

app = FastAPI(title="MF Pulse", docs_url="/api/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])


@app.get("/api")
def prices(symbols: str = Query(..., description="comma-separated, e.g. RELIANCE.NS")):
    wanted = [s.strip() for s in symbols.split(",") if s.strip()]
    if not wanted:
        raise HTTPException(400, "pass ?symbols=A.NS,B.NS")
    if len(wanted) > 150:
        raise HTTPException(400, "max 150 symbols per call")
    return JSONResponse(price_proxy(wanted),
                        headers={"Cache-Control": "public, max-age=30"})


@app.get("/api/health")
def health():
    return {"ok": True}
