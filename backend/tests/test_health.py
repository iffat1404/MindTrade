import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_health_returns_ok(app_client):
    response = await app_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
