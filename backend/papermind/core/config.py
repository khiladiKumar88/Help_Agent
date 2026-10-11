"""Configuration: YAML files in config/ for behaviour, .env for secrets.

- config/default.yaml         app, server, db, data feeds
- config/books/*.yaml         one file per book (market x segment x style)
- config/charges/*.yaml       fee schedules (each carries source_url / as_of / verified)
- config/calendars/*.yaml     exchange holiday calendars
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Market(StrEnum):
    INDIA = "india"
    CRYPTO = "crypto"


class Segment(StrEnum):
    SPOT = "spot"
    FUTURES = "futures"
    OPTIONS = "options"


class Style(StrEnum):
    INTRADAY = "intraday"
    SWING = "swing"


class Mode(StrEnum):
    MANUAL = "manual"
    COPILOT = "copilot"
    AUTO = "auto"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- secrets / env


class Settings(BaseSettings):
    """Secrets and deployment overrides. Loaded from environment / .env only."""

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    papermind_config_dir: Path = REPO_ROOT / "config"
    papermind_db_url: str | None = None
    papermind_crypto_provider: str | None = None  # override data.crypto.provider (ccxt|simulated)
    papermind_llm_provider: str | None = None  # override llm.provider (gemini|fake|off)
    papermind_history_db_url: str | None = None
    papermind_vault_dir: Path = REPO_ROOT / "vault"
    log_level: str = "INFO"

    gemini_api_key: SecretStr | None = None
    angel_api_key: SecretStr | None = None
    angel_client_code: SecretStr | None = None
    angel_pin: SecretStr | None = None
    angel_totp_secret: SecretStr | None = None

    def secret_values(self) -> list[str]:
        out: list[str] = []
        for name in self.secret_names():
            val = getattr(self, name)
            if val is not None and val.get_secret_value():
                out.append(val.get_secret_value())
        return out

    @staticmethod
    def secret_names() -> list[str]:
        return ["gemini_api_key", "angel_api_key", "angel_client_code", "angel_pin", "angel_totp_secret"]

    def secrets_status(self) -> dict[str, bool]:
        """Presence only — values are never exposed."""
        return {n: bool(getattr(self, n) and getattr(self, n).get_secret_value()) for n in self.secret_names()}

    def secret(self, name: str) -> str | None:
        """Read one secret's value, or None when it is unset/blank.

        This is the ONLY way components should reach a secret. `.env` is loaded by
        pydantic-settings into this object and is deliberately NOT exported into os.environ, so
        anything reading os.environ directly cannot see a key the user put where the docs say.
        """
        field = getattr(self, name, None)
        if field is None:
            return None
        value = field.get_secret_value()
        return value.strip() or None


# --------------------------------------------------------------------------- book config


class SessionConfig(_Strict):
    """Exchange session. None on a book means 24x7 (crypto)."""

    timezone: str = "Asia/Kolkata"
    market_open: time | None = None  # exchange open (e.g. 09:15); defaults to entry_start
    entry_start: time
    entry_end: time
    square_off: time | None = None
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    holidays: list[date] = Field(default_factory=list)
    holidays_file: str | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class RiskConfig(_Strict):
    """Per-book risk limits. Enforced by pure code in risk/; the LLM cannot touch these."""

    max_risk_per_trade_abs: Decimal | None = None
    max_risk_per_trade_pct: Decimal | None = None  # % of current equity
    max_lots: int | None = None
    max_qty: Decimal | None = None
    max_premium: Decimal | None = None  # options only
    max_open_positions: int = 1
    max_trades_per_day: int | None = None
    daily_loss_limit_abs: Decimal | None = None
    daily_loss_limit_pct: Decimal | None = None  # % of start-of-day equity
    flatten_on_daily_loss: bool = True
    max_leverage: Decimal = Decimal("1")
    require_stop_loss: bool = True

    @field_validator("require_stop_loss")
    @classmethod
    def _sl_always(cls, v: bool) -> bool:
        if not v:
            raise ValueError("require_stop_loss cannot be disabled (R-multiples and risk sizing need it)")
        return v


class InstrumentSlippage(_Strict):
    """Slippage for one symbol, with the measurement it was calibrated against.

    Like the fee files, this carries its provenance: `observed_spread_bps`, `source` and `as_of`
    record what the book actually looked like when the numbers were set, so nobody has to guess
    later whether an assumption is current. Keep the assumption a little WORSE than the
    observation — calm top-of-book is not what a stop-market order meets in a fast move.
    """

    bps: Decimal | None = None
    est_spread_bps: Decimal | None = None
    extra_ticks: int | None = None
    observed_spread_bps: Decimal | None = None  # full top-of-book spread, as measured
    source: str = ""
    as_of: date | None = None
    notes: str = ""


class SlippageConfig(_Strict):
    model: Literal["bps", "spread"] = "bps"
    bps: Decimal = Decimal("2")  # extra adverse slippage on top of the touch price
    est_spread_bps: Decimal = Decimal("2")  # used when bid/ask are unavailable
    extra_ticks: int = 0  # spread model: extra adverse ticks
    # Per-symbol overrides, keyed by exchange symbol (e.g. "BTC/USDT:USDT"). A missing field
    # falls back to the book-wide value above, so an entry can tune one number only.
    per_instrument: dict[str, InstrumentSlippage] = Field(default_factory=dict)

    def for_symbol(self, symbol: str) -> SlippageConfig:
        """The effective config for one symbol. Returns self when nothing is overridden."""
        o = self.per_instrument.get(symbol)
        if o is None:
            return self
        return SlippageConfig(
            model=self.model,
            bps=self.bps if o.bps is None else o.bps,
            est_spread_bps=self.est_spread_bps if o.est_spread_bps is None else o.est_spread_bps,
            extra_ticks=self.extra_ticks if o.extra_ticks is None else o.extra_ticks,
        )

    def per_side_bps(self, symbol: str | None = None) -> Decimal:
        """Total adverse bps a market order pays on ONE side, for the bps model.

        Half the estimated spread to reach the touch price, plus the slippage term. This is the
        number to compare against a measured half-spread.
        """
        eff = self.for_symbol(symbol) if symbol else self
        return eff.est_spread_bps / 2 + eff.bps


class StrategySpec(_Strict):
    id: str
    enabled: bool = True
    params: dict[str, float | int] = Field(default_factory=dict)


class ScannerConfig(_Strict):
    cooldown_bars: int = 3  # min signal-timeframe bars between signals of the same strategy+instrument
    window: int = 250  # closed candles fed to indicators


class BaselineConfig(_Strict):
    """Random-entry shadow trader: same risk rules, same frequency as the agent's taken signals."""

    enabled: bool = True
    seed: int = 7
    max_delay_bars: int = 4  # entry happens 0..N signal bars after the agent's decision (random)


