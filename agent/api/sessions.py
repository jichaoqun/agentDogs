from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Callable
from uuid import uuid4

from langchain_core.messages import BaseMessage

from agent.core.main_agent import MainAgent
from agent.core.utils.llm_config import AppConfig
from agent.core.utils.llm_models import ModelManager


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ChatSession:
    id: str
    title: str
    agent: MainAgent
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def messages(self) -> list[BaseMessage]:
        return list(self.agent.history.messages)


class SessionManager:
    def __init__(
        self,
        config: AppConfig,
        agent_factory: Callable[[AppConfig], MainAgent] | None = None,
    ) -> None:
        self.config = config
        self.models = ModelManager(config)
        self.agent_factory = agent_factory or (lambda active_config: MainAgent(active_config, self.models))
        self._sessions: dict[str, ChatSession] = {}
        self._lock = RLock()

    def create(self, title: str = "新会话") -> ChatSession:
        session = ChatSession(str(uuid4()), title.strip() or "新会话", self.agent_factory(self.config))
        with self._lock:
            self._sessions[session.id] = session
        return session

    def list(self) -> list[ChatSession]:
        with self._lock:
            return sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)

    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

    def touch(self, session: ChatSession, first_message: str | None = None) -> None:
        with self._lock:
            if first_message and session.title == "新会话":
                session.title = first_message.strip().replace("\n", " ")[:24] or "新会话"
            session.updated_at = utcnow()
