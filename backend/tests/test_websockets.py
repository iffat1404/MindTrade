"""Unit tests for the Sprint 7 WebSocket connection managers
(TickerConnectionManager for /ws/market/{ticker}, BroadcastConnectionManager
for /ws/admin/notifications) and their broadcast wrapper functions.

Tests the manager logic directly against fake WebSocket-like objects
rather than opening real WS connections through the ASGI app -- the
existing httpx.AsyncClient-based test fixtures aren't WS-capable, and the
protocol-level accept/receive/disconnect handling is already the same,
proven-correct pattern the pre-existing account_ws endpoint uses.
"""

import pytest

from app.api import websockets as ws

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeWebSocket:
    def __init__(self, *, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    async def accept(self) -> None:
        pass

    async def send_json(self, message: dict) -> None:
        if self.fail:
            raise RuntimeError("simulated send failure")
        self.sent.append(message)


# ---------------------------------------------------------------------------
# TickerConnectionManager (market tick channel)
# ---------------------------------------------------------------------------
async def test_ticker_manager_only_sends_to_subscribed_ticker():
    manager = ws.TickerConnectionManager()
    aapl_socket = _FakeWebSocket()
    msft_socket = _FakeWebSocket()
    await manager.connect("AAPL", aapl_socket)
    await manager.connect("MSFT", msft_socket)

    await manager.send_to_ticker("AAPL", {"event": "tick", "close": "100.00"})

    assert len(aapl_socket.sent) == 1
    assert msft_socket.sent == []


async def test_ticker_manager_disconnect_removes_subscriber():
    manager = ws.TickerConnectionManager()
    socket = _FakeWebSocket()
    await manager.connect("AAPL", socket)
    manager.disconnect("AAPL", socket)

    await manager.send_to_ticker("AAPL", {"event": "tick"})
    assert socket.sent == []


async def test_ticker_manager_broadcast_market_tick_uppercases_ticker(monkeypatch):
    manager = ws.TickerConnectionManager()
    socket = _FakeWebSocket()
    await manager.connect("AAPL", socket)
    monkeypatch.setattr(ws, "market_manager", manager)

    await ws.broadcast_market_tick("aapl", {"close": "100.00"})

    assert len(socket.sent) == 1
    assert socket.sent[0]["event"] == "tick"
    assert socket.sent[0]["close"] == "100.00"


async def test_ticker_manager_send_failure_does_not_raise():
    manager = ws.TickerConnectionManager()
    good_socket = _FakeWebSocket()
    bad_socket = _FakeWebSocket(fail=True)
    await manager.connect("AAPL", good_socket)
    await manager.connect("AAPL", bad_socket)

    await manager.send_to_ticker("AAPL", {"event": "tick"})  # must not raise
    assert len(good_socket.sent) == 1


# ---------------------------------------------------------------------------
# BroadcastConnectionManager (admin notifications channel)
# ---------------------------------------------------------------------------
async def test_broadcast_manager_sends_to_all_connections():
    manager = ws.BroadcastConnectionManager()
    a, b = _FakeWebSocket(), _FakeWebSocket()
    await manager.connect(a)
    await manager.connect(b)

    await manager.broadcast({"event": "kyc_submission"})

    assert len(a.sent) == 1
    assert len(b.sent) == 1


async def test_broadcast_manager_disconnect_stops_delivery():
    manager = ws.BroadcastConnectionManager()
    a, b = _FakeWebSocket(), _FakeWebSocket()
    await manager.connect(a)
    await manager.connect(b)
    manager.disconnect(a)

    await manager.broadcast({"event": "wash_trade_flag"})

    assert a.sent == []
    assert len(b.sent) == 1


async def test_notify_admin_delegates_to_admin_manager(monkeypatch):
    manager = ws.BroadcastConnectionManager()
    socket = _FakeWebSocket()
    await manager.connect(socket)
    monkeypatch.setattr(ws, "admin_manager", manager)

    await ws.notify_admin({"event": "kyc_submission", "submission_id": "abc"})

    assert socket.sent == [{"event": "kyc_submission", "submission_id": "abc"}]


# ---------------------------------------------------------------------------
# Per-account ConnectionManager (order/portfolio update wrappers, Sprint 7)
# ---------------------------------------------------------------------------
async def test_notify_order_update_and_portfolio_update(monkeypatch):
    import uuid

    manager = ws.ConnectionManager()
    account_id = uuid.uuid4()
    socket = _FakeWebSocket()
    await manager.connect(account_id, socket)
    monkeypatch.setattr(ws, "manager", manager)

    await ws.notify_order_update(account_id, {"order_id": "x", "status": "FILLED"})
    await ws.notify_portfolio_update(account_id, {"net_worth": "123.45"})

    assert len(socket.sent) == 2
    assert socket.sent[0]["event"] == "order_update"
    assert socket.sent[1]["event"] == "portfolio_update"
