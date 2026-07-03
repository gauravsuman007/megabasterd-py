"""The `/ws` WebSocket endpoint. Each connected browser tab is registered as a
subscriber; transfer progress is pushed to all subscribers via `state.broadcast`.
Inbound messages are ignored (the socket is push-only) -- receiving just keeps
the connection open and detects disconnects."""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import state

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Accept a tab's WebSocket, register it for progress broadcasts, and remove
    it on disconnect."""
    await websocket.accept()
    state.subscribers.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in state.subscribers:
            state.subscribers.remove(websocket)
