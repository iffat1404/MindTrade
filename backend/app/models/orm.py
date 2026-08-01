import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


# ---------------------------------------------------------------------------
# 1. Account
# ---------------------------------------------------------------------------
class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (CheckConstraint("role IN ('trader', 'admin')", name="ck_accounts_role"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    kyc_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="NOT_STARTED",
        server_default="NOT_STARTED",
    )
    starting_capital: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, server_default="1000000.00")
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, server_default="1000000.00")
    margin_multiplier: Mapped[Decimal] = mapped_column(Numeric(3, 1), nullable=False, server_default="1.0")
    margin_used: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __table_args__ = __table_args__ + (
        CheckConstraint(
            "kyc_status IN ('NOT_STARTED','PENDING_REVIEW','APPROVED','REJECTED')",
            name="ck_accounts_kyc_status",
        ),
    )

    kyc_submissions: Mapped[list["KYCSubmission"]] = relationship(
        back_populates="account", foreign_keys="KYCSubmission.account_id"
    )
    orders: Mapped[list["Order"]] = relationship(back_populates="account")
    positions: Mapped[list["Position"]] = relationship(back_populates="account")
    cash_ledger_entries: Mapped[list["CashLedger"]] = relationship(back_populates="account")
    strategies: Mapped[list["Strategy"]] = relationship(back_populates="account")
    behavioral_scores: Mapped[list["BehavioralScore"]] = relationship(back_populates="account")
    behavioral_history: Mapped[Optional["TraderBehavioralHistory"]] = relationship(back_populates="account")


# ---------------------------------------------------------------------------
# 2. KYCSubmission
# ---------------------------------------------------------------------------
class KYCSubmission(Base):
    __tablename__ = "kyc_submissions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    id_type: Mapped[str] = mapped_column(String(30), nullable=False)
    document_path: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_full_name: Mapped[Optional[str]] = mapped_column(Text)
    extracted_dob: Mapped[Optional[date]] = mapped_column(Date)
    extracted_id_number: Mapped[Optional[str]] = mapped_column(Text)
    extracted_expiry_date: Mapped[Optional[date]] = mapped_column(Date)
    extracted_issuing_country: Mapped[Optional[str]] = mapped_column(String(3))
    extraction_confidence: Mapped[Optional[str]] = mapped_column(String(10))
    auto_check_passed: Mapped[Optional[bool]] = mapped_column()
    auto_check_notes: Mapped[Optional[dict]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING_REVIEW", server_default="PENDING_REVIEW")
    reviewed_by_admin_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"))
    review_notes: Mapped[Optional[str]] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_kyc_account_status", "account_id", "status"),)

    account: Mapped["Account"] = relationship(back_populates="kyc_submissions", foreign_keys=[account_id])
    reviewed_by: Mapped[Optional["Account"]] = relationship(foreign_keys=[reviewed_by_admin_id])


# ---------------------------------------------------------------------------
# 3. Order
# ---------------------------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_orders_side"),
        CheckConstraint("\"type\" IN ('MARKET', 'LIMIT', 'SL', 'SL-M')", name="ck_orders_type"),
        CheckConstraint("product_type IN ('CNC', 'MIS')", name="ck_orders_product_type"),
        CheckConstraint("qty > 0", name="ck_orders_qty_positive"),
        CheckConstraint(
            "status IN ('NEW','VALIDATED','ROUTED','FILLED','REJECTED','CANCELLED')",
            name="ck_orders_status",
        ),
        Index("idx_orders_account_status", "account_id", "status"),
        Index("idx_orders_backtest", "is_backtest"),
        Index(
            "idx_orders_ticker_matching",
            "ticker",
            "side",
            "type",
            "status",
            postgresql_where=text("status IN ('VALIDATED', 'ROUTED')"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    order_type: Mapped[str] = mapped_column("type", String(6), nullable=False)
    product_type: Mapped[str] = mapped_column(String(3), nullable=False, default="CNC", server_default="CNC")
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    limit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    stop_trigger_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    stop_limit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="NEW", server_default="NEW")
    is_backtest: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    account: Mapped["Account"] = relationship(back_populates="orders")
    events: Mapped[list["OrderEvent"]] = relationship(back_populates="order")
    matches: Mapped[list["OrderMatch"]] = relationship(back_populates="order")
    fills: Mapped[list["Fill"]] = relationship(back_populates="order")
    behavioral_scores: Mapped[list["BehavioralScore"]] = relationship(back_populates="order")


# ---------------------------------------------------------------------------
# 4. OrderEvent
# ---------------------------------------------------------------------------
class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (Index("idx_order_events_order", "order_id", "timestamp"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    from_state: Mapped[Optional[str]] = mapped_column(String(20))
    to_state: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    is_backtest: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    order: Mapped["Order"] = relationship(back_populates="events")


# ---------------------------------------------------------------------------
# 5. OrderMatch
# ---------------------------------------------------------------------------
class OrderMatch(Base):
    __tablename__ = "order_matches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    match_type: Mapped[str] = mapped_column(String(20), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    fill_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    counterparty_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id")
    )
    nse_match_algorithm: Mapped[Optional[str]] = mapped_column(
        String(50), default="NSE_FIFO_PriceTime_v1", server_default="NSE_FIFO_PriceTime_v1"
    )
    matching_log: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    order: Mapped["Order"] = relationship(back_populates="matches")
    counterparty: Mapped[Optional["Account"]] = relationship(foreign_keys=[counterparty_account_id])


# ---------------------------------------------------------------------------
# 6. Fill
# ---------------------------------------------------------------------------
class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("idx_fills_order", "order_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    fill_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    fees: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, server_default="1.00")
    is_backtest: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    reason: Mapped[Optional[str]] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    order: Mapped["Order"] = relationship(back_populates="fills")


# ---------------------------------------------------------------------------
# 7. Position
# ---------------------------------------------------------------------------
class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("account_id", "ticker", "is_backtest", "is_intraday", name="uq_positions_account_ticker"),
        Index("idx_positions_account", "account_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    signed_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, server_default="0")
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, server_default="0")
    is_backtest: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    is_intraday: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    created_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("CURRENT_DATE"))

    account: Mapped["Account"] = relationship(back_populates="positions")


# ---------------------------------------------------------------------------
# 8. CashLedger
# ---------------------------------------------------------------------------
class CashLedger(Base):
    __tablename__ = "cash_ledger"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    is_backtest: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    account: Mapped["Account"] = relationship(back_populates="cash_ledger_entries")


# ---------------------------------------------------------------------------
# 9. PriceHistoryDaily
# ---------------------------------------------------------------------------
class PriceHistoryDaily(Base):
    __tablename__ = "price_history_daily"

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    adj_close: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    volume: Mapped[int] = mapped_column(nullable=False)
    dividend_amount: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, server_default="0")
    split_coefficient: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, server_default="1.0")


