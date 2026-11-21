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
class Choice:
    """One option of a choices message: `id` comes back when it is picked (the app's own key),
    `title` is what the person sees and taps, `description` an optional line under it."""

    id: str
    title: str
    description: str | None = None


CHOICES_MAX = 10  # the most options one message offers (WhatsApp's list: 10 rows)


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
    # The channel's id of the message of ours this one answers (WhatsApp: a button press or a
    # quoted reply carries `context.id`, the wamid we sent). Links replies to what they answer.
    reply_to: str | None = None


@dataclass(frozen=True)
class StatusUpdate:
    """The fate of a message we sent, reported asynchronously by the channel."""

    channel: str
    message_id: str  # id the channel returned when we sent it
    status: Literal["sent", "delivered", "read", "failed"]
    at: datetime
    error: str | None = None
    # Who received it, as the channel names them (WhatsApp: the phone number and the BSUID).
    # Two addresses here are a channel assertion that they are one person (`corroborate`).
    recipient: tuple[Address, ...] = ()


@dataclass(frozen=True)
class TemplateUpdate:
    """What the channel says about one of our message templates (review, pauses, category,
    quality). Fields the event doesn't speak about are None."""

    channel: str
    account: str  # the business account that owns it (WhatsApp: the WABA id)
    name: str
    language: str
    at: datetime  # when the channel reported it
    external_id: str | None = None  # the channel's template id
    status: str | None = None  # APPROVED | PENDING | REJECTED | PAUSED | DISABLED | ...
    category: str | None = None  # current category
    upcoming_category: str | None = None  # announced change, e.g. to MARKETING in 24 hours
    quality: str | None = None  # GREEN | YELLOW | RED | UNKNOWN
    reason: str | None = None  # why it was rejected or paused, in the channel's words
    # What it says, for showing a sent one in a thread: the body with its placeholders
    # ({{name}} or {{1}}), and the buttons' labels. None: the event doesn't say.
    body: str | None = None
    buttons: tuple[str, ...] | None = None


