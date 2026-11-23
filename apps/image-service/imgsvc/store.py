"""Store service: keeps results in Redis (or memory when REDIS_URL is empty) and counts requests."""
import json
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import config
from .metrics import instrument


class MemoryBackend:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.count = 0

    async def save(self, rid: str, value: str) -> int:
        self.data[rid] = value
        self.count += 1
        return self.count

    async def get(self, rid: str):
        return self.data.get(rid)

    async def total(self) -> int:
        return self.count

    async def close(self):
        pass


class RedisBackend:
    def __init__(self, url: str):
        import redis.asyncio as aioredis

        self.r = aioredis.from_url(url)

    async def save(self, rid: str, value: str) -> int:
        await self.r.set(f"result:{rid}", value, ex=config.RESULT_TTL_S)
        return int(await self.r.incr("results:count"))

    async def get(self, rid: str):
        v = await self.r.get(f"result:{rid}")
        return v.decode() if v else None

    async def total(self) -> int:
        return int(await self.r.get("results:count") or 0)

    async def close(self):
        await self.r.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.backend = RedisBackend(config.REDIS_URL) if config.REDIS_URL else MemoryBackend()
    yield
    await app.state.backend.close()


app = FastAPI(title="store", lifespan=lifespan)
instrument(app, "store")


@app.post("/results")
async def save(item: dict):
    rid = uuid.uuid4().hex[:12]
    item = {**item, "id": rid, "stored_at": time.time()}
    count = await app.state.backend.save(rid, json.dumps(item))
    return {"id": rid, "count": count}


@app.get("/results/{rid}")
async def get(rid: str):
    value = await app.state.backend.get(rid)
    if value is None:
        raise HTTPException(404, "not found")
    return json.loads(value)


@app.get("/stats")
async def stats():
    return {"results": await app.state.backend.total()}
