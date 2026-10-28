"""Live split-screen dashboard: swarm vs baseline on the same seed.

    uvicorn app:app --port 8000      then open http://localhost:8000
    POST /start?seed=42&ticks=90     restarts both runs

Events stream to the browser as server-sent events (one-way push, no extra libs).
To switch to WebSockets later, replace /stream with a Starlette WebSocketRoute.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from events import EventLog
from runner import make_run

TICK_SECONDS = float(os.getenv("TICK_SECONDS", "1.0"))
FEED_TYPES = {"SIGHTING_REPORTED", "SIGHTING_CONFIRMED", "SIGHTING_DISMISSED",
              "PLAYBOOK_FOUND", "ACTION_PROPOSED", "ACTION_APPROVED", "ACTION_REJECTED",
              "ACTION_EXECUTED", "ANOMALY_CONTAINED", "CIVILIAN_STATUS"}

subscribers: set[asyncio.Queue] = set()
state = {"task": None, "last": {}}


async def broadcast(msg: dict) -> None:
    state["last"][msg["mode"]] = msg
    for q in list(subscribers):
        q.put_nowait(msg)


async def run_both(seed: int, ticks: int) -> None:
    log = EventLog()
    runs = {m: make_run(m, seed, log) for m in ("swarm", "baseline")}
    cursor = 0
    for _ in range(ticks):
        for mode, (fac, brain, _) in runs.items():
            fac.step()
            brain.act(fac)
        new, cursor = log.events[cursor:], len(log.events)
        for mode, (fac, _, _) in runs.items():
            feed = [e.to_dict() for e in new if e.run_id == fac.run_id and e.type in FEED_TYPES]
            await broadcast({"mode": mode, "seed": seed, "snapshot": fac.snapshot(), "feed": feed})
        await asyncio.sleep(TICK_SECONDS)


async def start(request: Request):
    seed = int(request.query_params.get("seed", 42))
    ticks = int(request.query_params.get("ticks", 90))
    if state["task"] and not state["task"].done():
        state["task"].cancel()
    state["last"] = {}
    state["task"] = asyncio.create_task(run_both(seed, ticks))
    return JSONResponse({"started": True, "seed": seed, "ticks": ticks})


async def stream(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)
    for msg in state["last"].values():
        q.put_nowait(msg)

    async def gen():
        try:
            while True:
                msg = await q.get()
                yield f"data: {json.dumps(msg)}\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


async def index(request: Request):
    return FileResponse(Path(__file__).parent / "static" / "index.html")


async def health(request: Request):
    return JSONResponse({"ok": True})


app = Starlette(routes=[Route("/", index), Route("/start", start, methods=["POST"]),
                        Route("/stream", stream), Route("/health", health)])
