"""Classifier service (the CPU-heavy one the agent mainly scales): POST image bytes to /classify."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from . import core
from .metrics import instrument


@asynccontextmanager
async def lifespan(_app: FastAPI):
    core.get_classifier()          # load the model before the pod reports ready
    yield


app = FastAPI(title="classifier", lifespan=lifespan)
instrument(app, "classifier")


@app.post("/classify")
async def classify(request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body: send raw image bytes")
    try:
        return await run_in_threadpool(core.get_classifier().classify, data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - PIL raises various errors for bad images
        raise HTTPException(400, f"invalid image: {exc}") from exc


@app.get("/info")
def info():
    c = core.get_classifier()
    return {"mode": c.mode, "labels": len(c.labels)}
