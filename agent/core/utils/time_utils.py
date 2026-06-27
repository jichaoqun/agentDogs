"""Shared time helpers for agent runtime metadata and prompts."""

from __future__ import annotations

from datetime import datetime


def now_local() -> datetime:
    """Return the backend host's current timezone-aware local time."""

    return datetime.now().astimezone()


def isoformat(value: datetime | None = None) -> str:
    return (value or now_local()).isoformat()


def current_time_context(value: datetime | None = None) -> str:
    current = value or now_local()
    return f"当前时间：{current.strftime('%Y-%m-%d %H:%M:%S %z')}。"


def parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None
