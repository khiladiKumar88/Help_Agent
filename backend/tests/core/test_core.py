from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pytest
from pydantic import ValidationError

from papermind.core.clock import RealClock, ReplayClock, ensure_utc
from papermind.core.config import AppConfig, BookConfig, Settings, load_config
from papermind.core.events import EventBus, Topic
from papermind.core.logging import REDACTED, JsonFormatter, RedactingFilter, Redactor, setup_logging
from papermind.core.money import D, D_or_none, money, round_to_step
from papermind.core.serde import jdict, jlist, jsonable
from papermind.core.types import ChargeBreakdown, Direction, Side, Tick, TrailingConfig, trading_date
from tests.conftest import BTC, T0, book_dict, make_cfg

REPO_CONFIG = Path(__file__).resolve().parents[3] / "config"


# ------------------------------------------------------------------ money
def test_decimal_helpers() -> None:
    assert D(0.1) == Decimal("0.1")
    assert D("1.5") == Decimal("1.5") and D(Decimal("2")) == Decimal("2")
    assert D_or_none(None) is None
    with pytest.raises(ValueError):
        D(None)
    with pytest.raises(ValueError):
        D("abc")
    assert round_to_step(Decimal("65013.04"), Decimal("0.1"), "up") == Decimal("65013.1")
    assert round_to_step(Decimal("65013.09"), Decimal("0.1"), "down") == Decimal("65013")
    assert round_to_step(Decimal("0.0004"), Decimal("0.001"), "down") == 0
    assert round_to_step(Decimal("5"), Decimal("0")) == Decimal("5")
    assert money(Decimal("0.123456789")) == Decimal("0.12345679")


# ------------------------------------------------------------------ clock
def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError):
        ensure_utc(datetime(2026, 1, 1))


async def test_replay_clock_advances_and_never_goes_back() -> None:
    c = ReplayClock(T0)
    assert c.is_replay and c.now() == T0
    c.advance(30)
    assert c.now() == T0 + timedelta(seconds=30)
    with pytest.raises(ValueError):
        c.set(T0)


async def test_replay_clock_sleep_wakes_only_when_time_reached() -> None:
    c = ReplayClock(T0)
    woke: list[datetime] = []

    async def sleeper() -> None:
        await c.sleep(10)
        woke.append(c.now())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    c.advance(5)
    await asyncio.sleep(0)
    assert not woke
    c.advance(5)
    await asyncio.sleep(0)
    await task
    assert woke == [T0 + timedelta(seconds=10)]
    await c.sleep(0)  # zero sleep returns immediately


async def test_real_clock() -> None:
    c = RealClock()
    assert c.now().tzinfo is UTC and not c.is_replay
    await c.sleep(0)


# ------------------------------------------------------------------ events
async def test_event_bus_isolates_failing_handlers() -> None:
    bus = EventBus()
    got: list[int] = []

    def bad(_: object) -> None:
        raise RuntimeError("boom")

    async def good(p: int) -> None:
        got.append(p)

    bus.subscribe(Topic.TICK, bad)
    unsub = bus.subscribe(Topic.TICK, good)
    await bus.publish(Topic.TICK, 1)
    unsub()
    unsub()
    await bus.publish(Topic.TICK, 2)
    assert got == [1]


# ------------------------------------------------------------------ config
def test_repo_config_files_load_and_validate() -> None:
    cfg = load_config(
        Settings(
            papermind_config_dir=REPO_CONFIG,
            papermind_db_url="sqlite://",
            papermind_crypto_provider="simulated",
            _env_file=None,
        )
    )  # type: ignore[call-arg]
    assert cfg.db_url == "sqlite://"
    assert cfg.data.crypto.provider == "simulated"
    book = cfg.book("crypto-futures-intraday")
    assert book.currency == "USDT" and book.risk.max_leverage == Decimal("3")
    for ch in cfg.charges.values():
        assert ch.source_url.startswith("https://")
        assert ch.verified is False  # must be verified by a human before being marked true
    with pytest.raises(KeyError):
        cfg.book("nope")


def test_book_id_must_match_market_segment_style() -> None:
    with pytest.raises(ValidationError, match="book id must be"):
        BookConfig.model_validate({**book_dict(), "id": "wrong"})


def test_unknown_config_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        BookConfig.model_validate({**book_dict(), "typo_field": 1})


def test_book_referencing_missing_charges_fails(tmp_path: Path) -> None:
    (tmp_path / "books").mkdir()
    (tmp_path / "charges").mkdir()
    (tmp_path / "books" / "b.yaml").write_text(json.dumps({**book_dict(), "charges": "missing"}))
    with pytest.raises(ValueError, match="charges 'missing' not found"):
        load_config(Settings(papermind_config_dir=tmp_path, _env_file=None))  # type: ignore[call-arg]


