"""Channel-neutral message shapes. The orchestrator only ever sees these."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from pocket.data.db import utcnow

ChannelName = Literal["whatsapp", "discord", "telegram", "cli"]


class MediaAttachment(BaseModel):
    url: str
    content_type: str | None = None
    # some providers (twilio) need auth to fetch media, adapters fill this in
    auth: tuple[str, str] | None = None


class Location(BaseModel):
    lat: float
    lon: float
    label: str | None = None


class InboundMessage(BaseModel):
    channel: ChannelName
    channel_msg_id: str  # idempotency key
    user_channel_id: str  # e.g. "whatsapp:+3725xxxxxxx"
    text: str | None = None
    media: list[MediaAttachment] = Field(default_factory=list)
    location: Location | None = None
    received_at: datetime = Field(default_factory=utcnow)
    # adapter-private data needed to answer (e.g. discord interaction token)
    meta: dict[str, Any] = Field(default_factory=dict)


class Option(BaseModel):
    """A semantic quick reply. `value` is what gets fed back into the pipeline
    when the user taps it; `label` is what the button says."""

    value: str
    label: str


class OutboundMessage(BaseModel):
    text: str
    options: list[Option] = Field(default_factory=list)
    # "buttons" for yes/no style, "list" for pick-one-of-many
    options_style: Literal["buttons", "list"] = "buttons"
    reply_to: str | None = None
    attachment_url: str | None = None


class ChannelAdapter(Protocol):
    name: str

    async def send(
        self, user_channel_id: str, message: OutboundMessage, meta: dict[str, Any] | None = None
    ) -> None: ...
