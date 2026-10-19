"""Durable conversation and message operations over EventStore."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .event_store import EventStore
from .models import Conversation, ConversationTurn, Message


class ConversationService:
    """A thin service boundary around the conversation persistence methods."""

    def __init__(
        self,
        project_root: Path,
        *,
        store: EventStore | None = None,
    ) -> None:
        if store is None:
            store = EventStore(
                project_root.resolve() / "data" / "zevorinth.sqlite3"
            )
            store.initialize()
        self.store = store

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        now: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Conversation:
        return self.store.create_conversation(
            conversation_id=conversation_id,
            now=now,
            metadata=metadata,
        )

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.store.get_conversation(conversation_id)

    def append_user_message(
        self,
        conversation_id: str,
        content: str,
        *,
        client_message_id: str | None = None,
        now: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[ConversationTurn, Message]:
        return self.store.append_user_message(
            conversation_id,
            content,
            client_message_id=client_message_id,
            now=now,
            metadata=metadata,
        )

    def history(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[Message]:
        return self.store.list_conversation_messages(
            conversation_id,
            limit=limit,
        )
