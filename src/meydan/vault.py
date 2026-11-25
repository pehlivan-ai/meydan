"""Secrets kept encrypted at rest (extra `bridge`): AES-256-GCM with keys from the app's config.

A `Keyring` holds one or more 32-byte keys by id; it seals with the current one and opens with
whichever sealed it, so the key can be rotated (add a new current key, re-seal, drop the old).
`aad` binds a sealed value to where it is kept (a table and a row id, say): a value copied to
another row does not open there.

    keyring = Keyring.from_env()                 # MEYDAN_KEYRING="k2:<base64>,k1:<base64>"
    sealed = keyring.seal(b"whsec_...", aad=b"credential:<id>")
    keyring.open(sealed, aad=b"credential:<id>")

New key: `python -c "from meydan.vault import Keyring; print(Keyring.new_key())"`. Losing every
key loses every sealed value; keep the key where the app's other secrets are (not in the
database it protects).
"""

import base64
import os
import secrets
from collections.abc import Mapping

ENV = "MEYDAN_KEYRING"
_NONCE = 12


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


class Keyring:
    def __init__(self, keys: Mapping[str, str], current: str):
        if current not in keys:
            raise ValueError("the current key is not in the keyring")
        self.keys: dict[str, bytes] = {}
        for kid, key in keys.items():
            if not kid or "$" in kid or ":" in kid or "," in kid:
                raise ValueError(f"bad key id: {kid!r}")
            raw = base64.b64decode(key, validate=True)
            if len(raw) != 32:
                raise ValueError(f"key {kid!r} is not 32 bytes")
            self.keys[kid] = raw
        self.current = current

    @classmethod
    def from_env(cls, var: str = ENV) -> "Keyring | None":
        """`id:base64,id:base64`, the first is current; None when unset."""
        value = os.environ.get(var, "").strip()
        return cls.parse(value) if value else None

    @classmethod
    def parse(cls, value: str) -> "Keyring":
        pairs = [p.strip().split(":", 1) for p in value.split(",") if p.strip()]
        if not pairs or any(len(p) != 2 for p in pairs):
            raise ValueError("a keyring is id:base64[,id:base64...]")
        return cls(dict(pairs), current=pairs[0][0])  # type: ignore[arg-type]

    @staticmethod
    def new_key() -> str:
        return _b64(secrets.token_bytes(32))

    def seal(self, plaintext: bytes, *, aad: bytes) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = secrets.token_bytes(_NONCE)
        ct = AESGCM(self.keys[self.current]).encrypt(nonce, plaintext, aad)
        return f"{self.current}${_b64(nonce)}${_b64(ct)}"

    def open(self, sealed: str, *, aad: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        kid, nonce, ct = sealed.split("$", 2)
        if kid not in self.keys:
            raise KeyError(f"sealed with key {kid!r}, which is not in the keyring")
        return AESGCM(self.keys[kid]).decrypt(base64.b64decode(nonce), base64.b64decode(ct), aad)

    def needs_reseal(self, sealed: str) -> bool:
        return sealed.split("$", 1)[0] != self.current


__all__ = ["ENV", "Keyring"]
