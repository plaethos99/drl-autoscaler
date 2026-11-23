"""Resizer service: POST raw image bytes to /resize -> 224x224 JPEG thumbnail."""
from fastapi import FastAPI, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from . import core
from .metrics import instrument

app = FastAPI(title="resizer")
instrument(app, "resizer")


@app.post("/resize")
async def resize(request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body: send raw image bytes")
    try:
        thumb = await run_in_threadpool(core.resize_image, data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid image: {exc}") from exc
    return Response(thumb, media_type="image/jpeg")
