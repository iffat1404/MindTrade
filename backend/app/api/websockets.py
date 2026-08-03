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


async def notify_order_update(account_id: UUID, order: dict) -> None:
    await manager.send_to_account(account_id, {"event": "order_update", "order": order})


async def notify_portfolio_update(account_id: UUID, portfolio: dict) -> None:
    await manager.send_to_account(account_id, {"event": "portfolio_update", "portfolio": portfolio})


# ---------------------------------------------------------------------------
# Market tick channel (Sprint 7, Task 7.2) -- public, no auth: the feed
# simulator replays historical data, not live confidential positions, so
# there's nothing sensitive to gate here (unlike /ws/account and
# /ws/admin below).
# ---------------------------------------------------------------------------
class TickerConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, ticker: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(ticker, set()).add(websocket)

    def disconnect(self, ticker: str, websocket: WebSocket) -> None:
        conns = self._connections.get(ticker)
        if conns is not None:
            conns.discard(websocket)
            if not conns:
                self._connections.pop(ticker, None)

    async def send_to_ticker(self, ticker: str, message: dict) -> None:
        for ws in list(self._connections.get(ticker, ())):
            try:
                await ws.send_json(message)
            except Exception:
                logger.exception("Failed to send WS message for ticker %s", ticker)


market_manager = TickerConnectionManager()


@router.websocket("/ws/market/{ticker}")
async def market_ws(websocket: WebSocket, ticker: str) -> None:
    ticker = ticker.strip().upper()
    await market_manager.connect(ticker, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        market_manager.disconnect(ticker, websocket)


async def broadcast_market_tick(ticker: str, tick: dict) -> None:
    await market_manager.send_to_ticker(ticker.upper(), {"event": "tick", **tick})


# ---------------------------------------------------------------------------
# Admin notifications channel (Sprint 7, Task 7.2): new KYC submissions +
# wash-trade compliance flags, platform-wide (not per-account, so this is
# a simple broadcast set rather than the per-account ConnectionManager above).
# ---------------------------------------------------------------------------
class BroadcastConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        for ws in list(self._connections):
            try:
                await ws.send_json(message)
            except Exception:
                logger.exception("Failed to send WS message to an admin connection")


admin_manager = BroadcastConnectionManager()


@router.websocket("/ws/admin/notifications")
async def admin_notifications_ws(websocket: WebSocket, token: str = Query(...)) -> None:
    try:
        payload = verify_token(token)
        role = payload.get("role")
    except (JWTError, TypeError, ValueError):
        await websocket.close(code=1008)
        return
    if role != "admin":
        await websocket.close(code=1008)
        return

    await admin_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        admin_manager.disconnect(websocket)


async def notify_admin(event: dict) -> None:
    await admin_manager.broadcast(event)
