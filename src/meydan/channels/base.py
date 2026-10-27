"""Channel-neutral message types shared by adapters and the runtime."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class Address:
    """An identity on a channel: immutable, never reassigned. Names and usernames are not addresses.

    scheme: tel | wa-bsuid | wa-pnid | mailto | agent | ... (declared by the connector)
    value:  normalized (E.164 with "+", lowercased email, the channel's opaque id)
    scope:  namespace where the channel scopes ids (e.g. a business portfolio); "" if global
    """

    scheme: str
    value: str
    scope: str = ""

    def __str__(self) -> str:
        return (
            f"{self.scheme}:{self.scope}:{self.value}"
            if self.scope
            else f"{self.scheme}:{self.value}"
        )


@dataclass(frozen=True)
class Inbound:
    """One message someone sent to one of our endpoints."""

    channel: str  # e.g. "whatsapp"
    message_id: str  # channel's id (WhatsApp: wamid); unique per channel
    to: Address  # our endpoint (WhatsApp: wa-pnid, the phone_number_id written to)
    sender: Address  # the address to answer (WhatsApp: tel when shared, else wa-bsuid)
    sent_at: datetime  # when they sent it (channel timestamp, aware)
    kind: str  # text | button | interactive | image | ... (channel's type)
    text: str  # best-effort text: body, button title, caption; "" if none
    payload: dict[str, Any] = field(default_factory=dict)  # the raw message object
    sender_name: str | None = None  # profile name, if the channel shares it
    # Other addresses the channel asserts belong to the same sender in this very message
    # (WhatsApp: the BSUID next to the phone number). Evidence for `channel_cooccurrence`.
    also: tuple[Address, ...] = ()
    attrs: dict[str, Any] = field(default_factory=dict)  # e.g. username; stored on the identity


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