class CopilotConfig(_Strict):
    """Co-pilot signal cards: how long the human has to approve before the card expires."""

    card_ttl_bars: float = 1.0  # expiry = this many signal-timeframe bars after the signal
    card_ttl_seconds: float | None = None  # absolute override; wins over card_ttl_bars when set
    shadow_track_rejected: bool = True  # record what a rejected card would have done


class BookConfig(_Strict):
    id: str
    market: Market
    segment: Segment
    style: Style
    currency: str
    mode: Mode = Mode.MANUAL
    enabled: bool = True
    starting_capital: Decimal
    data_source: str  # key into data config, e.g. "crypto"
    instruments: list[str]  # exchange symbols, resolved through the instrument registry
    charges: str  # key into charges config
    slippage: SlippageConfig = SlippageConfig()
    risk: RiskConfig
    session: SessionConfig | None = None
    trading_day_tz: str = "UTC"
    timeframes: dict[str, str] = Field(default_factory=lambda: {"signal": "15m", "execution": "1m"})
    strategies: list[StrategySpec] = Field(default_factory=list)
    scanner: ScannerConfig = ScannerConfig()
    baseline: BaselineConfig = BaselineConfig()
    copilot: CopilotConfig = CopilotConfig()
    shadow_raw_strategy: bool = True  # also trade EVERY signal in shadow_raw, to measure the LLM's filter

    @model_validator(mode="after")
    def _check_id(self) -> BookConfig:
        expected = f"{self.market}-{self.segment}-{self.style}"
        if self.id != expected:
            raise ValueError(f"book id must be '{expected}', got '{self.id}'")
        return self


