"""Decimal helpers. Money and prices are always Decimal, never float."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, InvalidOperation

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
PCT = Decimal("100")


def D(value: object) -> Decimal:
    """Convert a value to Decimal safely (floats go through str to avoid binary noise)."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("cannot convert None to Decimal")
    if isinstance(value, float):
        value = repr(value)
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal: {value!r}") from exc


def D_or_none(value: object) -> Decimal | None:
    return None if value is None else D(value)


def round_to_step(value: Decimal, step: Decimal, mode: str = "nearest") -> Decimal:
    """Round value to a multiple of step. mode: nearest | up | down."""
    if step <= 0:
        return value
    rounding = {"nearest": ROUND_HALF_EVEN, "up": ROUND_CEILING, "down": ROUND_FLOOR}[mode]
    units = (value / step).quantize(ONE, rounding=rounding)
    return (units * step).normalize() if units else ZERO


def money(value: Decimal, places: int = 8) -> Decimal:
    """Quantize a money amount for storage/display (banker's rounding)."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
