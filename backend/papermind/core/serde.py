"""JSON-safe conversion: Decimal -> str (exact), datetime -> ISO-8601 UTC, enums -> value."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, float):
        return obj
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, BaseModel):
        return jsonable(obj.model_dump())
    if is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set):
        return [jsonable(v) for v in obj]
    return str(obj)


def jdict(obj: Any) -> dict[str, Any]:
    out = jsonable(obj)
    if not isinstance(out, dict):
        raise TypeError("expected an object")
    return out


def jlist(obj: Any) -> list[Any]:
    out = jsonable(obj)
    if not isinstance(out, list):
        raise TypeError("expected a list")
    return out
