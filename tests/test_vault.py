"""Secrets sealed at rest (meydan.vault): AES-256-GCM, bound to where they are kept, rotatable."""

import pytest
from cryptography.exceptions import InvalidTag

from meydan.vault import Keyring


def test_seal_and_open_only_with_the_same_aad():
    kr = Keyring({"k1": Keyring.new_key()}, current="k1")
    sealed = kr.seal(b"whsec_x", aad=b"row:1")
    assert kr.open(sealed, aad=b"row:1") == b"whsec_x"
    assert b"whsec_x" not in sealed.encode()
    with pytest.raises(InvalidTag):  # copied to another row: does not open
        kr.open(sealed, aad=b"row:2")


def test_a_new_current_key_still_opens_what_the_old_one_sealed():
    old = Keyring.new_key()
    sealed = Keyring({"k1": old}, current="k1").seal(b"v", aad=b"a")
    kr = Keyring.parse(f"k2:{Keyring.new_key()},k1:{old}")
    assert kr.current == "k2" and kr.needs_reseal(sealed)
    assert kr.open(sealed, aad=b"a") == b"v"
    with pytest.raises(KeyError):
        Keyring.parse(f"k3:{Keyring.new_key()}").open(sealed, aad=b"a")


@pytest.mark.parametrize("value", ["", "k1", "k1:short", "k$:" + "A" * 44])
def test_bad_keyrings_fail_at_start(value):
    with pytest.raises(ValueError):
        Keyring.parse(value)
