import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from jose import JWTError

from app.core.security import verify_token

logger = logging.getLogger("mindtrade.websockets")

router = APIRouter(tags=["websockets"])


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[UUID, set[WebSocket]] = {}

    async def connect(self, account_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(account_id, set()).add(websocket)

    def disconnect(self, account_id: UUID, websocket: WebSocket) -> None:
        conns = self._connections.get(account_id)
        if conns is not None:
            conns.discard(websocket)
            if not conns:
                self._connections.pop(account_id, None)

    async def send_to_account(self, account_id: UUID, message: dict) -> None:
        for ws in list(self._connections.get(account_id, ())):
            try:
                await ws.send_json(message)
            except Exception:
                logger.exception("Failed to send WS message to account %s", account_id)


manager = ConnectionManager()


@router.websocket("/ws/account/{account_id}")
async def account_ws(websocket: WebSocket, account_id: UUID, token: str = Query(...)) -> None:
    """Per-account push channel. Since this carries sensitive per-trader
    data (KYC decisions now, order/portfolio updates from Sprint 7 on),
    the connecting token must belong to that account or to an admin --
    browsers can't set custom headers on a WebSocket handshake, so the
    JWT is passed as a query param instead (`?token=...`), same as the
    common pattern for WS auth.
    """
    try:
        payload = verify_token(token)
        token_account_id = UUID(payload.get("sub"))
        token_role = payload.get("role")
    except (JWTError, TypeError, ValueError):
        await websocket.close(code=1008)  # policy violation
        return

    if token_account_id != account_id and token_role != "admin":
        await websocket.close(code=1008)
        return

    await manager.connect(account_id, websocket)
    try:
        while True:
            # Server -> client push only for now; still need to consume
            # incoming frames so WebSocketDisconnect fires on client close.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(account_id, websocket)


async def notify_kyc_status_update(account_id: UUID, kyc_status: str, review_notes: Optional[str] = None) -> None:
    await manager.send_to_account(
        account_id,
        {"event": "kyc_status_update", "kyc_status": kyc_status, "review_notes": review_notes},
    )
