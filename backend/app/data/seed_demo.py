import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_password_hash
from app.models.orm import Account, KYCSubmission, Order, Position

logger = logging.getLogger("mindtrade.seed")

# Documented in HOW_TO_USE_MASTER_BUILD_PLAN.md's Sprint 1 review step as
# the demo login credentials -- intentionally doesn't satisfy
# RegisterRequest's password-complexity rule, since these accounts are
# constructed directly rather than going through POST /api/auth/register.
_DEMO_PASSWORD = "demo123"


async def _get_account(session: AsyncSession, username: str) -> Optional[Account]:
    return await session.scalar(select(Account).where(Account.username == username))


async def seed_database(session: AsyncSession) -> None:
    """Idempotent: creates the admin account (from
    ADMIN_BOOTSTRAP_USERNAME/PASSWORD) plus 3 demo trader accounts if they
    don't already exist. Safe to call on every app startup.
    """
    if await _get_account(session, settings.ADMIN_BOOTSTRAP_USERNAME) is None:
        session.add(
            Account(
                username=settings.ADMIN_BOOTSTRAP_USERNAME,
                password_hash=get_password_hash(settings.ADMIN_BOOTSTRAP_PASSWORD),
                role="admin",
                kyc_status="APPROVED",
            )
        )
        logger.info("Seeded admin account %r", settings.ADMIN_BOOTSTRAP_USERNAME)
    else:
        logger.info("Admin account already exists, skipping")

    if await _get_account(session, "demo_trader1") is None:
        trader1 = Account(
            username="demo_trader1",
            password_hash=get_password_hash(_DEMO_PASSWORD),
            role="trader",
            kyc_status="APPROVED",
            starting_capital=Decimal("1000000.00"),
            cash_balance=Decimal("1000000.00"),
        )
        session.add(trader1)
        await session.flush()  # populate trader1.id for the FKs below

        session.add(
            KYCSubmission(
                account_id=trader1.id,
                id_type="PASSPORT",
                document_path="seed/demo_trader1_passport.pdf",
                extracted_full_name="Demo Trader One",
                extracted_dob=date(1995, 6, 15),
                extracted_id_number="X1234567",
                extracted_issuing_country="USA",
                extraction_confidence="HIGH",
                auto_check_passed=True,
                status="APPROVED",
            )
        )

        # A few pre-filled positions, per HOW_TO_USE_MASTER_BUILD_PLAN.md's
        # Task 1.5 example: long AAPL/MSFT, short TSLA.
        session.add(Position(account_id=trader1.id, ticker="AAPL", signed_qty=100, avg_cost=Decimal("185.00")))
        session.add(Position(account_id=trader1.id, ticker="MSFT", signed_qty=50, avg_cost=Decimal("370.00")))
        session.add(Position(account_id=trader1.id, ticker="TSLA", signed_qty=-20, avg_cost=Decimal("245.00")))

        # A couple of resting limit orders.
        session.add(
            Order(
                account_id=trader1.id,
                ticker="IBM",
                side="BUY",
                order_type="LIMIT",
                product_type="CNC",
                qty=10,
                remaining_qty=10,
                limit_price=Decimal("150.00"),
                status="VALIDATED",
            )
        )
        session.add(
            Order(
                account_id=trader1.id,
                ticker="WMT",
                side="SELL",
                order_type="LIMIT",
                product_type="CNC",
                qty=15,
                remaining_qty=15,
                limit_price=Decimal("165.00"),
                status="VALIDATED",
            )
        )
        logger.info("Seeded demo_trader1 (positions + resting limit orders)")
    else:
        logger.info("demo_trader1 already exists, skipping")

    if await _get_account(session, "demo_trader2") is None:
        session.add(
            Account(
                username="demo_trader2",
                password_hash=get_password_hash(_DEMO_PASSWORD),
                role="trader",
                kyc_status="APPROVED",
            )
        )
        logger.info("Seeded demo_trader2 (fresh account, no positions)")
    else:
        logger.info("demo_trader2 already exists, skipping")

    if await _get_account(session, "demo_pending") is None:
        pending = Account(
            username="demo_pending",
            password_hash=get_password_hash(_DEMO_PASSWORD),
            role="trader",
            kyc_status="PENDING_REVIEW",
        )
        session.add(pending)
        await session.flush()  # populate pending.id for the KYC submission FK

        session.add(
            KYCSubmission(
                account_id=pending.id,
                id_type="DRIVERS_LICENSE",
                document_path="seed/demo_pending_license.pdf",
                extracted_full_name="Demo Pending Trader",
                extracted_dob=date(1998, 3, 22),
                extracted_id_number="DL9988776",
                extracted_issuing_country="USA",
                extraction_confidence="MEDIUM",
                auto_check_passed=None,
                status="PENDING_REVIEW",
            )
        )
        logger.info("Seeded demo_pending (KYC submission awaiting review)")
    else:
        logger.info("demo_pending already exists, skipping")

    await session.commit()


if __name__ == "__main__":
    import asyncio

    from app.core.db import AsyncSessionLocal

    async def _main() -> None:
        async with AsyncSessionLocal() as session:
            await seed_database(session)

    asyncio.run(_main())
