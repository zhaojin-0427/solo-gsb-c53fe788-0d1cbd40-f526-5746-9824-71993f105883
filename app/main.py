"""Stateful interface upgrade compatibility checker.

POST /compare checks whether every request/response trace of the old state
model (up to a bounded number of requests) can be reproduced by the new one.
"""

from fastapi import FastAPI, HTTPException

from .engine import EngineError, compare_models
from .schemas import CompareRequest

app = FastAPI(
    title="Stateful Interface Upgrade Compatibility Checker",
    version="1.0.0",
    description=(
        "Bounded trace-inclusion checking for stateful interface models with "
        "32-bit unsigned registers, linear guards, tau steps and named "
        "requests. Powered by z3."
    ),
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/compare")
def compare(req: CompareRequest):
    try:
        return compare_models(req.old, req.new, req.max_requests)
    except EngineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