# ---------------------------------------------------------------------------
# 10. PriceHistoryMinute
# ---------------------------------------------------------------------------
# NOTE: Part 3 of the build plan partitions this table BY LIST (ticker), with
# one partition per ticker (price_history_minute_aapl, ..._goog, etc.). That
# physical partitioning is applied via raw DDL in the Alembic migration, not
# expressed through separate ORM classes -- there is exactly one mapped class
# for the logical table.
class PriceHistoryMinute(Base):
    __tablename__ = "price_history_minute"
    __table_args__ = {"postgresql_partition_by": "LIST (ticker)"}

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    volume: Mapped[int] = mapped_column(nullable=False)


# ---------------------------------------------------------------------------
# 11. NewsSentimentDaily
# ---------------------------------------------------------------------------
class NewsSentimentDaily(Base):
    __tablename__ = "news_sentiment_daily"

    ticker: Mapped[str] = mapped_column(String(10), primary_key=True)
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    avg_sentiment: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    headline_count: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------------------
# 12. Strategy
# ---------------------------------------------------------------------------
class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(10), nullable=False)
    entry_rule: Mapped[str] = mapped_column(Text, nullable=False)
    exit_rule: Mapped[str] = mapped_column(Text, nullable=False)
    position_size: Mapped[int] = mapped_column(Integer, nullable=False)

    account: Mapped["Account"] = relationship(back_populates="strategies")
    backtest_runs: Mapped[list["BacktestRun"]] = relationship(back_populates="strategy")


# ---------------------------------------------------------------------------
# 13. BacktestRun
# ---------------------------------------------------------------------------
class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)
    total_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4))
    benchmark_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    strategy: Mapped["Strategy"] = relationship(back_populates="backtest_runs")


# ---------------------------------------------------------------------------
# 14. BehavioralScore  (Module BG)
# ---------------------------------------------------------------------------
class BehavioralScore(Base):
    __tablename__ = "behavioral_scores"
    __table_args__ = (
        CheckConstraint("brs BETWEEN 0 AND 100", name="ck_behavioral_scores_brs_range"),
        Index("idx_behavioral_account", "account_id", "calculated_at"),
        Index("idx_behavioral_high_risk", "brs", postgresql_where=text("brs > 50")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    brs: Mapped[int] = mapped_column(Integer, nullable=False)
    biases_detected: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    intervention_shown: Mapped[bool] = mapped_column(nullable=False, default=False, server_default=text("false"))
    intervention_text: Mapped[Optional[str]] = mapped_column(Text)
    trader_proceeded: Mapped[Optional[bool]] = mapped_column()
    trader_acknowledged: Mapped[Optional[bool]] = mapped_column()
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    order: Mapped[Optional["Order"]] = relationship(back_populates="behavioral_scores")
    account: Mapped["Account"] = relationship(back_populates="behavioral_scores")


# ---------------------------------------------------------------------------
# 15. TraderBehavioralHistory  (Module BG)
# ---------------------------------------------------------------------------
class TraderBehavioralHistory(Base):
    __tablename__ = "trader_behavioral_history"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), primary_key=True
    )
    recent_trades: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    today_order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    today_pnl: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, server_default="0")
    session_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_loss_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_loss_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    streak_type: Mapped[Optional[str]] = mapped_column(String(4))
    streak_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    avg_order_size: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    account: Mapped["Account"] = relationship(back_populates="behavioral_history")
