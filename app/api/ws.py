from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import state

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
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
