import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.db import AsyncSessionLocal
from app.main import app


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def app_client():
    """Boots the app once for the whole test session on the SAME event loop
    pytest-asyncio uses for every other async fixture/test (see pytest.ini's
    asyncio_default_fixture_loop_scope = session).

    Deliberately httpx.AsyncClient + ASGITransport, not Starlette's
    TestClient: TestClient runs the app in a background thread with its own
    anyio portal/event loop, which conflicts with async fixtures like `db`
    below running on pytest-asyncio's loop -- asyncpg connections aren't
    safe to share across event loops, and that mismatch is exactly what
    caused this suite to hang the first time around.
    """
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


@pytest_asyncio.fixture(loop_scope="session")
async def db(app_client):
    """A real AsyncSession against the live test database. Rolled back
    after every test so throwaway accounts/orders created in tests don't
    accumulate.
    """
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()
