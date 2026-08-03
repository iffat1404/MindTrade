"""HTTP-level tests for the Sprint 7 reports/prices endpoints. Uses the
seeded demo_trader1 account (rather than creating a throwaway one) since
these tests go through the real HTTP stack -- a separate DB session from
the `db` fixture -- so a freshly created-but-uncommitted account wouldn't
be visible to it, and committing one here would leak into the persistent
dev DB the way test_feed_simulator.py's internally-committing calls do.
"""

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _login(app_client, username="demo_trader1", password="demo123") -> str:
    response = await app_client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


async def test_reports_portfolio_returns_pnl_statement(app_client):
    token = await _login(app_client)
    response = await app_client.get("/api/reports/portfolio", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert "cash_balance" in body
    assert "realized_pnl_total" in body
    assert "unrealized_pnl_total" in body
    assert "net_worth" in body
    assert isinstance(body["positions"], list)


async def test_reports_portfolio_export_returns_csv(app_client):
    token = await _login(app_client)
    response = await app_client.get(
        "/api/reports/portfolio/export", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    first_line = response.text.splitlines()[0]
    assert first_line == "timestamp,ticker,side,qty,price,fees,realized_pnl"


async def test_prices_daily_returns_ohlcv_list(app_client):
    token = await _login(app_client)
    response = await app_client.get("/api/prices/AAPL/daily", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) > 0
    assert set(body[0].keys()) == {"date", "open", "high", "low", "close", "volume"}


async def test_prices_intraday_returns_minute_bars(app_client):
    token = await _login(app_client)
    response = await app_client.get("/api/prices/AAPL/intraday", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) > 0
    assert set(body[0].keys()) == {"timestamp", "open", "high", "low", "close", "volume"}


async def test_prices_unknown_ticker_returns_empty_list(app_client):
    token = await _login(app_client)
    response = await app_client.get("/api/prices/NOPE/daily", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == []
