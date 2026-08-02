from decimal import Decimal
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Local dev default: this file lives at backend/app/core/config.py, three
# levels under the repo root, and data/ is a sibling of backend/. In Docker
# this is overridden to /app/data via docker-compose.yml's volume mount and
# DATA_DIR env var, since the image only contains backend/'s contents (the
# repo-root-relative computation below wouldn't resolve correctly there).
_REPO_ROOT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Environment ---
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    # localhost:5173 (Vite dev server), localhost:3000 (fallback/CRA-style
    # dev), localhost (docker-compose nginx on port 80)
    ALLOWED_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000", "http://localhost"]

    # --- Simulation data (Module G data loaders) ---
    DATA_DIR: str = str(_REPO_ROOT_DATA_DIR)

    # --- Database / Cache ---
    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379"

    # --- Auth ---
    SECRET_KEY: str = "dev-only-insecure-secret-change-me"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 hours

    # --- Admin bootstrap (seed.py creates this account on first run) ---
    ADMIN_BOOTSTRAP_USERNAME: str = "admin"
    ADMIN_BOOTSTRAP_PASSWORD: str = "changeme"

    # --- GenAI (Anthropic) ---
    ANTHROPIC_API_KEY: str = ""
    GENAI_MODEL: str = "claude-sonnet-4-6"
    GENAI_TIMEOUT_SECONDS: int = 15
    GENAI_MAX_TOKENS_BEHAVIORAL: int = 400

    # --- Ticker universe (Module G data loaders) ---
    TICKERS: list[str] = ["AAPL", "GOOG", "IBM", "MSFT", "TSLA", "UL", "WMT"]

    # --- Account defaults ---
    STARTING_CAPITAL: Decimal = Decimal("1000000.00")
    COMMISSION_FLAT_FEE: Decimal = Decimal("1.00")

    # --- Order validation chain (Module A, Sprint 3) ---
    # PLACEHOLDER VALUES: the build-plan docs reference an exhaustive
    # constants table ("MASTER_BUILD_PLAN Part 3, Section 6") that was not
    # present in any of the provided planning documents. These are
    # reasonable defaults for a demo platform — confirm/tune them before
    # Sprint 3 wires up the 12-check validation chain.
    PRICE_COLLAR_PCT: Decimal = Decimal("0.10")  # order rejected if >10% from last price
    MAX_NOTIONAL_PER_ORDER: Decimal = Decimal("500000.00")
    MAX_CONCENTRATION_PCT: Decimal = Decimal("0.30")  # max 30% of net worth in one ticker
    ORDER_RATE_LIMIT_PER_MINUTE: int = 10
    WASH_TRADE_WINDOW_SECONDS: int = 60

    # --- MIS (intraday) ---
    MIS_MARGIN_MULTIPLIER: Decimal = Decimal("1.5")  # 150% margin required, per FRONTEND_DESIGN_GUIDE
    SHORT_COLLATERAL_MULTIPLIER: Decimal = Decimal("1.5")
    MIS_SQUARE_OFF_TIME_UTC: str = "16:00"  # explicit in MASTER_BUILD_PLAN (EOD auto square-off)

    # --- Simulated market session (UTC) ---
    MARKET_OPEN_TIME_UTC: str = "09:30"  # feed simulator start time, per MASTER_BUILD_PLAN Sprint 4
    MARKET_CLOSE_TIME_UTC: str = "16:00"

    # --- Behavioral Guardrail (Module BG) risk bands, MASTER_BUILD_PLAN Part 2.2 ---
    BRS_CLEAR_MAX: int = 25
    BRS_CAUTION_MAX: int = 50
    BRS_WARNING_MAX: int = 75

    # --- KYC (Module H, Sprint 2) ---
    KYC_UPLOAD_DIR: str = "uploads"  # relative to backend/ (see .gitignore: backend/uploads/*)
    KYC_MAX_FILE_SIZE_MB: int = 10
    KYC_ALLOWED_MIME_TYPES: list[str] = ["application/pdf", "image/jpeg", "image/png"]
    KYC_MIN_AGE_YEARS: int = 18
    KYC_NAME_MATCH_MIN_SCORE: int = 80  # fuzzywuzzy ratio (0-100)

    # --- NSE matching + feed simulator (Sprint 4) ---
    # PLACEHOLDER: MARKET orders fill against a synthetic bid/ask around the
    # tick's close price when there's no real resting counterparty to match
    # against (per Sprint 3's "fills at synthetic ask"). No spread value is
    # specified anywhere in the planning docs.
    SYNTHETIC_SPREAD_BPS: Decimal = Decimal("5")  # 0.05%

    # Real seconds per simulated minute at 1x feed speed; speed_multiplier
    # (see FeedState) divides this, so 10x = 1/10th the interval, etc. Not
    # specified in the docs beyond "configurable speed" -- 1 real second
    # per simulated minute means the full Jun 30-Aug 29 dataset (~119k
    # simulated minutes) replays in ~33 hours at 1x, ~20 minutes at 100x.
    FEED_BASE_TICK_INTERVAL_SECONDS: float = 1.0
    FEED_DEFAULT_SPEED_MULTIPLIER: int = 1

    # Not in the schema anywhere -- no ticker-to-sector mapping exists.
    # Real-world GICS sector classifications for the 7-ticker universe.
    SECTOR_BY_TICKER: dict[str, str] = {
        "AAPL": "Information Technology",
        "MSFT": "Information Technology",
        "IBM": "Information Technology",
        "GOOG": "Communication Services",
        "TSLA": "Consumer Discretionary",
        "WMT": "Consumer Staples",
        "UL": "Consumer Staples",
    }


settings = Settings()