def test_holidays_file_merged(tmp_path: Path) -> None:
    for d in ("books", "charges", "calendars"):
        (tmp_path / d).mkdir()
    (tmp_path / "calendars" / "h.yaml").write_text("holidays: [2026-01-26]\n")
    (tmp_path / "charges" / "crypto_test.yaml").write_text(
        "kind: crypto\nsource_url: https://x\nas_of: 2026-01-01\nmaker_pct: 0.02\ntaker_pct: 0.05\n"
    )
    b = {**book_dict(), "session": {"entry_start": "09:20", "entry_end": "15:00", "holidays_file": "calendars/h.yaml"}}
    (tmp_path / "books" / "b.yaml").write_text(json.dumps(b))
    cfg = load_config(Settings(papermind_config_dir=tmp_path, _env_file=None))  # type: ignore[call-arg]
    session = cfg.book("crypto-futures-intraday").session
    assert session is not None and date(2026, 1, 26) in session.holidays


def test_settings_secret_status_never_exposes_values() -> None:
    s = Settings(gemini_api_key="sk-supersecret", angel_pin="1234", _env_file=None)  # type: ignore[call-arg]
    st = s.secrets_status()
    assert st["gemini_api_key"] is True and st["angel_api_key"] is False
    assert "sk-supersecret" not in json.dumps(st)
    assert "sk-supersecret" in s.secret_values()


def test_app_config_defaults() -> None:
    cfg = AppConfig()
    assert cfg.data.stale_after_seconds == 10
    assert make_cfg().charges["crypto_test"].kind == "crypto"


# ------------------------------------------------------------------ logging
def test_redactor_masks_known_secrets_and_patterns() -> None:
    r = Redactor(["sk-supersecret", "abc"])  # too-short secrets are ignored
    out = r.redact("key sk-supersecret api_key=XYZ123 Authorization: Bearer tok.en abc")
    assert "sk-supersecret" not in out and "XYZ123" not in out and "tok.en" not in out
    assert out.endswith("abc")
    obj = r.redact_obj(
        {"apiKey": "v", "access_token": "t", "nested": ["sk-supersecret"], "n": 1, "tokens_used": 5, "pinned": True}
    )
    assert obj == {
        "apiKey": REDACTED,
        "access_token": REDACTED,
        "nested": [REDACTED],
        "n": 1,
        "tokens_used": 5,
        "pinned": True,
    }


def test_logging_pipeline_redacts(capsys: pytest.CaptureFixture[str]) -> None:
    setup_logging("INFO", ["topsecretvalue"])
    logging.getLogger("t").info("login with topsecretvalue", extra={"token": "zzz", "ok": 1})
    line = capsys.readouterr().out.strip().splitlines()[-1]
    rec = json.loads(line)
    assert "topsecretvalue" not in line and rec["token"] == REDACTED and rec["ok"] == 1
    try:
        raise ValueError("password=hunter2")
    except ValueError:
        logging.getLogger("t").exception("failed")
    assert "hunter2" not in capsys.readouterr().out


def test_json_formatter_direct() -> None:
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "hello %s", ("w",), None)
    RedactingFilter().filter(rec)
    assert json.loads(JsonFormatter().format(rec))["msg"] == "hello w"


# ------------------------------------------------------------------ types / serde
def test_tick_validation_and_mid() -> None:
    with pytest.raises(ValidationError):
        Tick(instrument_id=BTC, ts=T0, ltp=Decimal("0"))
    assert Tick(instrument_id=BTC, ts=T0, ltp=Decimal("10"), bid=Decimal("9"), ask=Decimal("11")).mid == 10
    assert Tick(instrument_id=BTC, ts=T0, ltp=Decimal("10")).mid == 10


def test_enums_and_helpers() -> None:
    assert Direction.LONG.entry_side is Side.BUY and Direction.SHORT.exit_side is Side.BUY
    assert Side.BUY.opposite is Side.SELL and Direction.SHORT.sign == -1
    assert trading_date(datetime(2026, 1, 5, 20, 0, tzinfo=UTC), "Asia/Kolkata") == date(2026, 1, 6)
    with pytest.raises(ValidationError):
        TrailingConfig(pct=Decimal("150"))
    assert ChargeBreakdown(brokerage=Decimal("1"), gst=Decimal("0.18")).as_dict()["total"] == "1.18"


def test_jsonable() -> None:
    class E(Enum):
        A = "a"

    out = jsonable(
        {
            "d": Decimal("1.50"),
            "t": T0,
            "e": E.A,
            "l": (1, 2.5, None),
            "o": object,
            "day": date(2026, 1, 1),
            "m": TrailingConfig(distance=Decimal("5")),
        }
    )
    assert out["d"] == "1.50" and out["t"].startswith("2026-01-05T10:00") and out["e"] == "a"
    assert out["l"] == [1, 2.5, None] and out["day"] == "2026-01-01" and out["m"]["distance"] == "5"
    assert jdict({"a": 1}) == {"a": 1} and jlist([1]) == [1]
    with pytest.raises(TypeError):
        jdict([1])
    with pytest.raises(TypeError):
        jlist({"a": 1})
