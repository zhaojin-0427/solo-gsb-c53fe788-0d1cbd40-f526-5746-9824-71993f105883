"""HTTP API for the stateful interface upgrade compatibility checker."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .engine import InconclusiveError, check_compatibility
from .schemas import CompareRequest

app = FastAPI(
    title="Stateful API Upgrade Compatibility Checker",
    version="1.0.0",
    description=(
        "Checks, for every request/response trace of an old state model with up "
        "to a bounded number of requests, whether a new state model can "
        "reproduce it. Models are compared with a z3-based bounded "
        "trace-inclusion check."
    ),
)


@app.get("/")
def index() -> dict:
    return {
        "service": "stateful-api-upgrade-compatibility-checker",
        "endpoints": {
            "POST /compare": "compare an old and a new state model",
            "GET /healthz": "liveness probe",
            "GET /docs": "interactive OpenAPI documentation",
        },
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/compare")
def compare(request: CompareRequest) -> dict:
    try:
        return check_compatibility(request)
    except InconclusiveError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