# --------------------------------------------------------------------------- charges


class _ChargeMeta(_Strict):
    source_url: str
    as_of: date
    verified: bool = False
    notes: str = ""


class CryptoChargesConfig(_ChargeMeta):
    kind: Literal["crypto"] = "crypto"
    maker_pct: Decimal
    taker_pct: Decimal
    gst_pct: Decimal = Decimal("0")  # GST on fees (Indian exchanges)


class IndiaFnoChargesConfig(_ChargeMeta):
    kind: Literal["india_fno"] = "india_fno"
    brokerage_per_order: Decimal
    brokerage_pct_cap: Decimal | None = None  # "flat fee or x% whichever lower"
    stt_futures_sell_pct: Decimal
    stt_options_sell_pct: Decimal
    exchange_futures_pct: Decimal
    exchange_options_pct: Decimal
    sebi_per_crore: Decimal
    stamp_futures_buy_pct: Decimal
    stamp_options_buy_pct: Decimal
    ipft_pct: Decimal = Decimal("0")
    gst_pct: Decimal = Decimal("18")


ChargesConfig = Annotated[CryptoChargesConfig | IndiaFnoChargesConfig, Field(discriminator="kind")]


# --------------------------------------------------------------------------- data


class SimulatedFeedConfig(_Strict):
    seed: int = 42
    tick_interval_seconds: float = 1.0
    annual_vol: Decimal = Decimal("0.6")
    start_prices: dict[str, Decimal] = Field(default_factory=dict)
    spread_bps: Decimal = Decimal("1")


class CryptoDataConfig(_Strict):
    provider: Literal["ccxt", "simulated"] = "ccxt"
    exchange: str = "binanceusdm"
    feed: Literal["ws", "poll"] = "ws"
    poll_interval_seconds: float = 1.0
    candle_poll_seconds: float = 20.0
    funding_poll_seconds: float = 300.0
    history_candles: int = 500
    # higher timeframes seeded directly at startup so strategies have warm-up history immediately
    seed_timeframes: list[str] = Field(default_factory=lambda: ["15m", "1h"])
    seed_candles: int = 300
    symbols: list[str] = Field(default_factory=list)
    simulated: SimulatedFeedConfig = SimulatedFeedConfig()


class DataConfig(_Strict):
    stale_after_seconds: float = 10.0
    history_db_url: str = "sqlite:///history.db"  # local OHLCV cache for backtests / replays
    crypto: CryptoDataConfig = CryptoDataConfig()


class LLMRateLimits(_Strict):
    """Rate/budget limits for the LLM provider.

    Google does NOT publish the free-tier RPM/TPM/RPD in its docs any more — each account sees
    its own limits in AI Studio — so these are config, never hardcoded guesses. The defaults are
    deliberately conservative; set them to the numbers AI Studio shows for YOUR key.
    """

    requests_per_minute: int = 10  # token-bucket refill rate
    burst: int | None = None  # bucket capacity; defaults to requests_per_minute
    tokens_per_minute: int | None = 250_000  # None = don't track input tokens per minute
    requests_per_day: int = 200  # hard daily call budget; the agent PAUSES when it is used up
    max_retries: int = 3  # retries on 429 / 5xx (exponential backoff)
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 32.0

    @model_validator(mode="after")
    def _check(self) -> LLMRateLimits:
        if self.requests_per_minute <= 0 or self.requests_per_day <= 0:
            raise ValueError("requests_per_minute and requests_per_day must be positive")
        if self.burst is not None and self.burst < 1:
            raise ValueError("burst must be at least 1")
        return self


