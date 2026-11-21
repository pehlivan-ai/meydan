"""What Meta's messaging platforms share (WhatsApp Cloud API, Instagram messaging): the webhook
signature. Each platform's own shapes stay in its adapter."""

import hashlib
import hmac


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Meta's webhook signature, the same on WhatsApp and Instagram:
    X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app_secret, raw body)."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header.removeprefix("sha256="))