@dataclass(frozen=True)
class EndpointUpdate:
    """What a channel (or a check) says about one of our own addresses: a WhatsApp number we send
    from, an email address people write to. Fields the source doesn't speak about are None."""

    channel: str
    address: Address  # ours (WhatsApp: wa-pnid, the phone_number_id; email: mailto)
    at: datetime  # when it was reported or checked
    account: str | None = None  # who owns it on the channel (WhatsApp: the WABA id; email: domain)
    display: str | None = None  # how people see it: "+90 555 ...", "hello@example.com"
    name: str | None = None  # the name shown with it (WhatsApp: the verified display name)
    status: str | None = None  # the channel's words (WhatsApp: CONNECTED, FLAGGED, BANNED, ...)
    quality: str | None = None  # GREEN | YELLOW | RED | UNKNOWN
    limit: str | None = None  # messaging limit (WhatsApp: TIER_250, TIER_1K, ..., UNLIMITED)
    # Everything else the source reports, by name (name review, verification, DNS checks, ...).
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Outbound:
    """Something to send in a conversation: free-form text, an approved template, a location
    (a map pin), choices (a question with options to tap), a contact card or an image."""

    kind: Literal["text", "template", "location", "choices", "contact", "image"]
    text: str = ""
    template: str | None = None
    language: str | None = None
    params: dict[str, str] = field(default_factory=dict)  # NAMED template parameters
    # Template quick-reply buttons, in order: the payload each one sends back when pressed
    # (e.g. an invite token), so the reply can be matched to what it answers.
    buttons: tuple[str, ...] = ()
    # The app's own key for what this message is about (e.g. "invite:<id>"): its delivery,
    # reads and replies can then be looked up by it (`observe.query.interactions`).
    ref: str | None = None
    # A one-time code for an authentication template (WhatsApp: body {{1}} + copy-code button).
    # The outbox blanks it once sent: the code isn't kept in plain text after it left.
    otp: str | None = None
    # A location (WGS 84) with the place's name and address shown under the pin.
    lat: float | None = None
    lng: float | None = None
    place: str | None = None
    address: str | None = None
    # Choices: the question (`body`), its options, and the label of the button that opens them
    # when a channel shows them as a list (WhatsApp: 4+ options, or ones with descriptions).
    body: str | None = None
    choices: tuple[Choice, ...] = ()
    choices_label: str | None = None
    # A contact card: a person's name and phone number (E.164 with "+"), with an optional
    # organisation and title.
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_org: str | None = None
    contact_title: str | None = None
    # An image: the file's key in meydan.media and an optional caption under it.
    media: str | None = None
    caption: str | None = None
    # What an agent reads of this message later, in its history, instead of `text`: the person
    # and anyone allowed to see the conversation get the real message (meydan.observe), the model
    # a line like "[timetable: 23 Nov, 4 lessons]" (docs/privacy.md). Left out: `text`.
    recall: str | None = None
    # Sent in this message's place if it cannot be delivered (refused for good, or reported failed
    # later); forgotten once it is delivered (docs/failures.md, "When a message cannot go").
    fallback: "Outbound | None" = field(default=None, compare=False)
    # Filled by the sender when the message leaves, never stored: the channel's id for the file
    # (WhatsApp) or its bytes and type (a channel that takes the file itself).
    channel_media_id: str | None = field(default=None, compare=False, repr=False)
    media_bytes: bytes | None = field(default=None, compare=False, repr=False)
    media_mime: str | None = field(default=None, compare=False, repr=False)

    @classmethod
    def text_message(
        cls,
        text: str,
        *,
        ref: str | None = None,
        recall: str | None = None,
        fallback: "Outbound | None" = None,
    ) -> "Outbound":
        """Text. `recall`: what an agent reads of it later instead of the text itself.
        `fallback`: sent instead if it cannot be delivered."""
        return cls(kind="text", text=text, ref=ref, recall=recall, fallback=fallback)

    @classmethod
    def template_message(
        cls,
        template: str,
        language: str,
        /,
        *,
        buttons: tuple[str, ...] = (),
        ref: str | None = None,
        media: str | None = None,
        recall: str | None = None,
        fallback: "Outbound | None" = None,
        **params: str,
    ) -> "Outbound":
        """An approved template. `params` fill its body: by name (`name="Ada"`) for a template
        with named parameters, or `**{"1": "Ada"}` for one with numbered ones ({{1}}, {{2}}…).
        `media`, a key in meydan.media, is its image header: uploaded like an image message."""
        # Positional-only, so a template parameter may be called `name` or `template` too.
        return cls(
            kind="template",
            template=template,
            language=language,
            params=params,
            buttons=buttons,
            ref=ref,
            media=media,
            recall=recall,
            fallback=fallback,
        )

    @classmethod
    def code_message(
        cls, template: str, language: str, code: str, *, ref: str | None = None
    ) -> "Outbound":
        """An authentication template carrying a one-time code (e.g. a sign-in code)."""
        return cls(kind="template", template=template, language=language, otp=code, ref=ref)

    @classmethod
    def location_message(
        cls,
        lat: float,
        lng: float,
        *,
        place: str | None = None,
        address: str | None = None,
        ref: str | None = None,
    ) -> "Outbound":
        """A map pin. `text` keeps the place and address, so transcripts (what an agent reads as
        history) show what was sent."""
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError(f"not a WGS 84 coordinate: {lat}, {lng}")
        text = "\n".join(x for x in (place, address) if x)
        return cls(
            kind="location", text=text, lat=lat, lng=lng, place=place, address=address, ref=ref
        )

    @classmethod
    def choices_message(
        cls,
        body: str,
        choices: "list[Choice] | tuple[Choice, ...]",
        *,
        label: str | None = None,
        ref: str | None = None,
    ) -> "Outbound":
        """A question with 1-10 options to tap; the picked option's `id` comes back
        (`reply_payload`). `text` keeps the question and the options, so transcripts show what
        was offered. How it looks is the channel's call (WhatsApp: up to 3 short options as
        buttons, else a list opened by `label`)."""
        choices = tuple(choices)
        if not body.strip():
            raise ValueError("choices need a question")
        if not 1 <= len(choices) <= CHOICES_MAX:
            raise ValueError(f"choices: 1-{CHOICES_MAX} options, got {len(choices)}")
        if any(not c.id or not c.title.strip() for c in choices):
            raise ValueError("every option needs an id and a title")
        if len({c.id for c in choices}) < len(choices):
            raise ValueError("option ids must differ")
        if len({c.title.casefold() for c in choices}) < len(choices):
            raise ValueError("option titles must differ")
        text = "\n".join([body, *(f"• {c.title}" for c in choices)])
        return cls(kind="choices", text=text, body=body, choices=choices, choices_label=label,
                   ref=ref)  # fmt: skip

    @classmethod
    def contact_message(
        cls,
        name: str,
        phone: str,
        *,
        org: str | None = None,
        title: str | None = None,
        ref: str | None = None,
    ) -> "Outbound":
        """A contact card someone can message, call or save. `phone` is E.164 with "+".
        `text` keeps the name and number, so transcripts show whose card was sent."""
        if not name.strip():
            raise ValueError("a contact card needs a name")
        if not (phone.startswith("+") and phone[1:].isdigit() and 8 <= len(phone) <= 16):
            raise ValueError("a contact card's phone is E.164 with '+', e.g. +15550001111")
        text = "\n".join(x for x in (name, title, org, phone) if x)
        return cls(kind="contact", text=text, contact_name=name, contact_phone=phone,
                   contact_org=org, contact_title=title, ref=ref)  # fmt: skip

    @classmethod
    def image_message(
        cls,
        media: str,
        *,
        caption: str | None = None,
        alt: str | None = None,
        ref: str | None = None,
        fallback: "Outbound | None" = None,
    ) -> "Outbound":
        """An image, by its key in meydan.media (`media.put`): it arrives like a photo someone
        sent. `caption` is shown under it; `alt` only says in transcripts what it was (e.g.
        "Spring Cup poster"), so an agent reading the conversation knows it went out.
        `fallback`: sent instead if the image cannot be delivered (a text saying the same)."""
        if not media:
            raise ValueError("an image needs a media key (meydan.media.put)")
        text = "\n".join(x for x in (f"[image: {alt}]" if alt else "[image]", caption) if x)
        return cls(
            kind="image", text=text, media=media, caption=caption, ref=ref, fallback=fallback
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "template": self.template,
            "language": self.language,
            "params": self.params,
            "buttons": list(self.buttons),
            "ref": self.ref,
            "otp": self.otp,
            "lat": self.lat,
            "lng": self.lng,
            "place": self.place,
            "address": self.address,
            "body": self.body,
            "choices": [
                {"id": c.id, "title": c.title, "description": c.description} for c in self.choices
            ],
            "choices_label": self.choices_label,
            "contact_name": self.contact_name,
            "contact_phone": self.contact_phone,
            "contact_org": self.contact_org,
            "contact_title": self.contact_title,
            "media": self.media,
            "caption": self.caption,
            "recall": self.recall,
            "fallback": self.fallback.to_payload() if self.fallback else None,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "Outbound":
        return cls(
            **{
                **data,
                "buttons": tuple(data.get("buttons") or ()),
                "choices": tuple(Choice(**c) for c in data.get("choices") or ()),
                "fallback": cls.from_payload(data["fallback"]) if data.get("fallback") else None,
            }
        )


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


def reply_payload(message: Inbound) -> str | None:
    """What a pressed button carries, on any channel: a template quick reply's or a tapped
    option's payload (kind "button": WhatsApp quick replies, Telegram inline buttons, Instagram
    quick replies), or a WhatsApp interactive button's or list row's id. None for anything else."""
    raw = message.payload
    if message.kind == "button":
        return raw.get("button", {}).get("payload")
    if message.kind == "interactive":
        inter = raw.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("id")
    return None