class LLMConfig(_Strict):
    """The analyst's LLM. `model` and the limits come from here — never hardcoded in code."""

    provider: Literal["gemini", "fake", "off"] = "gemini"
    # Current free-tier Flash model (verified against Google's pricing page, 2026-10-01).
    # Any id the provider accepts works; this is the only place it is named.
    model: str = "gemini-3.8-flash"
    api_key_env: str = "GEMINI_API_KEY"  # read from .env only; never logged, never in a prompt
    timeout_seconds: float = 60.0  # PER ATTEMPT; max_retries multiplies it (see default.yaml)
    temperature: float = 0.2
    thinking_level: Literal["minimal", "low", "medium", "high"] = "low"
    max_output_tokens: int = 1024
    confidence_threshold: int = 70  # below this, the answer is SKIP
    prompt_version: str = "analyst_v1"  # template under analyst/prompts/, stored with each decision
    cache_responses: bool = True  # cache by prompt hash so replays don't burn quota
    log_prompts: bool = True  # store the full prompt with each call record
    limits: LLMRateLimits = LLMRateLimits()


class ServerConfig(_Strict):
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])


class AppConfig(_Strict):
    server: ServerConfig = ServerConfig()
    db_url: str = "sqlite:///papermind.db"
    data: DataConfig = DataConfig()
    equity_snapshot_seconds: int = 60
    books: dict[str, BookConfig] = Field(default_factory=dict)
    charges: dict[str, ChargesConfig] = Field(default_factory=dict)
    llm: LLMConfig = LLMConfig()

    def book(self, book_id: str) -> BookConfig:
        try:
            return self.books[book_id]
        except KeyError as exc:
            raise KeyError(f"unknown book '{book_id}'") from exc


# --------------------------------------------------------------------------- loading


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def _load_holidays(config_dir: Path, session: dict[str, Any]) -> None:
    fname = session.get("holidays_file")
    if fname:
        cal = _read_yaml(config_dir / fname)
        session["holidays"] = [*session.get("holidays", []), *cal.get("holidays", [])]


def load_config(settings: Settings | None = None) -> AppConfig:
    settings = settings or Settings()
    cdir = settings.papermind_config_dir
    raw: dict[str, Any] = _read_yaml(cdir / "default.yaml") if (cdir / "default.yaml").exists() else {}

    charges: dict[str, Any] = {}
    for p in sorted((cdir / "charges").glob("*.yaml")):
        charges[p.stem] = _read_yaml(p)
    books: dict[str, Any] = {}
    for p in sorted((cdir / "books").glob("*.yaml")):
        b = _read_yaml(p)
        if b.get("session"):
            _load_holidays(cdir, b["session"])
        books[b["id"]] = b

    raw["charges"] = charges
    raw["books"] = books
    if settings.papermind_db_url:
        raw["db_url"] = settings.papermind_db_url
    if settings.papermind_history_db_url:
        raw.setdefault("data", {})["history_db_url"] = settings.papermind_history_db_url
    if settings.papermind_crypto_provider:
        raw.setdefault("data", {}).setdefault("crypto", {})["provider"] = settings.papermind_crypto_provider
    if settings.papermind_llm_provider:
        raw.setdefault("llm", {})["provider"] = settings.papermind_llm_provider

    cfg = AppConfig.model_validate(raw)
    for book in cfg.books.values():
        if book.charges not in cfg.charges:
            raise ValueError(f"book {book.id}: charges '{book.charges}' not found in config/charges/")
    return cfg
