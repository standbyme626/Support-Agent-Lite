"""ConversationService: resolve a (channel, conversation_id) to a Conversation.

Purpose comes from configured conversations (seed/config or runtime
registration). Unknown conversations default to REQUESTER with a type
inferred from channel metadata (feishu chat_type / explicit marker);
operator and approval conversations MUST be registered explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.domain.conversation import Conversation, ConversationPurpose, ConversationType
from app.infrastructure.repositories import ConversationRepository


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex[:10]}"


class ConversationService:
    def __init__(self, repo: ConversationRepository, seed_dir: str | Path | None = None) -> None:
        self._repo = repo
        if seed_dir is not None:
            self._load_seed(seed_dir)

    def _load_seed(self, seed_dir: str | Path) -> None:
        path = Path(seed_dir)
        if not path.exists():
            return
        for file in sorted(path.glob("conversations.json")):
            payload = json.loads(file.read_text(encoding="utf-8"))
            for raw in payload:
                try:
                    self.register(
                        channel=str(raw["channel"]),
                        channel_conversation_id=str(raw["channel_conversation_id"]),
                        conversation_type=ConversationType(raw.get("conversation_type", "GROUP")),
                        purpose=ConversationPurpose(raw["purpose"]),
                        queue=raw.get("queue"),
                        location=raw.get("location"),
                        enabled=bool(raw.get("enabled", True)),
                    )
                except KeyError:
                    continue

    def register(
        self,
        *,
        channel: str,
        channel_conversation_id: str,
        conversation_type: ConversationType,
        purpose: ConversationPurpose,
        queue: str | None = None,
        location: str | None = None,
        enabled: bool = True,
    ) -> Conversation:
        existing = self._repo.find(channel, channel_conversation_id)
        if existing is not None:
            return existing
        return self._repo.create(
            Conversation(
                id=new_id("conv_"),
                channel=channel,
                channel_conversation_id=channel_conversation_id,
                conversation_type=conversation_type,
                purpose=purpose,
                queue=queue,
                location=location,
                enabled=enabled,
            )
        )

    def resolve(
        self,
        channel: str,
        channel_conversation_id: str,
        *,
        hint_type: ConversationType | None = None,
        hint_purpose: ConversationPurpose | None = None,
    ) -> Conversation:
        existing = self._repo.find(channel, channel_conversation_id)
        if existing is not None:
            return existing
        purpose = hint_purpose or ConversationPurpose.REQUESTER
        return self._repo.create(
            Conversation(
                id=new_id("conv_"),
                channel=channel,
                channel_conversation_id=channel_conversation_id,
                conversation_type=hint_type or ConversationType.DM,
                purpose=purpose,
            )
        )

    def operator_conversation(self, queue: str | None) -> Conversation | None:
        return self._repo.find_operator_conversation(queue)

    def list_all(self) -> list[Conversation]:
        return self._repo.list_all()
