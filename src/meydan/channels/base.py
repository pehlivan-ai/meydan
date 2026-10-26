"""Channel-neutral message types shared by adapters and the runtime."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class Inbound:
    """One message a person sent to one of our numbers."""

    channel: str  # e.g. "whatsapp"
    message_id: str  # channel's id (WhatsApp: wamid); unique per channel
    to: str  # our number/account id the person wrote to (WhatsApp: phone_number_id)
    sender: str  # the person's channel id (WhatsApp: wa_id, digits only)
    sent_at: datetime  # when the person sent it (channel timestamp, aware)
    kind: str  # text | button | interactive | image | ... (channel's type)
    text: str  # best-effort text: body, button title, caption; "" if none
    payload: dict[str, Any] = field(default_factory=dict)  # the raw message object
    sender_name: str | None = None  # profile name, if the channel shares it

    @property
    def conversation_key(self) -> str:
        """One conversation = (channel, our number, person). See docs/architecture.md, n:n."""
        return f"{self.channel}:{self.to}:{self.sender}"


@dataclass(frozen=True)
class StatusUpdate:
    """The fate of a message we sent, reported asynchronously by the channel."""

    channel: str
    message_id: str  # id the channel returned when we sent it
    status: Literal["sent", "delivered", "read", "failed"]
    at: datetime
    error: str | None = None


@dataclass(frozen=True)
class Outbound:
    """Something to send in a conversation: free-form text or an approved template."""

    kind: Literal["text", "template"]
    text: str = ""
    template: str | None = None
    language: str | None = None
    params: dict[str, str] = field(default_factory=dict)  # NAMED template parameters

    @classmethod
    def text_message(cls, text: str) -> "Outbound":
        return cls(kind="text", text=text)

    @classmethod
    def template_message(cls, name: str, language: str, **params: str) -> "Outbound":
        return cls(kind="template", template=name, language=language, params=params)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "template": self.template,
            "language": self.language,
            "params": self.params,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Outbound":
        return cls(**data)


class SendError(Exception):
    """A send that failed. `retryable` decides between backoff and dead letter."""

    def __init__(
        self, reason: str, *, retryable: bool, code: int | None = None, min_wait_s: float = 0
    ):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.code = code
        self.min_wait_s = min_wait_s  # channel-specific floor for the next attempt
