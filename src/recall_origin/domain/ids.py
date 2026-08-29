"""Opaque identifier and time helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

IdFactory = Callable[[str], str]
Clock = Callable[[], datetime]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_unix_micros(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return int(value.timestamp() * 1_000_000)


def from_unix_micros(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC)
