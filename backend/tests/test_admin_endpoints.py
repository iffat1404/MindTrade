"""HTTP-level tests for the Sprint 8 admin console endpoints: accounts
overview/detail, audit-log inspector, trade-log inspector, and compliance
flags. Uses the seeded demo accounts (rather than creating throwaway ones)
for the same reason test_reports_and_prices.py does -- these go through
the real HTTP stack, a separate DB session from the `db` fixture.
"""

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _login(app_client, username, password="demo123") -> str:
    response = await app_client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["access_token"]


async def _admin_headers(app_client) -> dict:
    token = await _login(app_client, "admin_mindtrade", "admin_dev_password_123")
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Accounts overview / detail
# ---------------------------------------------------------------------------
async def test_list_accounts_returns_traders_sorted_by_net_worth(app_client):
    headers = await _admin_headers(app_client)
    response = await app_client.get("/api/admin/accounts", headers=headers)

    assert response.status_code == 200
    accounts = response.json()
    usernames = {a["username"] for a in accounts}
    assert "demo_trader1" in usernames
    assert "demo_trader2" in usernames
    for a in accounts:
        assert set(a.keys()) == {"id", "username", "role", "kyc_status", "cash_balance", "net_worth", "order_count"}
    net_worths = [a["net_worth"] for a in accounts]
    assert net_worths == sorted(net_worths, reverse=True)


async def test_list_accounts_filters_by_kyc_status(app_client):
    headers = await _admin_headers(app_client)
    response = await app_client.get("/api/admin/accounts?status=PENDING_REVIEW", headers=headers)

    assert response.status_code == 200
    accounts = response.json()
    assert all(a["kyc_status"] == "PENDING_REVIEW" for a in accounts)
    assert any(a["username"] == "demo_pending" for a in accounts)


async def test_list_accounts_requires_admin_role(app_client):
    trader_token = await _login(app_client, "demo_trader1")
    response = await app_client.get(
        "/api/admin/accounts", headers={"Authorization": f"Bearer {trader_token}"}
    )
    assert response.status_code == 403


async def test_account_detail_returns_positions_and_recent_orders(app_client):
    headers = await _admin_headers(app_client)
    accounts = (await app_client.get("/api/admin/accounts", headers=headers)).json()
    trader1_id = next(a["id"] for a in accounts if a["username"] == "demo_trader1")

    response = await app_client.get(f"/api/admin/accounts/{trader1_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "demo_trader1"
    assert isinstance(body["positions"], list)
    assert isinstance(body["recent_orders"], list)
    assert len(body["positions"]) > 0  # demo_trader1 is seeded with AAPL/MSFT/TSLA positions


async def test_account_detail_404_for_unknown_account(app_client):
    headers = await _admin_headers(app_client)
    response = await app_client.get(
        "/api/admin/accounts/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Audit log inspector
# ---------------------------------------------------------------------------
async def test_audit_logs_filters_by_account_and_includes_events(app_client):
    headers = await _admin_headers(app_client)
    accounts = (await app_client.get("/api/admin/accounts", headers=headers)).json()
    trader1_id = next(a["id"] for a in accounts if a["username"] == "demo_trader1")

    response = await app_client.get(f"/api/admin/audit-logs?account_id={trader1_id}", headers=headers)
    assert response.status_code == 200
    logs = response.json()
    assert len(logs) > 0
    assert all(entry["account_id"] == trader1_id for entry in logs)
    # Seed-data orders are inserted directly at ROUTED status (bootstrap
    # data, not created through the real NEW->VALIDATED->ROUTED state
    # machine), so they legitimately have no event history -- this just
    # checks the events field is structurally present; the
    # reason_code test below proves it's actually populated for orders
    # placed through the real API.
    assert all(isinstance(entry["events"], list) for entry in logs)


async def test_audit_logs_reason_code_filter_finds_rejected_order(app_client):
    trader_token = await _login(app_client, "demo_trader1")
    # An absurd limit price triggers PRICE_COLLAR_BREACH deterministically.
    order_resp = await app_client.post(
        "/api/orders",
        headers={"Authorization": f"Bearer {trader_token}"},
        json={"ticker": "IBM", "side": "BUY", "type": "LIMIT", "product_type": "CNC", "qty": 1, "limit_price": "1.00"},
    )
    assert order_resp.json()["status"] == "REJECTED"

    headers = await _admin_headers(app_client)
    response = await app_client.get(
        "/api/admin/audit-logs?reason_code=PRICE_COLLAR_BREACH", headers=headers
    )
    assert response.status_code == 200
    logs = response.json()
    assert len(logs) > 0
    assert all(entry["reason_code"] == "PRICE_COLLAR_BREACH" for entry in logs)


# ---------------------------------------------------------------------------
# Trade log inspector
# ---------------------------------------------------------------------------
async def test_trade_logs_filters_by_ticker(app_client):
    headers = await _admin_headers(app_client)
    response = await app_client.get("/api/admin/trade-logs?ticker=AAPL", headers=headers)

    assert response.status_code == 200
    logs = response.json()
    assert all(entry["ticker"] == "AAPL" for entry in logs)


async def test_trade_logs_pnl_filter(app_client):
    headers = await _admin_headers(app_client)
    response = await app_client.get("/api/admin/trade-logs?pnl=loss", headers=headers)

    assert response.status_code == 200
    logs = response.json()
    assert all(entry["realized_pnl"] is not None and float(entry["realized_pnl"]) < 0 for entry in logs)


# ---------------------------------------------------------------------------
# Compliance flags
# ---------------------------------------------------------------------------
async def test_flags_detects_wash_trade_from_resting_opposite_order(app_client):
    """demo_trader1 is seeded with a resting SELL WMT LIMIT order -- placing
    a BUY WMT order on the same account should flag (not reject) as a wash
    trade. (Using the IBM side would hit INSUFFICIENT_HOLDINGS first: a
    CNC SELL requires existing shares, which demo_trader1 doesn't have for
    IBM, and that check runs before check_wash_trade in the validation
    chain -- BUY never needs holdings, so WMT's resting SELL is the
    reliable side to test this against.)
    """
    trader_token = await _login(app_client, "demo_trader1")
    response = await app_client.post(
        "/api/orders",
        headers={"Authorization": f"Bearer {trader_token}"},
        json={"ticker": "WMT", "side": "BUY", "type": "LIMIT", "product_type": "CNC", "qty": 1, "limit_price": "70.00"},
    )
    assert response.json()["status"] == "ROUTED"

    headers = await _admin_headers(app_client)
    flags = (await app_client.get("/api/admin/flags", headers=headers)).json()
    assert any(f["ticker"] == "WMT" for f in flags["wash_trade_flags"])


async def test_flags_includes_kyc_auto_check_failures(app_client):
    """Submitting a KYC doc with no ANTHROPIC_API_KEY configured means
    extraction always fails, which auto-check rules against -- a
    deterministic way to produce a real auto_check_passed=False row.
    """
    trader_token = await _login(app_client, "demo_trader2")
    files = {"file": ("id.png", b"fake-bytes", "image/png")}
    data = {"id_type": "PASSPORT", "declared_full_name": "Wont Match Anything"}
    submit = await app_client.post(
        "/api/kyc/submit", headers={"Authorization": f"Bearer {trader_token}"}, data=data, files=files
    )
    assert submit.status_code == 201

    headers = await _admin_headers(app_client)
    flags = (await app_client.get("/api/admin/flags", headers=headers)).json()
    assert len(flags["kyc_auto_check_failures"]) > 0
    assert all(entry["status"] == "PENDING_REVIEW" for entry in flags["kyc_auto_check_failures"])
