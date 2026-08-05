from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from .models import EventEnvelope, Subscription

router = APIRouter(prefix="/v1/agent", tags=["autonomous events"])


@router.put("/subscriptions/{subscription_id}")
async def put_subscription(subscription_id: str, body: Subscription, request: Request):
    if body.id != subscription_id:
        raise HTTPException(422, "path id and body id differ")
    stored = request.app.state.event_store.put_subscription(body)
    return {"accepted": True, "subscription": stored}


@router.get("/subscriptions")
async def subscriptions(request: Request):
    return {"subscriptions": [item.model_dump(mode="json")
                              for item in request.app.state.event_store.subscriptions()]}


@router.post("/events")
async def ingest_event(body: EventEnvelope, request: Request):
    return await request.app.state.event_engine.process(
        body,
        llm=lambda prompt, system: request.app.state.gateway.complete(prompt, system),
        transport=request.app.state.gateway,
    )


@router.get("/events")
async def event_history(request: Request, after: int = Query(default=0, ge=0)):
    return {"events": request.app.state.event_store.events(after=after)}


@router.get("/events/stream")
async def stream_events(request: Request, after: int = Query(default=0, ge=0)):
    """Live telemetry with cursor replay: reconnect with the last sequence."""
    async def generate():
        cursor = after
        while not await request.is_disconnected():
            records = request.app.state.event_store.events(after=cursor)
            if not records:
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)
                continue
            for record in records:
                cursor = record["sequence"]
                yield f"id: {cursor}\nevent: autonomous_event\ndata: {json.dumps(record)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
