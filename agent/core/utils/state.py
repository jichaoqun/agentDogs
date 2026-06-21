"""Shared message type for later persistence/API work."""

from typing import TypedDict


class MessageState(TypedDict):
    role: str
    content: str
